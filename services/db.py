"""
Async database service using asyncpg.
Provides a single shared connection pool and CRUD helpers.
"""

import logging
import os
from datetime import date, timedelta
from typing import Optional

import json

from services.tz import today_sgt

import asyncpg

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> None:
    """Initialize the shared asyncpg connection pool and run migrations.
    Idempotent — safe to call multiple times; skips if already initialised."""
    global _pool
    if _pool is not None:
        logger.debug("init_pool called but pool already exists — skipping.")
        return

    database_url = os.environ["DATABASE_URL"]

    async def _init_conn(conn):
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    logger.info("Creating asyncpg connection pool…")
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10, init=_init_conn)
    logger.info("Connection pool created — running migrations…")
    await _run_migrations()
    logger.info("Database pool initialised.")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first.")
    return _pool


async def _run_migrations() -> None:
    """Apply all SQL migrations in order."""
    migrations_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "migrations"))
    migration_files = sorted(
        f for f in os.listdir(migrations_dir)
        if f.endswith(".sql") and not f.startswith(".")
    )
    async with get_pool().acquire() as conn:
        for filename in migration_files:
            path = os.path.join(migrations_dir, filename)
            with open(path, "r") as f:
                sql = f.read()
            await conn.execute(sql)
            logger.info("Applied migration: %s", filename)
    logger.info("All migrations applied.")


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int, name: str) -> dict:
    """Return the user row, creating it if it doesn't exist."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, telegram_id, name, created_at FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            if row is None:
                row = await conn.fetchrow(
                    "INSERT INTO users (telegram_id, name) VALUES ($1, $2) "
                    "ON CONFLICT (telegram_id) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING id, telegram_id, name, created_at",
                    telegram_id,
                    name,
                )
            return dict(row)
    except Exception:
        logger.exception("get_or_create_user failed for telegram_id=%s", telegram_id)
        raise


async def get_user_by_telegram_id(telegram_id: int) -> Optional[dict]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, telegram_id, name, created_at FROM users WHERE telegram_id = $1",
                telegram_id,
            )
            return dict(row) if row else None
    except Exception:
        logger.exception("get_user_by_telegram_id failed for telegram_id=%s", telegram_id)
        raise


async def get_user_by_username(username: str) -> Optional[dict]:
    """
    Attempt to find a user whose stored name matches the given username
    (case-insensitive, with or without leading @).
    """
    pool = get_pool()
    clean = username.lstrip("@").lower()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, telegram_id, name, created_at FROM users WHERE LOWER(name) = $1",
                clean,
            )
            return dict(row) if row else None
    except Exception:
        logger.exception("get_user_by_username failed for username=%s", username)
        raise


async def get_all_users() -> list[dict]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, telegram_id, name, created_at FROM users ORDER BY name")
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_users failed")
        raise


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

async def insert_log(user_id: int, log_type: str, data: dict, log_date: Optional[date] = None) -> dict:
    pool = get_pool()
    log_date = log_date or today_sgt()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO logs (user_id, date, type, data) VALUES ($1, $2, $3, $4::jsonb) "
                "RETURNING id, user_id, date, type, data, created_at",
                user_id,
                log_date,
                log_type,
                json.dumps(data),
            )
            return dict(row)
    except Exception:
        logger.exception("insert_log failed for user_id=%s type=%s", user_id, log_type)
        raise


async def get_logs_for_user_today(user_id: int, log_type: Optional[str] = None) -> list[dict]:
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            if log_type:
                rows = await conn.fetch(
                    "SELECT id, user_id, date, type, data, created_at FROM logs "
                    "WHERE user_id = $1 AND date = $2 AND type = $3 ORDER BY created_at",
                    user_id, today, log_type,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, user_id, date, type, data, created_at FROM logs "
                    "WHERE user_id = $1 AND date = $2 ORDER BY created_at",
                    user_id, today,
                )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_logs_for_user_today failed for user_id=%s", user_id)
        raise


async def get_logs_for_user_date_range(
    user_id: int,
    start: date,
    end: date,
    log_type: Optional[str] = None,
) -> list[dict]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            if log_type:
                rows = await conn.fetch(
                    "SELECT id, user_id, date, type, data, created_at FROM logs "
                    "WHERE user_id = $1 AND date BETWEEN $2 AND $3 AND type = $4 ORDER BY date, created_at",
                    user_id, start, end, log_type,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, user_id, date, type, data, created_at FROM logs "
                    "WHERE user_id = $1 AND date BETWEEN $2 AND $3 ORDER BY date, created_at",
                    user_id, start, end,
                )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_logs_for_user_date_range failed for user_id=%s", user_id)
        raise


async def get_all_users_logs_today() -> list[dict]:
    """Return all logs for today joined with user info."""
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT l.id, l.user_id, u.name, u.telegram_id, l.date, l.type, l.data, l.created_at "
                "FROM logs l JOIN users u ON u.id = l.user_id "
                "WHERE l.date = $1 ORDER BY u.name, l.created_at",
                today,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_users_logs_today failed")
        raise


async def get_last_weight_before_today(user_id: int) -> "Optional[float]":
    """Return the most recent weight (kg) logged before today, or None."""
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM logs "
                "WHERE user_id = $1 AND type = 'weight' AND date < $2 "
                "ORDER BY date DESC, created_at DESC LIMIT 1",
                user_id, today,
            )
            if row is None:
                return None
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            return data.get("kg") if isinstance(data, dict) else None
    except Exception:
        logger.exception("get_last_weight_before_today failed for user_id=%s", user_id)
        return None


async def get_weight_logs_for_user(user_id: int, days: int = 7) -> list[dict]:
    """Return weight logs for the past N days, ordered oldest-first."""
    pool = get_pool()
    today = today_sgt()
    start = today - timedelta(days=days - 1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT date, data FROM logs "
                "WHERE user_id = $1 AND type = 'weight' AND date BETWEEN $2 AND $3 "
                "ORDER BY date ASC, created_at DESC",
                user_id, start, today,
            )
            results = []
            for r in rows:
                data = r["data"]
                if isinstance(data, str):
                    data = json.loads(data)
                if isinstance(data, dict) and data.get("kg") is not None:
                    results.append({"date": r["date"], "kg": float(data["kg"])})
            # Keep only the last entry per day
            seen = {}
            for entry in results:
                seen[entry["date"]] = entry["kg"]
            return [{"date": d, "kg": kg} for d, kg in sorted(seen.items())]
    except Exception:
        logger.exception("get_weight_logs_for_user failed for user_id=%s", user_id)
        return []


async def get_steps_logs_for_user(user_id: int, days: int = 7) -> list[dict]:
    """Return daily step totals for the past N days, ordered oldest-first."""
    pool = get_pool()
    today = today_sgt()
    start = today - timedelta(days=days - 1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT date, data FROM logs "
                "WHERE user_id = $1 AND type = 'steps' AND date BETWEEN $2 AND $3 "
                "ORDER BY date ASC, created_at ASC",
                user_id, start, today,
            )
            # Sum all step entries per day (handles multiple logs per day)
            daily: dict = {}
            for r in rows:
                data = r["data"]
                if isinstance(data, str):
                    data = json.loads(data)
                count = int(float(data.get("count", 0) or 0)) if isinstance(data, dict) else 0
                daily[r["date"]] = daily.get(r["date"], 0) + count
            return [{"date": d, "count": c} for d, c in sorted(daily.items())]
    except Exception:
        logger.exception("get_steps_logs_for_user failed for user_id=%s", user_id)
        return []


async def get_log_streak(user_id: int) -> int:
    """Return consecutive days ending today that have at least one log entry."""
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT date FROM logs WHERE user_id = $1 ORDER BY date DESC",
                user_id,
            )
            streak = 0
            expected = today
            for r in rows:
                if r["date"] == expected:
                    streak += 1
                    expected = expected - timedelta(days=1)
                elif r["date"] < expected:
                    break
            return streak
    except Exception:
        logger.exception("get_log_streak failed for user_id=%s", user_id)
        return 0


async def get_leaderboard_data(days: int = 7) -> list[dict]:
    """Aggregate steps and calories per user for the past N days."""
    pool = get_pool()
    today = today_sgt()
    start = today - timedelta(days=days - 1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.name, l.type, l.data FROM logs l "
                "JOIN users u ON u.id = l.user_id "
                "WHERE l.date BETWEEN $1 AND $2 AND l.type IN ('steps', 'meal') "
                "ORDER BY u.name",
                start, today,
            )
        user_stats: dict = {}
        for r in rows:
            name = r["name"]
            if name not in user_stats:
                user_stats[name] = {"steps": 0, "calories": 0}
            data = r["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            if r["type"] == "steps":
                user_stats[name]["steps"] += int(float(data.get("count", 0) or 0))
            elif r["type"] == "meal":
                user_stats[name]["calories"] += int(data.get("calories", 0) or 0)
        return [{"name": n, **v} for n, v in sorted(user_stats.items())]
    except Exception:
        logger.exception("get_leaderboard_data failed")
        return []


# ---------------------------------------------------------------------------
# Check-in schedule helpers
# ---------------------------------------------------------------------------

async def get_check_ins_for_date(check_date: date) -> list[dict]:
    """Return all users with a pending check-in scheduled on check_date."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.id, c.user_id, c.scheduled_date, c.prompted_at, c.completed_at, "
                "u.name, u.telegram_id FROM check_in_schedules c "
                "JOIN users u ON u.id = c.user_id "
                "WHERE c.scheduled_date = $1 AND c.completed_at IS NULL",
                check_date,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_check_ins_for_date failed")
        return []


