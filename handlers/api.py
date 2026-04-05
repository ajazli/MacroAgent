"""
HTTP API for iOS Shortcuts / external integrations.

POST /api/log  — log any data type (steps, weight, water, workout) for a user.
GET  /health   — health check.

Authentication: Authorization: Bearer <BOT_API_SECRET>
"""

import logging
import os

from aiohttp import web
from telegram.constants import ParseMode

from services import db, formatter

logger = logging.getLogger(__name__)


async def get_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def post_log(request: web.Request) -> web.Response:
    secret = os.environ.get("BOT_API_SECRET", "")
    if not secret:
        return web.json_response({"error": "BOT_API_SECRET not configured"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    # Accept auth from header OR body (iOS Shortcuts can inject invisible chars in headers)
    auth_header = request.headers.get("Authorization", "")
    auth_body = body.get("secret", "")
    if auth_header != f"Bearer {secret}" and auth_body != secret:
        return web.json_response({"error": "Unauthorized"}, status=401)

    telegram_id = body.get("telegram_id")
    log_type = body.get("type")
    data = body.get("data")

    # Sanitise values that iOS Shortcuts may wrap in extra quotes
    if isinstance(log_type, str):
        log_type = log_type.strip().strip('"').strip("'")
    # If data arrived as a plain number, wrap it as {"count": n}
    if isinstance(data, (int, float)):
        data = {"count": int(data)}
    # If data arrived as a raw string, try to parse it
    elif isinstance(data, str):
        import json as _json
        data_str = data.strip().strip('"').strip("'")
        try:
            data = _json.loads(data_str)
        except Exception:
            try:
                data = _json.loads("{" + data_str + "}")
            except Exception:
                data = {"raw": data_str}
    # Ensure data is always a dict
    if not isinstance(data, dict):
        data = {"value": data}

    if not telegram_id or not log_type or not data:
        return web.json_response(
            {"error": "Required fields: telegram_id, type, data", "received": {"telegram_id": telegram_id, "type": log_type, "data": str(data)}}, status=400
        )

    try:
        user = await db.get_user_by_telegram_id(int(telegram_id))
        if user is None:
            return web.json_response(
                {"error": "User not found — send /start to the bot first"}, status=404
            )
        await db.insert_log(user["id"], log_type, data)
        logger.info("API log: user=%s type=%s data=%s", user["name"], log_type, data)

        # Send confirmation to all registered groups
        bot = request.app["bot"]
        if bot:
            from services.db import get_all_groups
            from services.scheduler import get_clocker_topic_id
            reply = formatter.format_log_confirmation(log_type, data)
            name = formatter.escape(user["name"])
            msg = f"📲 {reply} _\\(auto\\-logged for {name}\\)_"
            groups = await get_all_groups()
            for g in groups:
                send_kwargs = {
                    "chat_id": g["chat_id"],
                    "text": msg,
                    "parse_mode": ParseMode.MARKDOWN_V2,
                }
                # Prefer in-memory cache (populated at startup), fall back to DB value
                topic_id = get_clocker_topic_id(g["chat_id"]) or g.get("clocker_topic_id")
                if topic_id:
                    send_kwargs["message_thread_id"] = topic_id
                try:
                    await bot.send_message(**send_kwargs)
                except Exception:
                    logger.exception("Failed to send group confirmation to %s", g["chat_id"])

        return web.json_response({"ok": True, "user": user["name"], "type": log_type})
    except Exception as exc:
        logger.exception("API log failed for telegram_id=%s", telegram_id)
        return web.json_response({"error": str(exc)}, status=500)


def build_api_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/health", get_health)
    app.router.add_post("/api/log", post_log)
    return app
