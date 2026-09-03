"""
Daily calorie targets.

The trainer sets each client's daily number; the bot then reports progress under
every meal analysis and flags anything that takes them over.

Only owners can set or clear a target — a client able to move their own goalpost
would make the warning meaningless. Anyone in the group can look one up.

Targets live on the user, not the group: a calorie target belongs to the person
rather than the room. Without one the meal analysis falls back to flagging
unusually large single meals, so the feature still says something useful before
any target has been set.
"""

import logging
import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter
from handlers.access import is_owner
from handlers.commands import _member_label, _resolve_target_user

logger = logging.getLogger(__name__)


def meal_kcal_limit() -> int:
    """Single-meal size that draws a comment when a client has no daily target."""
    try:
        return int(os.environ.get("MEAL_KCAL_WARN", "900"))
    except ValueError:
        return 900


_NOT_TRAINER = "🔒 Only the trainer can set calorie targets."
_GROUP_ONLY = "Calorie targets work in your program group, not here."


async def _require_trainer_in_group(update: Update) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if not is_owner(update.effective_user.id):
        await message.reply_text(_NOT_TRAINER)
        return False
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(_GROUP_ONLY)
        return False
    return True


async def cmd_settarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settarget <name> <kcal> — set a client's daily calorie target."""
    message = update.effective_message
    if not await _require_trainer_in_group(update):
        return

    args = context.args or []
    if len(args) < 2:
        await message.reply_text(
            "Usage: /settarget <name> <kcal>\n"
            "e.g. /settarget @minghui_t 1800\n\n"
            "Clear one with /cleartarget <name>."
        )
        return

    # Number last, so names with spaces still work.
    try:
        target = int(args[-1].replace(",", ""))
    except ValueError:
        await message.reply_text(
            f"'{args[-1]}' isn't a number. Put the target last, e.g. /settarget Ming 1800"
        )
        return
    if not 500 <= target <= 10000:
        await message.reply_text(
            "That looks off — give a daily target between 500 and 10000 kcal."
        )
        return

    original = context.args
    context.args = args[:-1]
    client, error = await _resolve_target_user(update, context)
    context.args = original
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not await db.set_calorie_target(client["id"], target):
        await message.reply_text("⚠️ Could not save that. Please try again.")
        return

    await message.reply_text(
        f"🎯 {_member_label(client)}'s daily target is now {target:,} kcal.\n"
        f"I'll show their running total under every meal from here."
    )


async def cmd_cleartarget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cleartarget <name> — remove a client's daily target."""
    message = update.effective_message
    if not await _require_trainer_in_group(update):
        return

    if not (context.args or []):
        await message.reply_text("Usage: /cleartarget <name>")
        return

    client, error = await _resolve_target_user(update, context)
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not await db.set_calorie_target(client["id"], None):
        await message.reply_text("⚠️ Could not save that. Please try again.")
        return

    await message.reply_text(
        f"🎯 Cleared {_member_label(client)}'s daily target. "
        f"I'll only flag unusually large single meals now."
    )


async def cmd_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/target [name] — show a target. A trainer with no name gets the whole group."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(_GROUP_ONLY)
        return

    # A trainer asking without naming anyone wants the roster, not their own row.
    if not (context.args or []) and is_owner(update.effective_user.id):
        rows = await db.get_group_targets(message.chat_id)
        if not rows:
            await message.reply_text("Nobody in this group yet.")
            return
        lines = ["🎯 *Daily targets*", ""]
        for r in rows:
            handle = (r.get("username") or "").strip()
            label = f"@{handle}" if handle else (r.get("display_name") or r["name"])
            tgt = r.get("daily_calorie_target")
            value = f"{tgt:,} kcal" if tgt else "not set"
            lines.append(f"👤 *{formatter.escape(label)}* — {formatter.escape(value)}")
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        return

    client, error = await _resolve_target_user(update, context)
    if error:
        await message.reply_text(error, parse_mode=ParseMode.MARKDOWN_V2)
        return

    target = await db.get_calorie_target(client["id"])
    label = _member_label(client)
    if not target:
        await message.reply_text(
            f"No daily target set for {label}. "
            f"The trainer can set one with /settarget {label} 1800"
        )
        return
    await message.reply_text(f"🎯 {label}'s daily target: {target:,} kcal")
