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

async def get_or_create_user(
    telegram_id: int, name: str, username: Optional[str] = None
) -> dict:
    """Return the user row, creating it if it doesn't exist.

    username is the Telegram @handle when the person has one, kept apart from
    name so callers can tell a real mention from a first name.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO users (telegram_id, name, username) VALUES ($1, $2, $3) "
                "ON CONFLICT (telegram_id) DO UPDATE "
                "SET username = COALESCE(EXCLUDED.username, users.username) "
                "RETURNING id, telegram_id, name, username, approved, revoked_at, is_owner, "
                "          daily_calorie_target, created_at",
                telegram_id, name, (username or "").lstrip("@").lower() or None,
            )
            return dict(row)
    except Exception:
        logger.exception("get_or_create_user failed for telegram_id=%s", telegram_id)
        raise


async def get_or_create_user_from_tg(tg_user) -> dict:
    """Upsert from a Telegram user object.

    One place decides how a Telegram account becomes a row, so the handlers stop
    each deriving the display name slightly differently.
    """
    username = (tg_user.username or "").strip() or None
    name = (username or tg_user.first_name or "user").lower()
    return await get_or_create_user(tg_user.id, name, username=username)


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


async def get_leaderboard_data() -> list[dict]:
    """Today's calories for every user, across all groups.

    Deliberately not scoped by chat: the leaderboard is meant to rank everyone
    the bot knows, so the same board shows the same people wherever it is run.
    Only users who have actually logged a meal today appear — a global list of
    everyone who has not would be noise.
    """
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.id, u.name, u.username, u.daily_calorie_target, l.data "
                "FROM logs l "
                "JOIN users u ON u.id = l.user_id "
                "WHERE l.date = $1 AND l.type = 'meal'",
                today,
            )

        stats: dict = {}
        for r in rows:
            uid = r["id"]
            if uid not in stats:
                stats[uid] = {
                    "name": r["name"],
                    "username": r["username"],
                    "target": r["daily_calorie_target"],
                    "calories": 0,
                    "meals": 0,
                }
            data = r["data"]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                continue
            stats[uid]["calories"] += int(data.get("calories", 0) or 0)
            stats[uid]["meals"] += 1

        return sorted(
            stats.values(),
            key=lambda s: (-s["calories"], s["name"].lower()),
        )
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


async def save_log_message(log_id: int, chat_id: int, message_id: int) -> None:
    """Record which Telegram message corresponds to a log entry (for corrections)."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO log_messages (log_id, chat_id, message_id) VALUES ($1, $2, $3) "
                "ON CONFLICT (chat_id, message_id) DO UPDATE SET log_id = EXCLUDED.log_id",
                log_id, chat_id, message_id,
            )
    except Exception:
        logger.exception("save_log_message failed for log_id=%s", log_id)


async def get_log_by_message(chat_id: int, message_id: int) -> Optional[dict]:
    """Return the log row linked to a specific bot message, or None."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT l.id, l.user_id, l.date, l.type, l.data, l.created_at, "
                "       u.name AS owner_name, u.username AS owner_username "
                "FROM log_messages lm "
                "JOIN logs l ON l.id = lm.log_id "
                "JOIN users u ON u.id = l.user_id "
                "WHERE lm.chat_id = $1 AND lm.message_id = $2",
                chat_id, message_id,
            )
            return dict(row) if row else None
    except Exception:
        logger.exception("get_log_by_message failed for chat_id=%s msg=%s", chat_id, message_id)
        return None


async def update_log_data(log_id: int, data: dict) -> None:
    """Replace the JSONB data payload of an existing log entry."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE logs SET data = $1::jsonb WHERE id = $2",
                json.dumps(data), log_id,
            )
    except Exception:
        logger.exception("update_log_data failed for log_id=%s", log_id)
        raise


async def record_group_member(
    chat_id: int, user_id: int, display_name: Optional[str] = None
) -> None:
    """Note that a user belongs to a group chat.

    Group members can ask about each other's logged data, so this roster is the
    boundary that keeps that inside one group — every lookup filters on chat_id.
    display_name is how Telegram shows them, which is how the others refer to them.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO group_members (chat_id, user_id, display_name) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (chat_id, user_id) DO UPDATE "
                "SET last_seen = NOW(), "
                "    display_name = COALESCE(EXCLUDED.display_name, group_members.display_name)",
                chat_id, user_id, display_name,
            )
    except Exception:
        logger.exception("record_group_member failed for chat=%s user=%s", chat_id, user_id)


async def get_group_members(chat_id: int) -> list[dict]:
    """Return every user known to belong to this chat: {id, name, telegram_id}."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.id, u.name, u.telegram_id, u.username, gm.display_name "
                "FROM group_members gm JOIN users u ON u.id = gm.user_id "
                "WHERE gm.chat_id = $1 ORDER BY u.name",
                chat_id,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_group_members failed for chat=%s", chat_id)
        return []


