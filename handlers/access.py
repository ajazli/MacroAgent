"""
Access control — who is allowed to use the bot.

Everything that existed when this shipped was grandfathered in (see migration
010), so nothing that was working stopped. From then on:

  • a group the bot is added to starts unapproved
  • a person who has never used the bot before starts unapproved

Either way the owner gets a DM naming them and the exact command to let them in.

The gate runs in handler group -2, ahead of every other handler, and raises
ApplicationHandlerStop so an unapproved chat reaches nothing at all — no Claude
calls, no logging, no replies beyond one "this bot is private" notice.

If no owner is configured the gate is disabled and says so loudly at startup.
Failing open is deliberate: locking the owner out of their own bot would need an
env-var change and a redeploy to undo, which is worse than a gap in cover.
"""

import logging
import os
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from services import db, formatter

logger = logging.getLogger(__name__)

# OWNER_TELEGRAM_ID is the intended name; INSTRUCTOR_TELEGRAM_ID is honoured so
# existing deployments keep working without an env change.
_OWNER_ENV_VARS = ("OWNER_TELEGRAM_ID", "INSTRUCTOR_TELEGRAM_ID")

# One notice per chat and per user per process, so a blocked group cannot turn
# into a notification loop for either party.
_notified_chats: set = set()
_notified_owner: set = set()

DENIED_MESSAGE = (
    "🔒 This bot is private. I've let the owner know you'd like access — "
    "they can approve you from their end."
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


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE, key) -> None:
    """Tell the requester once that the bot is private."""
    if key in _notified_chats:
        return
    _notified_chats.add(key)
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(DENIED_MESSAGE)
    except Exception:
        logger.debug("Could not send denial notice", exc_info=True)


async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block anything not approved. Runs before every other handler."""
    if owner_id() is None:
        return  # Gate disabled — see log_access_mode()

    tg_user = update.effective_user
    chat = update.effective_chat
    if tg_user is None or chat is None or tg_user.is_bot:
        return

    if is_owner(tg_user.id):
        return  # The owner is never locked out of their own bot

    if chat.type in ("group", "supergroup"):
        # Make sure the group exists in the table so it shows up in /access,
        # then check it. New rows default to unapproved.
        try:
            await db.register_group(chat.id, chat.title or "")
        except Exception:
            logger.warning("Could not register group %s during access check", chat.id, exc_info=True)

        if not await db.is_group_approved(chat.id):
            logger.info("Blocked unapproved group %s (%s)", chat.id, chat.title)
            await _tell_owner(
                context,
                f"🔒 New group wants access: {chat.title or 'untitled'}\n"
                f"chat_id: {chat.id}\n\n"
                f"Send /approve in that group to allow it, or ignore this to keep it blocked.",
                ("chat", chat.id),
            )
            await _deny(update, context, ("chat", chat.id))
            raise ApplicationHandlerStop

    # Whoever is speaking must themselves be approved, in a group or a DM.
    try:
        user = await db.get_or_create_user_from_tg(tg_user)
    except Exception:
        logger.warning("Could not resolve user %s during access check", tg_user.id, exc_info=True)
        return  # Fail open rather than lock out a working bot on a DB blip

    if not user.get("approved", True):
        logger.info("Blocked unapproved user %s (%s)", tg_user.id, user.get("name"))
        handle = f"@{user['username']}" if user.get("username") else user.get("name", "unknown")
        where = chat.title if chat.type in ("group", "supergroup") else "a private chat"
        await _tell_owner(
            context,
            f"🔒 New person wants access: {handle}\n"
            f"telegram_id: {tg_user.id}\n"
            f"seen in: {where}\n\n"
            f"Approve with:  /approve {tg_user.id}",
            ("user", tg_user.id),
        )
        await _deny(update, context, ("user", tg_user.id))
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