async def schedule_check_in(user_id: int, scheduled_date: date) -> dict:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO check_in_schedules (user_id, scheduled_date) VALUES ($1, $2) "
                "ON CONFLICT (user_id, scheduled_date) DO UPDATE SET "
                "prompted_at = NULL, completed_at = NULL, created_at = NOW() "
                "RETURNING *",
                user_id, scheduled_date,
            )
            return dict(row)
    except Exception:
        logger.exception("schedule_check_in failed")
        raise


async def mark_check_in_prompted(user_id: int, scheduled_date: date) -> None:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE check_in_schedules SET prompted_at = NOW() "
                "WHERE user_id = $1 AND scheduled_date = $2",
                user_id, scheduled_date,
            )
    except Exception:
        logger.exception("mark_check_in_prompted failed")


async def mark_check_in_completed(user_id: int, scheduled_date: date) -> None:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE check_in_schedules SET completed_at = NOW() "
                "WHERE user_id = $1 AND scheduled_date = $2",
                user_id, scheduled_date,
            )
    except Exception:
        logger.exception("mark_check_in_completed failed")


async def get_check_in_schedule(user_id: int) -> Optional[dict]:
    """Return the next pending (incomplete) check-in for a user."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT c.*, u.name, u.telegram_id FROM check_in_schedules c "
                "JOIN users u ON u.id = c.user_id "
                "WHERE c.user_id = $1 AND c.completed_at IS NULL "
                "ORDER BY c.scheduled_date ASC LIMIT 1",
                user_id,
            )
            return dict(row) if row else None
    except Exception:
        logger.exception("get_check_in_schedule failed")
        return None


async def get_all_check_in_schedules() -> list[dict]:
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.*, u.name, u.telegram_id FROM check_in_schedules c "
                "JOIN users u ON u.id = c.user_id "
                "ORDER BY c.scheduled_date ASC, u.name ASC",
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_check_in_schedules failed")
        return []


async def delete_check_in_schedule(user_id: int) -> bool:
    """Delete all pending check-ins for a user. Returns True if any were deleted."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM check_in_schedules WHERE user_id = $1 AND completed_at IS NULL",
                user_id,
            )
            return result != "DELETE 0"
    except Exception:
        logger.exception("delete_check_in_schedule failed")
        return False


