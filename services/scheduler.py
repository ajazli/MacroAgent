"""
Scheduled jobs for the fitness bot.
Uses APScheduler's AsyncIOScheduler (same event loop as the bot).
Supports multiple groups — no GROUP_CHAT_ID env var required.
"""

import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None

# Per-group Clocker topic cache: {chat_id: topic_id or None}
_clocker_cache: dict[int, Optional[int]] = {}


def get_clocker_topic_id(chat_id: int) -> Optional[int]:
    return _clocker_cache.get(chat_id)


async def resolve_clocker_topic(bot, chat_id: int) -> Optional[int]:
    """Scan the group's forum topics for one named 'Clocker', cache and return its thread ID."""
    from services.db import set_group_clocker_topic
    try:
        result = await bot.get_forum_topics(chat_id=chat_id)
        for topic in result.topics:
            if topic.name.strip().lower() == "clocker":
                topic_id = topic.message_thread_id
                _clocker_cache[chat_id] = topic_id
                await set_group_clocker_topic(chat_id, topic_id)
                logger.info("Found 'Clocker' topic in %s: thread_id=%s", chat_id, topic_id)
                return topic_id
        logger.info("No 'Clocker' topic in %s — prompts go to General", chat_id)
        _clocker_cache[chat_id] = None
        await set_group_clocker_topic(chat_id, None)
        return None
    except Exception as exc:
        logger.warning("Could not resolve Clocker topic for %s: %s", chat_id, exc)
        return _clocker_cache.get(chat_id)  # Return cached value if available


async def _get_groups_with_topics(bot) -> list[tuple[int, Optional[int]]]:
    """Return [(chat_id, clocker_topic_id)] for all registered groups,
    resolving any that haven't been looked up yet."""
    from services.db import get_all_groups
    groups = await get_all_groups()
    result = []
    for g in groups:
        chat_id = g["chat_id"]
        if chat_id not in _clocker_cache:
            # Populate cache from DB first, then attempt live resolve
            _clocker_cache[chat_id] = g.get("clocker_topic_id")
            await resolve_clocker_topic(bot, chat_id)
        result.append((chat_id, _clocker_cache.get(chat_id)))
    return result


def _escape(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


async def daily_morning_prompt(bot) -> None:
    """8am SGT daily prompt for weight, sleep, energy, water — sent to all groups."""
    text = (
        "🌅 *Good morning\\!* Time to log your daily metrics:\n\n"
        "⚖️ `/weight` _e\\.g\\. /weight 74\\.2_\n"
        "😴 `/sleep` _e\\.g\\. /sleep 7\\.5_\n"
        "⚡ `/energy` _e\\.g\\. /energy 8_\n"
        "💧 `/water` _e\\.g\\. /water 500_"
    )
    for chat_id, clocker_topic_id in await _get_groups_with_topics(bot):
        kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
        if clocker_topic_id:
            kwargs["message_thread_id"] = clocker_topic_id
        try:
            await bot.send_message(**kwargs)
        except Exception as exc:
            logger.error("Failed to send morning prompt to %s: %s", chat_id, exc)


async def weekly_checkin_trigger(bot) -> None:
    """10am SGT — auto-schedule recurring check-ins and ping users due today."""
    from datetime import timedelta
    from services.db import (
        get_check_ins_for_date, mark_check_in_prompted,
        get_all_weekly_schedules, schedule_check_in,
    )
    from services.tz import today_sgt

    today = today_sgt()

    # Auto-schedule next occurrence for all indefinite recurring users
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

    groups = await _get_groups_with_topics(bot)

    for entry in due:
        name = entry["name"]
        mention = _escape(f"@{name}" if not name.startswith("@") else name)
        text = (
            f"📋 *Weekly Check\\-In Reminder*\n\n"
            f"Hey {mention}\\, it's your check\\-in day\\!\n"
            f"Use `/checkin` to complete your weekly assessment\\."
        )
        for chat_id, clocker_topic_id in groups:
            kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
            if clocker_topic_id:
                kwargs["message_thread_id"] = clocker_topic_id
            try:
                await bot.send_message(**kwargs)
            except Exception as exc:
                logger.error("Failed to send check-in prompt for %s in %s: %s", name, chat_id, exc)
        try:
            await mark_check_in_prompted(entry["user_id"], entry["scheduled_date"])
        except Exception as exc:
            logger.error("Failed to mark check-in prompted for %s: %s", name, exc)


def start_scheduler(bot) -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Asia/Singapore")

    _scheduler.add_job(
        daily_morning_prompt,
        "cron", hour=8, minute=0,
        args=[bot],
        id="daily_morning_prompt",
        replace_existing=True,
    )
    _scheduler.add_job(
        weekly_checkin_trigger,
        "cron", hour=10, minute=0,
        args=[bot],
        id="weekly_checkin_trigger",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Scheduler started (Asia/Singapore timezone) — multi-group mode")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
