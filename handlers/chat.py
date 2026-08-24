"""
Text message dispatcher — meal corrections and natural-language conversation.

A single handler owns plain text so the two features cannot shadow each other:
python-telegram-bot stops after the first matching handler in a group, so
registering corrections and chat separately would mean whichever came first
swallowed every reply.

Order of precedence for a text message:
  1. A reply to one of the bot's meal analyses → correction (see handlers.photo)
  2. Otherwise, if the bot is being addressed → conversational reply
  3. Otherwise → ignored

"Addressed" means: any message in a private chat, a reply to something the bot
said, or a message that @mentions the bot. Group chatter that is none of these is
left alone.
"""

import logging
import os
import re
import time

from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from services import chat as chat_service
from services import db
from handlers.photo import handle_meal_correction

logger = logging.getLogger(__name__)

_HISTORY_KEY = "nl_history"

# --- Spend guards -------------------------------------------------------------
# Every conversational reply costs an API call, so three cheap local checks run
# before one is made: skip pure acknowledgements, throttle rapid-fire messages,
# and cap how many replies one person can draw in a day.

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


COOLDOWN_SECONDS = _env_int("CHAT_COOLDOWN_SECONDS", 3)
DAILY_REPLY_LIMIT = _env_int("CHAT_DAILY_LIMIT", 40)

_LAST_REPLY_KEY = "nl_last_reply_ts"
_QUOTA_KEY = "nl_quota"

# Messages that carry no question and expect no answer. Replying to these is
# pure spend, and in a group they arrive constantly.
_TRIVIAL = {
    "ok", "okay", "k", "kk", "ok lah", "okla", "thanks", "thank you", "thx", "ty",
    "nice", "cool", "good", "great", "wah", "shiok", "steady", "solid", "same",
    "haha", "hahaha", "lol", "lmao", "hehe", "yes", "yeah", "yep", "ya", "no",
    "nope", "sure", "alright", "noted", "got it", "gotcha", "true", "fair",
    "understood", "roger", "done", "bye", "hi", "hello", "hey",
}

# Greetings get a canned answer rather than an API call: being tagged and
# ignored is worse than any saving, and a hello needs no model to answer.
_GREETINGS = {
    "hi", "hello", "hey", "yo", "sup", "hola", "helo", "hai",
    "morning", "good morning", "good afternoon", "good evening", "gm",
}

FALLBACK_REPLY = (
    "Sorry, I couldn't think of a reply just then. Try asking again, "
    "or use /today to see where you're at."
)

QUOTA_REPLY = (
    "I've hit my chat limit for today — keeping the API bill sensible 🙂 "
    "Commands still work: /today, /dailymeals, /weeklymeals, /myreport."
)


def _is_trivial(text: str) -> bool:
    """True for acknowledgements that do not warrant an API call."""
    if "?" in text:
        return False
    stripped = text.strip().strip(".!,~ ").lower()
    if stripped in _TRIVIAL:
        return True
    # Reactions with no letters or digits at all (emoji, punctuation).
    return bool(stripped) and not any(ch.isalnum() for ch in stripped)