# ---------------------------------------------------------------------------
# Group registry helpers
# ---------------------------------------------------------------------------

async def register_group(chat_id: int, title: str) -> None:
    """Upsert a group the bot is active in (title may change over time)."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO groups (chat_id, title) VALUES ($1, $2) "
                "ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title",
                chat_id, title,
            )
    except Exception:
        logger.exception("register_group failed for chat_id=%s", chat_id)


async def get_all_groups() -> list[dict]:
    """Return all registered groups."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT chat_id, title, clocker_topic_id FROM groups ORDER BY registered_at")
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_groups failed")
        return []


async def set_group_clocker_topic(chat_id: int, clocker_topic_id: Optional[int]) -> None:
    """Cache the resolved Clocker topic ID for a group."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE groups SET clocker_topic_id = $1 WHERE chat_id = $2",
                clocker_topic_id, chat_id,
            )
    except Exception:
        logger.exception("set_group_clocker_topic failed for chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# Indefinite weekly schedule helpers
# ---------------------------------------------------------------------------

async def set_weekly_schedule(user_id: int, day_of_week: int) -> None:
    """Upsert a user's indefinite weekly check-in day (0=Mon … 6=Sun)."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_weekly_schedules (user_id, day_of_week) VALUES ($1, $2) "
                "ON CONFLICT (user_id) DO UPDATE SET day_of_week = EXCLUDED.day_of_week, created_at = NOW()",
                user_id, day_of_week,
            )
    except Exception:
        logger.exception("set_weekly_schedule failed for user_id=%s", user_id)
        raise


async def remove_weekly_schedule(user_id: int) -> bool:
    """Remove a user's indefinite weekly schedule. Returns True if one existed."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM user_weekly_schedules WHERE user_id = $1", user_id
            )
            return result != "DELETE 0"
    except Exception:
        logger.exception("remove_weekly_schedule failed for user_id=%s", user_id)
        return False


async def get_all_weekly_schedules() -> list[dict]:
    """Return all users with an indefinite weekly schedule."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT w.user_id, w.day_of_week, u.name, u.telegram_id "
                "FROM user_weekly_schedules w JOIN users u ON u.id = w.user_id "
                "ORDER BY u.name",
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_weekly_schedules failed")
        return []


async def get_all_users_logs_date_range(start: date, end: date) -> list[dict]:
    """Return all logs for all users in date range joined with user info."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT l.id, l.user_id, u.name, u.telegram_id, l.date, l.type, l.data, l.created_at "
                "FROM logs l JOIN users u ON u.id = l.user_id "
                "WHERE l.date BETWEEN $1 AND $2 ORDER BY u.name, l.date, l.created_at",
                start, end,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_users_logs_date_range failed")
        raise
