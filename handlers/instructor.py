"""
Instructor-only command handlers: /stats, /report, /week, /meals
Access is gated by INSTRUCTOR_TELEGRAM_ID environment variable.
"""

import logging
import os
from collections import defaultdict
from datetime import date, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter

logger = logging.getLogger(__name__)

UNAUTHORIZED_MSG = formatter.escape("🚫 This command is only available to the instructor.")


def _is_instructor(telegram_id: int) -> bool:
    instructor_id = os.environ.get("INSTRUCTOR_TELEGRAM_ID", "")
    try:
        return int(instructor_id) == telegram_id
    except (ValueError, TypeError):
        return False


def _guard(func):
    """Decorator that blocks non-instructors."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_instructor(update.effective_user.id):
            await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=ParseMode.MARKDOWN_V2)
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Resolve target user(s) from command args
# ---------------------------------------------------------------------------

async def _resolve_target(args: list[str]) -> tuple[list[dict], str | None]:
    """
    Returns (list_of_user_dicts, error_message).
    If no args, returns all users.
    If @username or username given, returns that single user (or error).
    """
    if not args:
        users = await db.get_all_users()
        return users, None

    raw = args[0].lstrip("@").lower()
    user = await db.get_user_by_username(raw)
    if user is None:
        return [], formatter.escape(f"User '@{raw}' not found.")
    return [user], None


# ---------------------------------------------------------------------------
# /stats — today's totals (one user or all)
# ---------------------------------------------------------------------------

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        users, err = await _resolve_target(context.args or [])
        if err:
            await update.message.reply_text(err, parse_mode=ParseMode.MARKDOWN_V2)
            return

        if not users:
            await update.message.reply_text(
                formatter.escape("No users registered yet."), parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        # Collect today's logs for each user
        users_logs: dict[str, list] = {}
        for u in users:
            logs = await db.get_logs_for_user_today(u["id"])
            users_logs[u["name"]] = logs

        reply = formatter.format_stats_today(users_logs)
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("Error in cmd_stats")
        await update.message.reply_text(
            formatter.escape("⚠️ Could not retrieve stats."), parse_mode=ParseMode.MARKDOWN_V2
        )


# ---------------------------------------------------------------------------
# /report / /week — 7-day summary (one user or all)
# ---------------------------------------------------------------------------

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        users, err = await _resolve_target(context.args or [])
        if err:
            await update.message.reply_text(err, parse_mode=ParseMode.MARKDOWN_V2)
            return

        if not users:
            await update.message.reply_text(
                formatter.escape("No users registered yet."), parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        today = date.today()
        start = today - timedelta(days=6)

        parts = []
        for u in users:
            logs = await db.get_logs_for_user_date_range(u["id"], start, today)
            parts.append(formatter.format_report(u["name"], logs, days=7))

        # Send as separate messages if multiple users (avoids hitting message length limits)
        if len(parts) == 1:
            await update.message.reply_text(parts[0], parse_mode=ParseMode.MARKDOWN_V2)
        else:
            for part in parts:
                await update.message.reply_text(part, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("Error in cmd_report")
        await update.message.reply_text(
            formatter.escape("⚠️ Could not generate report."), parse_mode=ParseMode.MARKDOWN_V2
        )


# /week is an alias
cmd_week = cmd_report


# ---------------------------------------------------------------------------
# /meals — today's meal entries for a user
# ---------------------------------------------------------------------------

async def cmd_meals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        users, err = await _resolve_target(context.args or [])
        if err:
            await update.message.reply_text(err, parse_mode=ParseMode.MARKDOWN_V2)
            return

        if not users:
            await update.message.reply_text(
                formatter.escape("No users registered yet."), parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        for u in users:
            meal_logs = await db.get_logs_for_user_today(u["id"], log_type="meal")
            reply = formatter.format_meals_today(u["name"], meal_logs)
            await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("Error in cmd_meals")
        await update.message.reply_text(
            formatter.escape("⚠️ Could not retrieve meal data."), parse_mode=ParseMode.MARKDOWN_V2
        )
