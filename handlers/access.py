"""
Access control — who is allowed to use the bot.

Everything that existed when this shipped was grandfathered in (see migration
010), so nothing that was working stopped. From then on:

  • a group the bot is added to starts unapproved
  • a person who has never used the bot before starts unapproved

Either way the owner gets a DM naming them and the exact command to let them in.

The gate runs in handler group -2, ahead of every other handler, and raises
ApplicationHandlerStop so an unapproved chat reaches nothing at all — no Claude
calls, no replies.

In a group it answers only someone actually trying to use it — a meal photo, a
command, an @mention, a reply to the bot. Those earn a short "not approved yet"
instead of an analysis, because silence there just looks broken. Ordinary
chatter gets nothing: answering that is what made the bot butt into
conversations it had no business in. A one-to-one chat always gets an answer,
since messaging the bot directly is deliberate. The owner is told either way.

Revoking is distinct from never having been approved: a revoked group or person
is dropped in complete silence, with no repeat notification about a decision the
owner already made.

If no owner is configured the gate is disabled and says so loudly at startup.
Failing open is deliberate: locking the owner out of their own bot would need an
env-var change and a redeploy to undo, which is worse than a gap in cover.
"""

import logging
import os
import time
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from services import db, formatter

logger = logging.getLogger(__name__)

# OWNER_TELEGRAM_ID is the intended name; INSTRUCTOR_TELEGRAM_ID is honoured so
# existing deployments keep working without an env change.
_OWNER_ENV_VARS = ("OWNER_TELEGRAM_ID", "INSTRUCTOR_TELEGRAM_ID")

# The owner hears about each new chat or person once per process. Re-nagging them
# about something they have chosen to ignore is not useful.
_notified_owner: set = set()

# In-chat notices are rate limited rather than one-shot, so a second person
# trying an hour later still learns why nothing happened, without the bot
# answering every photo in a room it is not approved for.
_last_notice: dict = {}
NOTICE_COOLDOWN_SECONDS = 600

DENIED_MESSAGE = (
    "🔒 This bot is private. I've let the owner know you'd like access — "
    "they can approve you from their end."
)

PENDING_CHAT_MESSAGE = (
    "🔒 I'm not approved for this chat yet, so I can't analyse that. "
    "I've asked the owner — once they approve it, send the photo again."
)

PENDING_USER_MESSAGE = (
    "🔒 You're not approved to use me yet. I've let the owner know — "
    "once they approve you, try again."
)


def owner_id() -> Optional[int]:
    for name in _OWNER_ENV_VARS:
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                logger.warning("%s is set but is not a number: %r", name, raw)
    return None


def is_owner(telegram_id: int) -> bool:
    oid = owner_id()
    return oid is not None and oid == telegram_id


def log_access_mode() -> None:
    """Say at startup whether the gate is on, so a missing env var is visible."""
    oid = owner_id()
    if oid is None:
        logger.warning(
            "ACCESS CONTROL DISABLED — no OWNER_TELEGRAM_ID (or INSTRUCTOR_TELEGRAM_ID) set, "
            "so anyone who finds this bot can use it. Set one to require approval."
        )
    else:
        logger.info("Access control active — owner is telegram_id=%s", oid)


# Commands that manage access itself. These keep working for the owner in any
# chat, so revoking a group can never strand them with no way back in.
_ACCESS_COMMANDS = ("/approve", "/revoke", "/access")


def _is_access_command(message) -> bool:
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip().lower()
    return text.startswith(_ACCESS_COMMANDS)


async def _tell_owner(context, text: str, key) -> None:
    """DM the owner once about a blocked chat or person."""
    oid = owner_id()
    if oid is None or key in _notified_owner:
        return
    _notified_owner.add(key)
    try:
        await context.bot.send_message(chat_id=oid, text=text)
    except Exception:
        logger.warning("Could not notify owner %s about %s", oid, key, exc_info=True)


