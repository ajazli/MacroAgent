"""
Scheduled jobs for the fitness bot.
Uses APScheduler's AsyncIOScheduler (same event loop as the bot).
"""

import logging
import os
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_clocker_topic_id: Optional[int] = None


def get_clocker_topic_id() -> Optional[int]:
    return _clocker_topic_id


async def resolve_clocker_topic(bot, group_chat_id: int) -> Optional[int]:
    """Scan the group's forum topics for one named 'Clocker' and return its thread ID."""
    global _clocker_topic_id
    try:
        result = await bot.get_forum_topics(chat_id=group_chat_id)
        for topic in result.topics:
            if topic.name.strip().lower() == "clocker":
                _clocker_topic_id = topic.message_thread_id
                logger.info("Found 'Clocker' topic: message_thread_id=%s", _clocker_topic_id)
                return _clocker_topic_id
        logger.warning("No 'Clocker' topic found in group %s — prompts go to General", group_chat_id)
        _clocker_topic_id = None
        return None
    except Exception as exc:
        logger.warning("Could not resolve Clocker topic: %s", exc)
        _clocker_topic_id = None
        return None


def _escape(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


async def daily_morning_prompt(bot, group_chat_id: int, clocker_topic_id: Optional[int]) -> None:
    """8am SGT daily prompt for weight, sleep, energy, water."""
    text = (
        "🌅 *Good morning\\!* Time to log your daily metrics:\n\n"
        "⚖️ `/log weight` _e\\.g\\. /log weight 74\\.2_\n"
        "😴 `/sleep` _e\\.g\\. /sleep 7\\.5_\n"
        "⚡ `/energy` _e\\.g\\. /energy 8_\n"
        "💧 `/water` _e\\.g\\. /water 500_"
    )
    kwargs = {"chat_id": group_chat_id, "text": text, "parse_mode": "MarkdownV2"}
    if clocker_topic_id:
        kwargs["message_thread_id"] = clocker_topic_id
    try:
        await bot.send_message(**kwargs)
    except Exception as exc:
        logger.error("Failed to send daily morning prompt: %s", exc)


async def weekly_checkin_trigger(bot, group_chat_id: int, clocker_topic_id: Optional[int]) -> None:
    """10am SGT check — ping users whose weekly check-in is due today, and
    auto-schedule the next occurrence for users on an indefinite recurring plan."""
    from datetime import timedelta
    from services.db import (
        get_check_ins_for_date, mark_check_in_prompted,
        get_all_weekly_schedules, schedule_check_in,
    )
    from services.tz import today_sgt

    today = today_sgt()

    # Auto-schedule next week's entry for all indefinite users who don't
    # already have one queued up for their next occurrence.
    try:
        weekly = await get_all_weekly_schedules()
        for w in weekly:
            days_ahead = (w["day_of_week"] - today.weekday()) % 7 or 7
            next_date = today + timedelta(days=days_ahead)
            await schedule_check_in(w["user_id"], next_date)
    except Exception as exc:
        logger.warning("Could not auto-schedule weekly check-ins: %s", exc)

    due = await get_check_ins_for_date(today)
    if not due:
        return

    for entry in due:
        name = entry["name"]
        mention = _escape(f"@{name}" if not name.startswith("@") else name)
        text = (
            f"📋 *Weekly Check\\-In Reminder*\n\n"
            f"Hey {mention}\\, it's your check\\-in day\\!\n"
            f"Use `/checkin` to complete your weekly assessment\\."
        )
        kwargs = {"chat_id": group_chat_id, "text": text, "parse_mode": "MarkdownV2"}
        if clocker_topic_id:
            kwargs["message_thread_id"] = clocker_topic_id
        try:
            await bot.send_message(**kwargs)
            await mark_check_in_prompted(entry["user_id"], entry["scheduled_date"])
        except Exception as exc:
            logger.error("Failed to send check-in prompt for %s: %s", name, exc)


def start_scheduler(bot, group_chat_id: int, clocker_topic_id: Optional[int]) -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Singapore")

    _scheduler.add_job(
        daily_morning_prompt,
        "cron", hour=8, minute=0,
        args=[bot, group_chat_id, clocker_topic_id],
        id="daily_morning_prompt",
        replace_existing=True,
    )
    _scheduler.add_job(
        weekly_checkin_trigger,
        "cron", hour=10, minute=0,
        args=[bot, group_chat_id, clocker_topic_id],
        id="weekly_checkin_trigger",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Scheduler started (Asia/Singapore timezone)")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
