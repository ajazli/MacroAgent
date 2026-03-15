"""
Async database service using asyncpg.
Provides a single shared connection pool and CRUD helpers.
"""

import logging
import os
from datetime import date
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
