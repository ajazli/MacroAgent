"""
FitBot — Telegram fitness tracking bot entry point.

Registers all handlers and starts the bot in:
  - Polling mode  (local dev — no WEBHOOK_URL env var)
  - Webhook mode  (Railway deployment — WEBHOOK_URL is set)
"""

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
    from handlers.commands import cmd_start, cmd_log, cmd_today, cmd_health
    from handlers.photo import handle_photo
    from handlers.instructor import cmd_stats, cmd_report, cmd_week, cmd_meals

    # -----------------------------------------------------------------------
    # Register command handlers
    # -----------------------------------------------------------------------
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("today", cmd_today))

    # Instructor commands
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("meals", cmd_meals))

    # Photo handler — catches all photos in private chats and groups
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    return app


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
        # Polling mode (local development)
        # ----------------------------------------------------------------
        logger.info("Starting in POLLING mode.")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
