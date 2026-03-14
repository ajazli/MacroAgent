"""
Fitness exercise command handlers.
Commands: /pushups, /situps, /planks, /run, /jog

Multi-step input via ConversationHandler:
  - Static exercises (push-ups, sit-ups, planks): asks reps → sets
  - Runs / jogs: asks distance → timing
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from services import db, formatter

logger = logging.getLogger(__name__)

# Conversation states
REPS, SETS, DISTANCE, TIMING = range(4)

_KEY_TYPE = "fitness_type"
_KEY_REPS = "fitness_reps"
_KEY_DISTANCE = "fitness_distance"

_STATIC_LABELS = {
    "exercise_pushup": ("Push-ups", "💪"),
    "exercise_situp":  ("Sit-ups",  "🔥"),
    "exercise_plank":  ("Planks",   "🧱"),
}


def _user_display_name(update: Update) -> str:
    user = update.effective_user
    return user.username.lower() if user.username else (user.first_name or "user").lower()


async def _ensure_registered(update: Update) -> dict:
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, _user_display_name(update))


# ---------------------------------------------------------------------------
# Entry points — static exercises (reps × sets)
# ---------------------------------------------------------------------------

async def _start_static(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ex_type: str
) -> int:
    label, emoji = _STATIC_LABELS[ex_type]
    context.user_data[_KEY_TYPE] = ex_type
    await update.message.reply_text(
        formatter.escape(f"{emoji} {label} — how many reps?"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return REPS


async def cmd_pushups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_static(update, context, "exercise_pushup")


async def cmd_situps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_static(update, context, "exercise_situp")


async def cmd_planks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_static(update, context, "exercise_plank")


# ---------------------------------------------------------------------------
# Entry points — runs / jogs (distance → timing)
# ---------------------------------------------------------------------------

async def _start_run(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ex_type: str, emoji: str, name: str
) -> int:
    context.user_data[_KEY_TYPE] = ex_type
    await update.message.reply_text(
        formatter.escape(f"{emoji} {name} — what distance in km? (e.g. 2.4)"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return DISTANCE


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_run(update, context, "exercise_run", "🏃", "Run")


async def cmd_jog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _start_run(update, context, "exercise_jog", "🚶", "Jog")


# ---------------------------------------------------------------------------
# State: REPS
# ---------------------------------------------------------------------------

async def received_reps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        reps = int(float(update.message.text.strip()))
        if reps <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter a valid number of reps (e.g. 20)."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return REPS

    context.user_data[_KEY_REPS] = reps
    await update.message.reply_text(
        formatter.escape(f"Got {reps} reps — how many sets?"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return SETS


# ---------------------------------------------------------------------------
# State: SETS
# ---------------------------------------------------------------------------

async def received_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        sets_count = int(float(update.message.text.strip()))
        if sets_count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter a valid number of sets (e.g. 3)."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return SETS

    user = await _ensure_registered(update)
    ex_type = context.user_data.pop(_KEY_TYPE, "exercise_pushup")
    reps = context.user_data.pop(_KEY_REPS, 0)

    data = {"reps": reps, "sets": sets_count}
    await db.insert_log(user["id"], ex_type, data)

    label, emoji = _STATIC_LABELS.get(ex_type, ("Exercise", "✅"))
    await update.message.reply_text(
        formatter.escape(f"✅ {label} logged: {reps} reps × {sets_count} sets"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# State: DISTANCE
# ---------------------------------------------------------------------------

async def received_distance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower().replace("km", "").strip()
    try:
        distance = round(float(text), 2)
        if distance <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter a valid distance in km (e.g. 2.4)."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return DISTANCE

    context.user_data[_KEY_DISTANCE] = distance
    await update.message.reply_text(
        formatter.escape(f"Got {distance}km — what was your timing? (mm:ss, e.g. 12:30)"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return TIMING


# ---------------------------------------------------------------------------
# State: TIMING
# ---------------------------------------------------------------------------

def _parse_timing(text: str) -> tuple:
    """Parse mm:ss or h:mm:ss into (total_seconds, display_str). Raises ValueError on bad input."""
    parts = text.strip().split(":")
    if len(parts) == 2:
        m, s = int(parts[0]), int(parts[1])
        if not (0 <= s < 60):
            raise ValueError("Invalid seconds")
        return m * 60 + s, f"{m}:{s:02d}"
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        if not (0 <= s < 60 and 0 <= m < 60):
            raise ValueError("Invalid time")
        return h * 3600 + m * 60 + s, f"{h}:{m:02d}:{s:02d}"
    raise ValueError("Expected mm:ss format")


async def received_timing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        total_seconds, timing_str = _parse_timing(update.message.text)
    except (ValueError, IndexError):
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter timing as mm:ss (e.g. 12:30)."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return TIMING

    user = await _ensure_registered(update)
    ex_type = context.user_data.pop(_KEY_TYPE, "exercise_run")
    distance = context.user_data.pop(_KEY_DISTANCE, 0.0)

    data = {
        "distance_km": distance,
        "timing_seconds": total_seconds,
        "timing_str": timing_str,
    }
    await db.insert_log(user["id"], ex_type, data)

    label = "Jog" if "jog" in ex_type else "Run"
    await update.message.reply_text(
        formatter.escape(f"✅ {label} logged: {distance}km in {timing_str}"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in (_KEY_TYPE, _KEY_REPS, _KEY_DISTANCE):
        context.user_data.pop(k, None)
    await update.message.reply_text(
        formatter.escape("❌ Cancelled."), parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Build ConversationHandler
# ---------------------------------------------------------------------------

def build_fitness_conversation() -> ConversationHandler:
    """Build the ConversationHandler for all fitness exercise commands."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("pushups", cmd_pushups),
            CommandHandler("situps",  cmd_situps),
            CommandHandler("planks",  cmd_planks),
            CommandHandler("run",     cmd_run),
            CommandHandler("jog",     cmd_jog),
        ],
        states={
            REPS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, received_reps)],
            SETS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, received_sets)],
            DISTANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_distance)],
            TIMING:   [MessageHandler(filters.TEXT & ~filters.COMMAND, received_timing)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )
