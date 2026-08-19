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

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from services import chat as chat_service
from services import db
from handlers.photo import handle_meal_correction

logger = logging.getLogger(__name__)

_HISTORY_KEY = "nl_history"

FALLBACK_REPLY = (
    "Sorry, I couldn't think of a reply just then. Try asking again, "
    "or use /today to see where you're at."
)


def _is_addressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when the message is clearly meant for the bot."""
    message = update.effective_message
    chat = update.effective_chat

    if chat and chat.type == "private":
        return True

    try:
        replied = message.reply_to_message
        if replied and replied.from_user and replied.from_user.id == context.bot.id:
            return True

        username = context.bot.username
        if username and f"@{username}".lower() in (message.text or "").lower():
            return True
    except Exception:
        logger.debug("Could not determine whether the bot was addressed", exc_info=True)

    return False


def _strip_mention(text: str, username: str | None) -> str:
    """Remove the bot's @handle so it doesn't clutter the prompt."""
    if not username:
        return text.strip()
    cleaned = text.replace(f"@{username}", " ").replace(f"@{username.lower()}", " ")
    return " ".join(cleaned.split())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not text or text.startswith("/"):
        return

    # 1) Meal correction takes priority over conversation.
    if message.reply_to_message:
        try:
            if await handle_meal_correction(update, context):
                return
        except Exception:
            logger.exception("Meal correction failed in chat %s", message.chat_id)
            return

    # 2) Natural-language reply, but only when the bot is being spoken to.
    if not _is_addressed(update, context):
        return

    prompt = _strip_mention(text, context.bot.username)
    if not prompt:
        return

    tg_user = update.effective_user
    try:
        display_name = (
            tg_user.username.lower() if tg_user.username
            else (tg_user.first_name or "user").lower()
        )
        user = await db.get_or_create_user(tg_user.id, display_name)
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
            user, prompt, history=history, is_group=is_group
        )
    except Exception:
        logger.exception("Chat reply generation failed for user %s", user["id"])
        reply = None

    if not reply:
        await message.reply_text(FALLBACK_REPLY)
        return

    # In a group the history is shared, so tag who said what.
    speaker = f"{user['name']}: " if is_group else ""
    history.append({"role": "user", "content": f"{speaker}{prompt}"})
    history.append({"role": "assistant", "content": reply})
    context.chat_data[_HISTORY_KEY] = chat_service.trim_history(history)

    # Plain text on purpose — MarkdownV2 would reject ordinary punctuation.
    await message.reply_text(reply)


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

    tg_user = update.effective_user
    try:
        display_name = (
            tg_user.username.lower() if tg_user.username
            else (tg_user.first_name or "user").lower()
        )
        user = await db.get_or_create_user(tg_user.id, display_name)
    except Exception:
        logger.exception("Could not resolve user %s for /ask", tg_user.id)
        await message.reply_text("Couldn't look you up just now — try /start first.")
        return

    is_group = bool(update.effective_chat and update.effective_chat.type in ("group", "supergroup"))

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
            message_thread_id=message.message_thread_id,
        )
    except Exception:
        pass

    history = chat_service.trim_history(context.chat_data.get(_HISTORY_KEY, []))

    try:
        reply = await chat_service.generate_reply(
            user, question, history=history, is_group=is_group
        )
    except Exception:
        logger.exception("/ask reply generation failed for user %s", user["id"])
        reply = None

    if not reply:
        await message.reply_text(FALLBACK_REPLY)
        return

    speaker = f"{user['name']}: " if is_group else ""
    history.append({"role": "user", "content": f"{speaker}{question}"})
    history.append({"role": "assistant", "content": reply})
    context.chat_data[_HISTORY_KEY] = chat_service.trim_history(history)

    await message.reply_text(reply)