def _throttled(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if this user replied too recently. Per user, and only for non-explicit
    messages — see the call site in _converse."""
    if COOLDOWN_SECONDS <= 0:
        return False
    now = time.monotonic()
    last = context.user_data.get(_LAST_REPLY_KEY)
    if last is not None and (now - last) < COOLDOWN_SECONDS:
        return True
    context.user_data[_LAST_REPLY_KEY] = now
    return False


def _quota_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Per-user reply counter, reset at Singapore midnight to match the rest of the app."""
    from services.tz import today_sgt
    today = today_sgt().isoformat()
    quota = context.user_data.get(_QUOTA_KEY)
    if not isinstance(quota, dict) or quota.get("date") != today:
        quota = {"date": today, "count": 0, "notified": False}
        context.user_data[_QUOTA_KEY] = quota
    return quota


def _mentions_bot(message, bot) -> bool:
    """True if this message @-mentions the bot.

    Checks the entities Telegram sends as well as the raw text, and compares
    case-insensitively — @MakanLens_AI_Bot is the same handle as @makanlens_ai_bot.
    """
    username = (getattr(bot, "username", "") or "").lower()
    if not username:
        return False

    handle = f"@{username}"
    text = message.text or message.caption or ""

    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == "mention":
            if text[entity.offset:entity.offset + entity.length].lower() == handle:
                return True
        elif entity.type == "text_mention" and getattr(entity, "user", None):
            if entity.user.id == bot.id:
                return True

    return handle in text.lower()


def _address_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Why this message counts as spoken to the bot, or None if it doesn't."""
    message = update.effective_message
    chat = update.effective_chat

    if chat and chat.type == "private":
        return "private"

    try:
        if _mentions_bot(message, context.bot):
            return "mention"
        replied = message.reply_to_message
        if replied and replied.from_user and replied.from_user.id == context.bot.id:
            return "reply"
    except Exception:
        logger.debug("Could not determine whether the bot was addressed", exc_info=True)

    return None


def _is_addressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when the message is clearly meant for the bot."""
    return _address_reason(update, context) is not None


def _strip_mention(text: str, username: Optional[str]) -> str:
    """Remove the bot's @handle so it doesn't clutter the prompt.

    Case-insensitive: Telegram preserves whatever casing the sender typed, and a
    leftover "@MakanLens_AI_Bot" in the prompt confused the model.
    """
    if not username:
        return text.strip()
    cleaned = re.sub(rf"@{re.escape(username)}\b", " ", text, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


async def _converse(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prompt: str,
    *,
    explicit: bool,
) -> None:
    """Generate and send one conversational reply, subject to the spend guards.

    explicit=True for /ask — a deliberate request, so the trivial-message filter
    is skipped (the cooldown and daily cap still apply).
    """
    message = update.effective_message
    chat_id = message.chat_id

    # Greetings and bare mentions are answered here, without an API call. Being
    # tagged and getting nothing back is the worst outcome, and neither needs a model.
    if not prompt or prompt.strip(".!? ").lower() in _GREETINGS:
        # Costs nothing, so it is always worth sending — we only reach _converse
        # when the bot was actually addressed.
        who = (update.effective_user.first_name or "there").strip()
        await message.reply_text(
            f"Hey {who} 👋 Ask me about your food or training — "
            "\"how much protein today?\", \"how is Ming Hui doing?\" — "
            "or try /today for your day so far."
        )
        return

    # The trivial filter is for group chatter ("thanks", "nice" under a meal
    # analysis). An explicit summon is never chatter, so it bypasses this.
    if not explicit and _is_trivial(prompt):
        logger.info("chat: skipped trivial message in %s: %r", chat_id, prompt[:40])
        return

    # The cooldown guards against passing chatter piling up calls. A deliberate
    # summon is never throttled — being tagged and staying silent is exactly the
    # confusing behaviour this is meant to avoid, and the daily cap below already
    # bounds what one person can spend.
    if not explicit and _throttled(context):
        logger.info("chat: throttled in %s (cooldown %ss)", chat_id, COOLDOWN_SECONDS)
        return

    quota = _quota_state(context)
    if DAILY_REPLY_LIMIT > 0 and quota["count"] >= DAILY_REPLY_LIMIT:
        logger.info("chat: daily limit %s reached in %s", DAILY_REPLY_LIMIT, chat_id)
        if not quota["notified"]:
            quota["notified"] = True
            await message.reply_text(QUOTA_REPLY)
        return

    tg_user = update.effective_user
    try:
        user = await db.get_or_create_user_from_tg(tg_user)
    except Exception:
        logger.exception("Could not resolve user %s for chat", tg_user.id)
        return

    is_group = bool(update.effective_chat and update.effective_chat.type in ("group", "supergroup"))

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=message.message_thread_id,
        )
    except Exception:
        pass  # Typing indicator is cosmetic — never let it break the reply

    history = chat_service.trim_history(context.chat_data.get(_HISTORY_KEY, []))

    try:
        reply = await chat_service.generate_reply(
            user, prompt, history=history, is_group=is_group,
            chat_id=message.chat_id if is_group else None,
        )
    except Exception:
        logger.exception("Chat reply generation failed for user %s", user["id"])
        reply = None

    if not reply:
        await message.reply_text(FALLBACK_REPLY)
        return

    quota["count"] += 1

    # In a group the history is shared, so tag who said what.
    speaker = f"{user['name']}: " if is_group else ""
    history.append({"role": "user", "content": f"{speaker}{prompt}"})
    history.append({"role": "assistant", "content": reply})
    context.chat_data[_HISTORY_KEY] = chat_service.trim_history(history)

    # Plain text on purpose — MarkdownV2 would reject ordinary punctuation.
    await message.reply_text(reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    # 1) Meal correction takes priority over conversation. Skip the attempt for
    #    pure acknowledgements — "nice" under someone's analysis is applause, not
    #    an edit, and working that out costs a reasoning-model call.
    if message.reply_to_message and not _is_trivial(text):
        try:
            if await handle_meal_correction(update, context):
                return
        except Exception:
            logger.exception("Meal correction failed in chat %s", message.chat_id)
            return

    # 2) Natural-language reply, but only when the bot is being spoken to.
    reason = _address_reason(update, context)
    if reason is None:
        return

    prompt = _strip_mention(text, context.bot.username)

    # An @mention is someone deliberately summoning the bot, so it always gets an
    # answer. A DM or a reply to the bot is weaker — "ok" or "thanks" there is an
    # acknowledgement, and should still cost nothing.
    await _converse(update, context, prompt, explicit=reason == "mention")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ask <question> — talk to the bot explicitly.

    Useful in groups where privacy mode means plain messages never reach the bot,
    and as a discoverable entry point for people who don't know they can just chat.
    """
    message = update.effective_message
    question = " ".join(context.args).strip() if context.args else ""

    if not question:
        await message.reply_text(
            "Ask me anything about your food or training, e.g.\n"
            "  /ask how much protein have I had today?\n"
            "  /ask is chicken rice a decent post-workout meal?\n\n"
            "In a private chat you can just type normally — no /ask needed."
        )
        return

    await _converse(update, context, question, explicit=True)