async def get_group_logs_today(chat_id: int) -> list[dict]:
    """Today's logs for every member of this chat, with the owner's name attached.

    Scoped by the group_members roster, so a user's data can only surface in a
    chat they are actually part of.
    """
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT l.id, l.user_id, u.name, l.date, l.type, l.data, l.created_at "
                "FROM group_members gm "
                "JOIN users u ON u.id = gm.user_id "
                "JOIN logs l ON l.user_id = gm.user_id "
                "WHERE gm.chat_id = $1 AND l.date = $2 "
                "ORDER BY u.name, l.created_at",
                chat_id, today,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_group_logs_today failed for chat=%s", chat_id)
        return []


async def get_group_access(chat_id: int) -> dict:
    """Access state for a group: {approved, revoked_at}.

    revoked_at distinguishes a group the owner deliberately blocked from one the
    bot has simply never been approved for — the first should stay silent, the
    second is worth a heads-up.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT approved, revoked_at FROM groups WHERE chat_id = $1", chat_id)
            if row is None:
                return {"approved": False, "revoked_at": None}
            return dict(row)
    except Exception:
        logger.exception("get_group_access failed for chat=%s", chat_id)
        # Fail open: a database blip must not lock everyone out of a working bot.
        return {"approved": True, "revoked_at": None}


async def set_group_approved(chat_id: int, approved: bool) -> bool:
    """Approve or revoke a group. Returns False if the group is unknown."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE groups SET approved = $2, "
                "       revoked_at = CASE WHEN $2 THEN NULL ELSE NOW() END "
                "WHERE chat_id = $1", chat_id, approved)
            return result.endswith(" 1")
    except Exception:
        logger.exception("set_group_approved failed for chat=%s", chat_id)
        return False


async def set_user_approved(telegram_id: int, approved: bool) -> bool:
    """Approve or revoke a user by Telegram id. Returns False if unknown."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET approved = $2, "
                "       revoked_at = CASE WHEN $2 THEN NULL ELSE NOW() END "
                "WHERE telegram_id = $1", telegram_id, approved)
            return result.endswith(" 1")
    except Exception:
        logger.exception("set_user_approved failed for telegram_id=%s", telegram_id)
        return False


async def get_user_by_handle_or_id(token: str) -> Optional[dict]:
    """Look a user up by @handle, stored name, or numeric Telegram id."""
    pool = get_pool()
    cleaned = token.strip().lstrip("@").lower()
    try:
        async with pool.acquire() as conn:
            if cleaned.isdigit():
                row = await conn.fetchrow(
                    "SELECT id, telegram_id, name, username, approved, revoked_at, is_owner FROM users "
                    "WHERE telegram_id = $1", int(cleaned))
                if row:
                    return dict(row)
            row = await conn.fetchrow(
                "SELECT id, telegram_id, name, username, approved, revoked_at, is_owner FROM users "
                "WHERE lower(username) = $1 OR lower(name) = $1", cleaned)
            return dict(row) if row else None
    except Exception:
        logger.exception("get_user_by_handle_or_id failed for %s", token)
        return None


# ---------------------------------------------------------------------------
# Daily calorie targets
# ---------------------------------------------------------------------------

async def set_calorie_target(user_id: int, target: Optional[int]) -> bool:
    """Set a client's daily calorie target, or clear it with None."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE users SET daily_calorie_target = $2 WHERE id = $1", user_id, target)
            return result.endswith(" 1")
    except Exception:
        logger.exception("set_calorie_target failed for user_id=%s", user_id)
        return False


async def get_calorie_target(user_id: int) -> Optional[int]:
    """A client's daily calorie target, or None if the trainer hasn't set one."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT daily_calorie_target FROM users WHERE id = $1", user_id)
    except Exception:
        logger.exception("get_calorie_target failed for user_id=%s", user_id)
        return None


async def get_group_targets(chat_id: int) -> list[dict]:
    """Every member of a group with their target, for the trainer's overview."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT u.id, u.name, u.username, gm.display_name, u.daily_calorie_target "
                "FROM group_members gm JOIN users u ON u.id = gm.user_id "
                "WHERE gm.chat_id = $1 ORDER BY lower(u.name)", chat_id)
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_group_targets failed for chat=%s", chat_id)
        return []


# ---------------------------------------------------------------------------
# PT session balances
# ---------------------------------------------------------------------------

