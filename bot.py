"""
FitBot — Telegram fitness tracking bot entry point.

Registers all handlers and starts the bot in:
  - Polling mode  (local dev — no WEBHOOK_URL env var)
  - Webhook mode  (Railway deployment — WEBHOOK_URL is set)
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

# Load .env for local development (no-op in production where env vars are injected)
load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Initialise the DB pool after the application is built."""
    from services.db import init_pool
    await init_pool()
    logger.info("DB pool ready.")


async def _post_shutdown(application: Application) -> None:
    """Close the DB pool on shutdown."""
    from services.db import close_pool
    await close_pool()
    logger.info("DB pool closed.")


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # -----------------------------------------------------------------------
    # Import handlers (late import keeps module-level side-effects minimal)
    # -----------------------------------------------------------------------
    from handlers.commands import cmd_start, cmd_log, cmd_today, cmd_health, cmd_weight_graph, cmd_weight_avg
    from handlers.photo import handle_photo
    from handlers.instructor import cmd_stats, cmd_report, cmd_week, cmd_meals
    from handlers.fitness import build_fitness_conversation

    # -----------------------------------------------------------------------
    # Register command handlers
    # -----------------------------------------------------------------------
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("weightgraph", cmd_weight_graph))
    app.add_handler(CommandHandler("weightavg", cmd_weight_avg))

    # Instructor commands
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("meals", cmd_meals))

    # Fitness exercise conversation (/pushups, /situps, /planks, /run, /jog)
    # Must be registered before the photo handler
    app.add_handler(build_fitness_conversation())

    # Photo handler — catches all photos in private chats and groups
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    return app


async def _run_polling_with_api(app: Application) -> None:
    """Run the bot in polling mode alongside the aiohttp API server."""
    from aiohttp import web
    from handlers.api import build_api_app
    from services.db import init_pool, close_pool

    # Initialise DB pool BEFORE starting the Application context so
    # the pool is guaranteed to exist when the first update arrives.
    await init_pool()

    api_app = build_api_app(app.bot)
    port = int(os.environ.get("PORT", 8080))

    try:
        async with app:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await app.start()

            runner = web.AppRunner(api_app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", port)
            await site.start()
            logger.info("API server listening on port %d", port)

            try:
                await asyncio.Event().wait()
            finally:
                await runner.cleanup()
                await app.updater.stop()
                await app.stop()
    finally:
        await close_pool()


def main() -> None:
    app = build_application()
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()

    if webhook_url:
        # ----------------------------------------------------------------
        # Webhook mode (Railway / production)
        # ----------------------------------------------------------------
        port = int(os.environ.get("PORT", 8443))
        webhook_path = "/webhook"
        full_webhook_url = f"{webhook_url}{webhook_path}"

        logger.info("Starting in WEBHOOK mode: %s (port %d)", full_webhook_url, port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # ----------------------------------------------------------------
        # Polling mode — runs bot + HTTP API server on PORT
        # ----------------------------------------------------------------
        logger.info("Starting in POLLING mode with API server.")
        asyncio.run(_run_polling_with_api(app))


if __name__ == "__main__":
    main()
