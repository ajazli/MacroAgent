"""
User-facing command handlers: /start, /log, /today, /health
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_display_name(update: Update) -> str:
    user = update.effective_user
    if user.username:
        return user.username.lower()
    return (user.first_name or "user").lower()


async def _ensure_registered(update: Update) -> dict:
    """Auto-register user if not yet in DB and return the user row."""
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, _user_display_name(update))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _ensure_registered(update)
    name = formatter.escape(user["name"])
    msg = (
        f"👋 Hey *{name}*\\! Welcome to *Jazli's Macro Agent* 🏋️\n\n"
        "I'll help you track meals, workouts, weight, steps, and water\\.\n\n"
        "*Nutrition & basics:*\n"
        "`/log weight 74\\.2` — log weight\n"
        "`/log steps 8500` — log steps\n"
        "`/log water 500` — log water \\(ml\\)\n"
        "`/log workout chest day 45min` — free\\-form workout note\n"
        "`/today` — today's summary\n\n"
        "*Tracked exercises:*\n"
        "`/pushups` — log push\\-ups \\(reps × sets\\)\n"
        "`/situps` — log sit\\-ups \\(reps × sets\\)\n"
        "`/planks` — log planks \\(reps × sets\\)\n"
        "`/run` — log a run \\(distance → timing\\)\n"
        "`/jog` — log a jog \\(distance → timing\\)\n"
        "`/cancel` — cancel a pending exercise entry\n\n"
        "📸 Send any photo and I'll analyse it as a meal\\!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Bot is running")


# ---------------------------------------------------------------------------
# /log
# ---------------------------------------------------------------------------

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args  # list of strings after /log
    user = await _ensure_registered(update)

    if not args:
        await update.message.reply_text(
            formatter.escape(
                "Usage:\n"
                "/log weight 74.2\n"
                "/log steps 8500\n"
                "/log water 500\n"
                "/log workout <description>"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    sub = args[0].lower()

    try:
        if sub == "weight":
            await _log_weight(update, user, args[1:])
        elif sub == "steps":
            await _log_steps(update, user, args[1:])
        elif sub == "water":
            await _log_water(update, user, args[1:])
        elif sub == "workout":
            await _log_workout(update, user, args[1:])
        else:
            await update.message.reply_text(
                formatter.escape(f"Unknown log type '{sub}'. Use: weight, steps, water, workout."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception:
        logger.exception("Error in cmd_log sub=%s user_id=%s", sub, user["id"])
        await update.message.reply_text(
            formatter.escape("⚠️ Something went wrong saving your log. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _log_weight(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log weight 74.2"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        kg = float(args[0])
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"kg": round(kg, 2)}
    await db.insert_log(user["id"], "weight", data)
    reply = formatter.format_log_confirmation("weight", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_steps(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log steps 8500"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        count = int(float(args[0]))
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"count": count}
    await db.insert_log(user["id"], "steps", data)
    reply = formatter.format_log_confirmation("steps", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_water(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log water 500"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        ml = int(float(args[0]))
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"ml": ml}
    await db.insert_log(user["id"], "water", data)
    reply = formatter.format_log_confirmation("water", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_workout(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log workout chest and triceps 45min"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    description = " ".join(args)
    data = {"description": description, "exercises": []}
    await db.insert_log(user["id"], "workout", data)
    reply = formatter.format_log_confirmation("workout", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /today
# ---------------------------------------------------------------------------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = await _ensure_registered(update)
        logs = await db.get_logs_for_user_today(user["id"])
        prev_weight = await db.get_last_weight_before_today(user["id"])
        reply = formatter.format_today_summary(user["name"], logs, prev_weight_kg=prev_weight)
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("Error in cmd_today for telegram_id=%s", update.effective_user.id)
        await update.message.reply_text(
            formatter.escape("⚠️ Could not retrieve today's summary. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
