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
from telegram import ChatMemberUpdated
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
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
    """Initialise DB, start scheduler, and register bot commands."""
    from services.db import init_pool, get_all_groups
    from services.scheduler import resolve_clocker_topic, start_scheduler
    await init_pool()
    logger.info("DB pool ready.")

    # Start scheduler — discovers registered groups from DB at job runtime
    try:
        start_scheduler(application.bot)
        groups = await get_all_groups()
        for g in groups:
            await resolve_clocker_topic(application.bot, g["chat_id"])
        logger.info("Scheduler started for %d known group(s).", len(groups))
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)

    # Register command menu (shows up when users type / in Telegram)
    from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
    user_commands = [
        BotCommand("start",       "Welcome & command guide"),
        BotCommand("today",       "Today's summary"),
        BotCommand("weight",      "Log your weight (kg)"),
        BotCommand("steps",       "Log step count (auto-logged from iOS Health)"),
        BotCommand("sleep",       "Log sleep hours"),
        BotCommand("energy",      "Log energy level (1-10)"),
        BotCommand("water",       "Log water intake (ml)"),
        BotCommand("workout",     "Log a workout note"),
        BotCommand("myreport",    "Your 7-day nutrition & exercise report"),
        BotCommand("weightgraph", "Weight trend chart (past 7 days)"),
        BotCommand("weightavg",   "Weight average & stats"),
        BotCommand("stepsgraph",  "Steps chart (past 7 days)"),
        BotCommand("stepsavg",    "Steps average & stats"),
        BotCommand("leaderboard", "Weekly group rankings"),
        BotCommand("checkin",     "Start your weekly check-in"),
        BotCommand("pushups",     "Log push-ups"),
        BotCommand("situps",      "Log sit-ups"),
        BotCommand("planks",      "Log planks"),
        BotCommand("run",         "Log a run"),
        BotCommand("jog",         "Log a jog"),
        BotCommand("maxpushups",  "Log max push-up reps (PB)"),
        BotCommand("maxsitups",   "Log max sit-ups in 1 min (PB)"),
        BotCommand("pb24",        "Log 2.4km best time (PB)"),
        BotCommand("cancel",      "Cancel current input"),
        BotCommand("health",      "Bot health check"),
    ]
    try:
        # Register for all group chats (all members see the menu when they type /)
        await application.bot.set_my_commands(user_commands, scope=BotCommandScopeAllGroupChats())
        # Also register for private chats
        await application.bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
        logger.info("Bot command menu registered for groups and private chats.")
    except Exception as exc:
        logger.warning("Could not register bot commands: %s", exc)


async def _post_shutdown(application: Application) -> None:
    """Close DB pool and stop scheduler on shutdown."""
    from services.db import close_pool
    from services.scheduler import stop_scheduler
    await close_pool()
    stop_scheduler()
    logger.info("DB pool closed.")


async def _register_group(bot, chat) -> None:
    """Register a group in the DB and resolve its Clocker topic."""
    from services.db import register_group
    from services.scheduler import resolve_clocker_topic
    if chat.type not in ("group", "supergroup"):
        return
    await register_group(chat.id, chat.title or "")
    await resolve_clocker_topic(bot, chat.id)
    logger.info("Registered group: %s (%s)", chat.title, chat.id)


async def _on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when the bot's membership in a chat changes (added/removed)."""
    change: ChatMemberUpdated = update.my_chat_member
    new_status = change.new_chat_member.status
    if new_status in ("member", "administrator"):
        await _register_group(context.bot, change.chat)


async def _on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Passively register the group on any group update (commands or messages).
    Runs in handler group -1 so it never blocks command handling."""
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return
    from services.db import get_all_groups
    groups = await get_all_groups()
    known_ids = {g["chat_id"] for g in groups}
    if chat.id not in known_ids:
        await _register_group(context.bot, chat)


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
    from handlers.commands import (
        cmd_start, cmd_today, cmd_health,
        cmd_weight, cmd_weight_graph, cmd_weight_avg,
        cmd_steps, cmd_steps_graph, cmd_steps_avg,
        cmd_sleep, cmd_energy, cmd_water, cmd_workout,
        cmd_myreport, cmd_leaderboard,
    )
    from handlers.photo import handle_photo
    from handlers.instructor import (
        cmd_stats, cmd_report, cmd_week, cmd_meals,
        cmd_schedule, cmd_scheduleweekly, cmd_stopweekly, cmd_checkinstatus, cmd_clearschedule,
    )
    from handlers.fitness import build_fitness_conversation
    from handlers.checkin import build_checkin_conversation

    # -----------------------------------------------------------------------
    # Register command handlers
    # -----------------------------------------------------------------------
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("health",      cmd_health))
    app.add_handler(CommandHandler("today",       cmd_today))
    app.add_handler(CommandHandler("weight",      cmd_weight))
    app.add_handler(CommandHandler("steps",       cmd_steps))
    app.add_handler(CommandHandler("sleep",       cmd_sleep))
    app.add_handler(CommandHandler("energy",      cmd_energy))
    app.add_handler(CommandHandler("water",       cmd_water))
    app.add_handler(CommandHandler("workout",     cmd_workout))
    app.add_handler(CommandHandler("weightgraph", cmd_weight_graph))
    app.add_handler(CommandHandler("weightavg",   cmd_weight_avg))
    app.add_handler(CommandHandler("stepsgraph",  cmd_steps_graph))
    app.add_handler(CommandHandler("stepsavg",    cmd_steps_avg))
    app.add_handler(CommandHandler("myreport",    cmd_myreport))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    # Instructor commands
    app.add_handler(CommandHandler("stats",          cmd_stats))
    app.add_handler(CommandHandler("report",         cmd_report))
    app.add_handler(CommandHandler("week",           cmd_week))
    app.add_handler(CommandHandler("meals",          cmd_meals))
    app.add_handler(CommandHandler("schedule",        cmd_schedule))
    app.add_handler(CommandHandler("scheduleweekly", cmd_scheduleweekly))
    app.add_handler(CommandHandler("stopweekly",     cmd_stopweekly))
    app.add_handler(CommandHandler("checkinstatus",  cmd_checkinstatus))
    app.add_handler(CommandHandler("clearschedule",  cmd_clearschedule))

    # Conversations — must be registered before the photo handler
    app.add_handler(build_fitness_conversation())
    app.add_handler(build_checkin_conversation())

    # Auto-register any group the bot is active in.
    # Group -1 ensures this runs on every group update before command handlers.
    app.add_handler(ChatMemberHandler(_on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, _on_group_message), group=-1)

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