def _is_use_attempt(message, bot) -> bool:
    """Is this someone actually trying to use the bot, rather than just talking?

    A meal photo, a command, an @mention or a reply to the bot are all deliberate.
    Getting silence for those looks broken, so they earn an explanation. Ordinary
    chatter does not — answering that is what made the bot butt into conversations
    it had no business in.
    """
    if message is None:
        return False
    if getattr(message, "photo", None):
        return True

    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "")
    if text.strip().startswith("/"):
        return True

    username = (getattr(bot, "username", "") or "").lower()
    if username and f"@{username}" in text.lower():
        return True

    replied = getattr(message, "reply_to_message", None)
    if replied is not None and getattr(replied, "from_user", None) is not None:
        if replied.from_user.id == bot.id:
            return True
    return False


def _notice_due(key) -> bool:
    """Rate limit in-chat notices to one per key per NOTICE_COOLDOWN_SECONDS."""
    now = time.monotonic()
    last = _last_notice.get(key)
    if last is not None and (now - last) < NOTICE_COOLDOWN_SECONDS:
        return False
    _last_notice[key] = now
    return True


async def _say(update: Update, text: str, key) -> None:
    """Send an in-chat notice, at most once per key per cooldown."""
    message = update.effective_message
    if message is None or not _notice_due(key):
        return
    try:
        await message.reply_text(text)
    except Exception:
        logger.debug("Could not send access notice", exc_info=True)


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block anything not approved. Runs before every other handler."""
    if owner_id() is None:
        return  # Gate disabled — see log_access_mode()

    tg_user = update.effective_user
    chat = update.effective_chat
    if tg_user is None or chat is None or tg_user.is_bot:
        return

    owner = is_owner(tg_user.id)

    # The owner keeps access to the access commands themselves, everywhere. Their
    # ordinary messages do not bypass anything: a group they revoked is off for
    # them too, which is the whole point of revoking it.
    if owner and _is_access_command(update.effective_message):
        return

    is_group = chat.type in ("group", "supergroup")

    if is_group:
        # Register first so the group shows up in /access. New rows are unapproved.
        try:
            await db.register_group(chat.id, chat.title or "")
        except Exception:
            logger.warning("Could not register group %s during access check", chat.id, exc_info=True)

        state = await db.get_group_access(chat.id)
        if not state.get("approved"):
            if state.get("revoked_at") is None:
                logger.info("Blocked pending group %s (%s)", chat.id, chat.title)
                await _tell_owner(
                    context,
                    f"🔒 New group wants access: {chat.title or 'untitled'}\n"
                    f"chat_id: {chat.id}\n\n"
                    f"Send /approve in that group to allow it, or ignore this to keep it blocked.",
                    ("chat", chat.id),
                )
                # Awaiting approval: explain, but only to someone actually trying
                # to use the bot. A revoked group gets nothing at all — the owner
                # turned it off on purpose and does not need it advertised.
                if _is_use_attempt(update.effective_message, context.bot):
                    await _say(update, PENDING_CHAT_MESSAGE, ("chat", chat.id))
            else:
                logger.debug("Ignoring revoked group %s (%s)", chat.id, chat.title)
            raise ApplicationHandlerStop

    if owner:
        return  # Owner is always an approved person

    try:
        user = await db.get_or_create_user_from_tg(tg_user)
    except Exception:
        logger.warning("Could not resolve user %s during access check", tg_user.id, exc_info=True)
        return  # Fail open rather than lock out a working bot on a DB blip

    if not user.get("approved", True):
        if user.get("revoked_at") is None:
            logger.info("Blocked new person %s (%s)", tg_user.id, user.get("name"))
            handle = f"@{user['username']}" if user.get("username") else user.get("name", "unknown")
            where = chat.title if is_group else "a private chat"
            await _tell_owner(
                context,
                f"🔒 New person wants access: {handle}\n"
                f"telegram_id: {tg_user.id}\n"
                f"seen in: {where}\n\n"
                f"Approve with:  /approve {tg_user.id}",
                ("user", tg_user.id),
            )
            if not is_group:
                await _say(update, DENIED_MESSAGE, ("user", tg_user.id))
            elif _is_use_attempt(update.effective_message, context.bot):
                await _say(update, PENDING_USER_MESSAGE, ("user", tg_user.id))
        else:
            logger.debug("Ignoring revoked person %s", tg_user.id)
        raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Owner commands
# ---------------------------------------------------------------------------

_NOT_OWNER = "🔒 Only the bot owner can manage access."


async def _set_access(update: Update, context: ContextTypes.DEFAULT_TYPE, approved: bool) -> None:
    message = update.effective_message
    chat = update.effective_chat
    verb = "Approved" if approved else "Revoked"

    if not is_owner(update.effective_user.id):
        await message.reply_text(_NOT_OWNER)
        return

    args = context.args or []

    # No argument in a group means "this group".
    if not args:
        if chat.type not in ("group", "supergroup"):
            await message.reply_text(
                f"Usage:\n"
                f"  /{'approve' if approved else 'revoke'} — in a group, {verb.lower()} that group\n"
                f"  /{'approve' if approved else 'revoke'} <@handle or telegram_id> — for a person\n\n"
                f"See everything with /access."
            )
            return
        ok = await db.set_group_approved(chat.id, approved)
        await message.reply_text(
            f"✅ {verb} this group ({chat.title})." if ok
            else "⚠️ Could not find this group in the database."
        )
        return

    token = args[0]
    user = await db.get_user_by_handle_or_id(token)
    if user is None:
        await message.reply_text(
            f"No one matching '{token}'. They need to have messaged the bot at least once, "
            f"or use their numeric telegram_id (shown in the access request)."
        )
        return

    ok = await db.set_user_approved(user["telegram_id"], approved)
    handle = f"@{user['username']}" if user.get("username") else user["name"]
    await message.reply_text(
        f"✅ {verb} {handle} (telegram_id {user['telegram_id']})." if ok
        else f"⚠️ Could not update {handle}."
    )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve — allow this group, or /approve <@handle|id> for a person."""
    await _set_access(update, context, True)


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/revoke — block this group, or /revoke <@handle|id> for a person."""
    await _set_access(update, context, False)


async def cmd_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/access — list every group and anyone waiting on approval."""
    message = update.effective_message
    if not is_owner(update.effective_user.id):
        await message.reply_text(_NOT_OWNER)
        return

    if owner_id() is None:
        await message.reply_text("Access control is disabled — no owner id configured.")
        return

    groups, pending = await db.get_access_overview()
    allowed = [g for g in groups if g["approved"]]
    blocked = [g for g in groups if not g["approved"]]

    lines = [f"🔑 *Access* — {len(allowed)} group\\(s\\) allowed, {len(blocked)} blocked", ""]

    def _rows(items, limit=15):
        out = []
        for g in items[:limit]:
            title = formatter.escape(g["title"] or "untitled")
            out.append(f"  {title} \\({formatter.escape(str(g['chat_id']))}\\)")
        if len(items) > limit:
            out.append(formatter.escape(f"  …and {len(items) - limit} more"))
        return out

    if allowed:
        lines.append("✅ *Allowed*")
        lines.extend(_rows(allowed))
        lines.append("")
    if blocked:
        lines.append("⛔ *Blocked*")
        lines.extend(_rows(blocked))
        lines.append("")
    if pending:
        lines.append("⏳ *People waiting*")
        for u in pending[:15]:
            handle = f"@{u['username']}" if u.get("username") else u["name"]
            lines.append(
                f"  {formatter.escape(handle)} — `/approve {formatter.escape(str(u['telegram_id']))}`"
            )
        if len(pending) > 15:
            lines.append(formatter.escape(f"  …and {len(pending) - 15} more"))
        lines.append("")

    lines.append(formatter.escape("Revoke a group by sending /revoke inside it."))
    await message.reply_text("\n".join(lines).rstrip(), parse_mode=ParseMode.MARKDOWN_V2)
