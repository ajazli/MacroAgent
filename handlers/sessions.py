"""
Personal-training session balances.

The trainer sells a package and marks sessions off as they happen; anyone in the
group can check where a client stands.

Only owners — the trainer accounts — can change a balance. A client being able
to add or spend their own sessions would make the number worth nothing, so
/addsessions, /logsession and /undosession are owner-only. /sessions is open to
everyone in the group.

Balances live per group (see migration 013), matching how the rest of the bot is
scoped: these groups are one per client, so the group is the trainer-client pair.

The bot stays quiet unless asked, with one exception — when a package crosses
down to LOW_THRESHOLD sessions, and again when it runs out, it says so once in
the group. Because a session is logged one at a time, crossing a threshold
happens exactly once per package, so no "already warned" state is needed.
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter
from handlers.access import is_owner
from handlers.commands import _member_label, _resolve_target_user

logger = logging.getLogger(__name__)

LOW_THRESHOLD = 2

_NOT_TRAINER = "🔒 Only the trainer can change session counts."
_GROUP_ONLY = "Session tracking works in your program group, not here."


async def _require_trainer_in_group(update: Update) -> bool:
    """Owner, in a group. Replies and returns False when either is missing."""
    message = update.effective_message
    chat = update.effective_chat
    if not is_owner(update.effective_user.id):
        await message.reply_text(_NOT_TRAINER)
        return False
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(_GROUP_ONLY)
        return False
    return True


async def _balance_for(chat_id: int, user_id: int) -> dict:
    rows = await db.get_pt_balances(chat_id, user_id)
    return rows[0] if rows else {
        "used": 0, "purchased": 0, "remaining": 0, "last_session": None,
    }


async def cmd_addsessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addsessions <name> <count> — sell a client a package of sessions."""
    message = update.effective_message
    if not await _require_trainer_in_group(update):
        return

    args = context.args or []
    if len(args) < 2:
        await message.reply_text(
            "Usage: /addsessions <name> <count>\n"
            "e.g. /addsessions @minghui_t 10\n\n"
            "The count is added to whatever they have left."
        )
        return

    # The count is last so names with spaces still work.
    try:
        count = int(args[-1])
    except ValueError:
        await message.reply_text(f"'{args[-1]}' isn't a number. Put the count last, e.g. /addsessions Ming 10")
        return
    if count == 0:
        await message.reply_text("That would change nothing. Use a positive number to add sessions.")
        return

    context.args = args[:-1]
    client, error = await _resolve_target_user(update, context)
    context.args = args
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    trainer = await db.get_or_create_user_from_tg(update.effective_user)
    if not await db.add_pt_entry(message.chat_id, client["id"], count,
                                 note="package", created_by=trainer["id"]):
        await message.reply_text("⚠️ Could not save that. Please try again.")
        return

    row = await _balance_for(message.chat_id, client["id"])
    await message.reply_text(
        formatter.format_pt_balance(_member_label(client), row),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_logsession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/logsession <name> [count] — mark session(s) done."""
    message = update.effective_message
    if not await _require_trainer_in_group(update):
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Usage: /logsession <name> [count]\n"
            "e.g. /logsession @minghui_t   — marks one session done\n"
            "     /logsession Ming 2       — marks two"
        )
        return

    count = 1
    if len(args) > 1 and args[-1].isdigit():
        count = int(args[-1])
        args = args[:-1]
    if count < 1:
        await message.reply_text("Count must be at least 1.")
        return

    original = context.args
    context.args = args
    client, error = await _resolve_target_user(update, context)
    context.args = original
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    before = int((await _balance_for(message.chat_id, client["id"]))["remaining"])

    trainer = await db.get_or_create_user_from_tg(update.effective_user)
    if not await db.add_pt_entry(message.chat_id, client["id"], -count,
                                 note="session", created_by=trainer["id"]):
        await message.reply_text("⚠️ Could not save that. Please try again.")
        return

    row = await _balance_for(message.chat_id, client["id"])
    after = int(row["remaining"])
    label = _member_label(client)

    await message.reply_text(
        formatter.format_pt_balance(label, row),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Warn on the way past a threshold, not merely while under it — otherwise
    # every session below the line would repeat the same message.
    if before > 0 >= after:
        await message.reply_text(
            f"⚠️ {label} has no sessions left — time to renew."
        )
    elif before > LOW_THRESHOLD >= after:
        await message.reply_text(
            f"⚠️ {label} is down to {after} session{'s' if after != 1 else ''}."
        )


async def cmd_undosession(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/undosession <name> — remove the most recent entry for a client."""
    message = update.effective_message
    if not await _require_trainer_in_group(update):
        return

    if not (context.args or []):
        await message.reply_text("Usage: /undosession <name>\n\nRemoves their most recent entry.")
        return

    client, error = await _resolve_target_user(update, context)
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    removed = await db.delete_last_pt_entry(message.chat_id, client["id"])
    if removed is None:
        await message.reply_text(f"Nothing recorded for {_member_label(client)} in this group yet.")
        return

    delta = int(removed["delta"])
    what = f"a package of {delta}" if delta > 0 else f"{abs(delta)} session(s) used"
    row = await _balance_for(message.chat_id, client["id"])
    await message.reply_text(f"↩️ Removed {what}.")
    await message.reply_text(
        formatter.format_pt_balance(_member_label(client), row),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sessions [name] — session balance. Trainers with no name get the whole group."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(_GROUP_ONLY)
        return

    # A trainer asking without naming anyone wants the roster, not their own row.
    if not (context.args or []) and is_owner(update.effective_user.id):
        rows = await db.get_pt_balances(message.chat_id)
        await message.reply_text(formatter.format_pt_group(rows),
                                 parse_mode=ParseMode.MARKDOWN_V2)
        return

    client, error = await _resolve_target_user(update, context)
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    rows = await db.get_pt_balances(message.chat_id, client["id"])
    if not rows:
        await message.reply_text(
            f"No sessions recorded for {_member_label(client)} in this group yet."
        )
        return

    await message.reply_text(
        formatter.format_pt_balance(_member_label(client), rows[0]),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
