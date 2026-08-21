"""
Scheduled jobs for the bot.
Uses APScheduler's AsyncIOScheduler (same event loop as the bot).
Supports multiple groups — no GROUP_CHAT_ID env var required.

Jobs (all Asia/Singapore):
  23:00 daily  — daily_nutrition_summary, per-group meal recap
  10:00 daily  — weekly_checkin_trigger, pings only users with a check-in due

The 08:00 "log your daily metrics" prompt was removed: the bot is focused on
meal analysis, and it nagged every group daily for weight/sleep/energy/water.
The /weight, /sleep, /energy, /water commands still work for anyone who wants
them — nothing asks for them unprompted any more.
"""

import logging
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

_SGT = ZoneInfo("Asia/Singapore")

_scheduler: Optional[AsyncIOScheduler] = None

# Per-group Clocker topic cache: {chat_id: topic_id or None}
_clocker_cache: dict[int, Optional[int]] = {}


def get_clocker_topic_id(chat_id: int) -> Optional[int]:
    return _clocker_cache.get(chat_id)


async def resolve_clocker_topic(bot, chat_id: int) -> Optional[int]:
    """Scan the group's forum topics for one named 'Clocker', cache and return its thread ID."""
    from services.db import set_group_clocker_topic

    # The Bot API exposes no method to list a forum's topics, so on python-telegram-bot
    # there is nothing to call. Fall back to whatever is cached from the DB; with no
    # thread id, messages land in the group's General topic, which is fine.
    if not hasattr(bot, "get_forum_topics"):
        _clocker_cache.setdefault(chat_id, None)
        return _clocker_cache.get(chat_id)

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


async def daily_nutrition_summary(bot) -> None:
    """23:00 SGT — send each group only the meals logged in that group's chat."""
    from services.db import get_all_users_meals_today
    from services import formatter

    logger.info("daily_nutrition_summary: job fired")

    try:
        all_rows = await get_all_users_meals_today()
    except Exception as exc:
        logger.error("daily_nutrition_summary: failed to fetch meals: %s", exc)
        return

    logger.info("daily_nutrition_summary: fetched %d meal rows", len(all_rows))

    groups = await _get_groups_with_topics(bot)
    logger.info("daily_nutrition_summary: sending to %d group(s)", len(groups))

    for chat_id, clocker_topic_id in groups:
        group_rows = [r for r in all_rows if r.get("chat_id") == chat_id]
        if not group_rows:
            logger.info("daily_nutrition_summary: no meals for chat %s — skipping", chat_id)
            continue

        text = formatter.format_daily_nutrition_summary(group_rows)
        kwargs = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"}
        if clocker_topic_id:
            kwargs["message_thread_id"] = clocker_topic_id
        try:
            await bot.send_message(**kwargs)
            logger.info("daily_nutrition_summary: sent to chat %s", chat_id)
        except Exception as exc:
            logger.error("daily_nutrition_summary: failed to send to %s: %s", chat_id, exc)


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
    if _scheduler is not None and _scheduler.running:
        logger.info("Scheduler already running — not starting a second one")
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone=_SGT)

    _scheduler.add_job(
        weekly_checkin_trigger,
        "cron", hour=10, minute=0,
        args=[bot],
        id="weekly_checkin_trigger",
        replace_existing=True,
    )
    _scheduler.add_job(
        daily_nutrition_summary,
        "cron", hour=23, minute=0,
        args=[bot],
        id="daily_nutrition_summary",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Scheduler started (Asia/Singapore timezone) — multi-group mode")
    for job in _scheduler.get_jobs():
        logger.info("  scheduled job %-24s next run: %s", job.id, job.next_run_time)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler stopped")