async def add_pt_entry(
    chat_id: int, user_id: int, delta: int,
    note: Optional[str] = None, created_by: Optional[int] = None,
) -> bool:
    """Record one ledger entry: +N for a package sold, -1 for a session used."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO pt_sessions (chat_id, user_id, delta, note, created_by) "
                "VALUES ($1, $2, $3, $4, $5)",
                chat_id, user_id, delta, note, created_by,
            )
            return True
    except Exception:
        logger.exception("add_pt_entry failed for chat=%s user=%s", chat_id, user_id)
        return False


_PT_BALANCE_SELECT = (
    "SELECT u.id AS user_id, u.name, u.username, gm.display_name, "
    "       COALESCE(SUM(p.delta), 0) AS remaining, "
    "       COALESCE(SUM(CASE WHEN p.delta > 0 THEN p.delta ELSE 0 END), 0) AS purchased, "
    "       COALESCE(-SUM(CASE WHEN p.delta < 0 THEN p.delta ELSE 0 END), 0) AS used, "
    "       MAX(CASE WHEN p.delta < 0 THEN p.created_at END) AS last_session "
    "FROM pt_sessions p "
    "JOIN users u ON u.id = p.user_id "
    "LEFT JOIN group_members gm ON gm.chat_id = p.chat_id AND gm.user_id = p.user_id "
    "WHERE p.chat_id = $1 "
)


async def get_pt_balances(chat_id: int, user_id: Optional[int] = None) -> list[dict]:
    """Session balances for a group, or for one client in it."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            if user_id is None:
                rows = await conn.fetch(
                    _PT_BALANCE_SELECT +
                    "GROUP BY u.id, u.name, u.username, gm.display_name "
                    "ORDER BY lower(u.name)", chat_id)
            else:
                rows = await conn.fetch(
                    _PT_BALANCE_SELECT + "AND p.user_id = $2 "
                    "GROUP BY u.id, u.name, u.username, gm.display_name", chat_id, user_id)
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_pt_balances failed for chat=%s", chat_id)
        return []


async def delete_last_pt_entry(chat_id: int, user_id: int) -> Optional[dict]:
    """Remove the newest ledger entry for a client. Returns it, or None if empty."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM pt_sessions WHERE id = ("
                "  SELECT id FROM pt_sessions WHERE chat_id = $1 AND user_id = $2 "
                "  ORDER BY created_at DESC, id DESC LIMIT 1"
                ") RETURNING delta, note, created_at",
                chat_id, user_id)
            return dict(row) if row else None
    except Exception:
        logger.exception("delete_last_pt_entry failed for chat=%s user=%s", chat_id, user_id)
        return None


async def get_owner_telegram_ids() -> list[int]:
    """Telegram ids of everyone promoted to owner in the database."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT telegram_id FROM users WHERE is_owner = TRUE")
            return [r["telegram_id"] for r in rows]
    except Exception:
        logger.exception("get_owner_telegram_ids failed")
        return []


async def set_user_owner(telegram_id: int, is_owner: bool) -> bool:
    """Promote or demote an owner. Promotion also approves them, since an owner
    who is still awaiting approval would be a contradiction."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            if is_owner:
                result = await conn.execute(
                    "UPDATE users SET is_owner = TRUE, approved = TRUE, revoked_at = NULL "
                    "WHERE telegram_id = $1", telegram_id)
            else:
                result = await conn.execute(
                    "UPDATE users SET is_owner = FALSE WHERE telegram_id = $1", telegram_id)
            return result.endswith(" 1")
    except Exception:
        logger.exception("set_user_owner failed for telegram_id=%s", telegram_id)
        return False


async def get_owners() -> list[dict]:
    """Owner rows for display."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT telegram_id, name, username FROM users "
                "WHERE is_owner = TRUE ORDER BY lower(name)")
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_owners failed")
        return []


async def get_access_overview() -> tuple[list[dict], list[dict]]:
    """Return (groups, unapproved_users) for the owner's access listing."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            groups = await conn.fetch(
                "SELECT chat_id, title, approved FROM groups "
                "ORDER BY approved DESC, lower(title)")
            users = await conn.fetch(
                "SELECT telegram_id, name, username FROM users "
                "WHERE approved = FALSE ORDER BY lower(name)")
            return [dict(r) for r in groups], [dict(r) for r in users]
    except Exception:
        logger.exception("get_access_overview failed")
        return [], []


async def get_all_users_meals_today() -> list[dict]:
    """Return today's meal logs joined with user info and the chat_id they were logged in.

    log_messages is keyed by (chat_id, message_id), so one meal accumulates a row
    per bot message that represents it — the original analysis plus one for every
    correction. Joining it naively returns that meal once per message and the
    summary counts it several times over, so DISTINCT ON collapses back to one
    row per log before the totals are computed.
    """
    pool = get_pool()
    today = today_sgt()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM ("
                "  SELECT DISTINCT ON (l.id) "
                "         l.id, l.user_id, u.name, u.username, u.telegram_id, l.date, l.type, "
                "         l.data, l.created_at, lm.chat_id "
                "  FROM logs l "
                "  JOIN users u ON u.id = l.user_id "
                "  JOIN log_messages lm ON lm.log_id = l.id "
                "  WHERE l.date = $1 AND l.type = 'meal' "
                "  ORDER BY l.id, lm.message_id"
                ") m ORDER BY m.name, m.created_at",
                today,
            )
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_users_meals_today failed")
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
