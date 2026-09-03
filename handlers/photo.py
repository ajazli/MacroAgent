"""
Photo message handler — analyses meal photos via Claude vision API.

Triggered two ways:
  1. Auto: any photo is silently sent to Claude; only replied to if food is detected.
  2. Command: /meal — send a photo with /meal as caption, or reply to a photo with /meal.
     Works in groups even when bot privacy mode is enabled, since commands are always received.

Corrections:
  Reply to the bot's meal analysis message in plain language and the saved log is
  recalculated in place — add an item ("add one fried chicken wing"), remove one
  ("no rice"), change a quantity ("there were 2 wings"), fix the dish ("it's satay
  not rendang"), or set values outright ("350 calories, 28g protein").
  The bot identifies its own analysis messages via a DB mapping rather than text
  matching. Anyone in the chat may correct any meal analysed there — the chat is
  the boundary, not the owner — and a cross-edit says whose log it changed.
"""

import json
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter, nutrition

logger = logging.getLogger(__name__)

ANALYSIS_ERROR_MSG = (
    "⚠️ Could not analyse this meal\\. "
    "Try `/meal` with a clearer photo or log manually with `/log meal`\\."
)


def _log_data(log_row: dict) -> dict:
    """Return a log row's JSONB payload as a dict, tolerating a raw JSON string."""
    raw = log_row.get("data", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


# Image tokens scale with area (roughly width x height / 750), so Telegram's
# largest size (~1280px) costs about 2.5x the ~800px one while adding nothing
# useful for naming a dish and judging a portion.
TARGET_PHOTO_EDGE = 800


def _pick_photo_size(sizes):
    """Pick the smallest Telegram photo whose longest edge still clears the target.

    Telegram returns sizes ascending; falls back to the largest when every
    available size is below the target.
    """
    usable = [p for p in sizes if max(p.width, p.height) >= TARGET_PHOTO_EDGE]
    return min(usable, key=lambda p: p.width * p.height) if usable else sizes[-1]


async def _download_photo(photo_message, tg_user, context):
    """Download a right-sized copy of the photo. Returns (image_bytes, media_type)."""
    photo = _pick_photo_size(photo_message.photo)
    logger.info(
        "Analysing photo at %dx%d (largest offered: %dx%d)",
        photo.width, photo.height,
        photo_message.photo[-1].width, photo_message.photo[-1].height,
    )
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download photo from Telegram for user %s", tg_user.id)
        return None, None

    file_path: str = tg_file.file_path or ""
    if file_path.lower().endswith(".png"):
        media_type = "image/png"
    elif file_path.lower().endswith(".webp"):
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    return image_bytes, media_type


async def _run_meal_analysis(
    photo_message,
    reply_message,
    user: dict,
    tg_user,
    context: ContextTypes.DEFAULT_TYPE,
    silent: bool = False,
) -> None:
    """Download the photo, run Claude analysis, persist, and reply.

    silent=True: called from auto-detection — no feedback until food is confirmed;
                 if Claude returns no food, do nothing at all.
    silent=False: called from /meal command — show processing message immediately
                  and report errors to the user.
    """
    # In command mode, show immediate feedback before the API call.
    processing_msg = None
    if not silent:
        processing_msg = await reply_message.reply_text(
            formatter.escape("🔍 Analysing your meal photo…"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    image_bytes, media_type = await _download_photo(photo_message, tg_user, context)
    if image_bytes is None:
        if processing_msg:
            await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    try:
        raw_result = await nutrition.analyse_meal_photo(image_bytes, media_type=media_type)
    except Exception:
        logger.exception("Nutrition analysis raised unexpectedly for user %s", tg_user.id)
        raw_result = None

    if raw_result is None:
        if processing_msg:
            await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if "_debug_error" in raw_result:
        if processing_msg:
            await processing_msg.edit_text(
                formatter.escape(f"⚠️ API Error: {raw_result['_debug_error']}"),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    if "error" in raw_result:
        # Claude could not identify food — stay silent in auto mode, report in command mode.
        if processing_msg:
            await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Food confirmed — in silent mode, send the processing message now.
    if silent:
        processing_msg = await reply_message.reply_text(
            formatter.escape("🔍 Analysing your meal photo…"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    try:
        meal_data = nutrition.normalise_nutrition(raw_result)
        log_row = await db.insert_log(user["id"], "meal", meal_data)
    except Exception:
        logger.exception("Failed to save meal log for user %s", tg_user.id)
        await processing_msg.edit_text(
            formatter.escape("⚠️ Meal analysed but could not be saved. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    status = await _calorie_status(user["id"], int(meal_data.get("calories", 0) or 0))
    await processing_msg.edit_text(
        formatter.format_meal_analysis(meal_data) + status,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Store the message→log mapping so users can reply to correct the analysis.
    try:
        await db.save_log_message(log_row["id"], processing_msg.chat_id, processing_msg.message_id)
    except Exception:
        logger.warning("Could not save log_message mapping for log %s", log_row["id"])


async def _calorie_status(user_id: int, meal_kcal: int) -> str:
    """The progress/warning line for a client's day, ready to append to an analysis.

    Returns "" when there is nothing worth saying, so callers can concatenate it
    unconditionally. Never raises: a meal must still be reported even if the
    day's totals cannot be worked out.
    """
    from handlers.targets import meal_kcal_limit
    try:
        target = await db.get_calorie_target(user_id)
        meals = await db.get_logs_for_user_today(user_id, log_type="meal")
        day_total = sum(int(_log_data(row).get("calories", 0) or 0) for row in meals)
        line = formatter.format_calorie_status(
            day_total, target, meal_kcal, meal_kcal_limit())
        return f"\n\n{line}" if line else ""
    except Exception:
        logger.warning("Could not build calorie status for user %s", user_id, exc_info=True)
        return ""


async def _resolve_user(tg_user, message):
    """Auto-register user and return DB row, or send error and return None."""
    try:
        return await db.get_or_create_user_from_tg(tg_user)
    except Exception:
        logger.exception("DB error auto-registering user %s", tg_user.id)
        await message.reply_text(
            formatter.escape("⚠️ Could not register you. Please try /start first."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-triggered for every photo. Silently calls Claude; only replies if food is detected."""
    message = update.effective_message
    if not message.photo:
        return

    tg_user = update.effective_user
    user = await _resolve_user(tg_user, message)
    if user is None:
        return

    await _run_meal_analysis(message, message, user, tg_user, context, silent=True)


async def cmd_meal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /meal command — analyse a meal photo in any chat, including groups with privacy mode on.

    Usage:
      • Send a photo with /meal as the caption  →  analyses that photo
      • Reply to someone's photo with /meal     →  analyses the replied-to photo
    """
    message = update.effective_message
    tg_user = update.effective_user

    photo_message = None
    if message.photo:
        photo_message = message
    elif message.reply_to_message and message.reply_to_message.photo:
        photo_message = message.reply_to_message

    if photo_message is None:
        await message.reply_text(
            formatter.escape(
                "📸 To analyse a meal:\n"
                "• Send a food photo with /meal as the caption\n"
                "• Or reply to a food photo with /meal"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    user = await _resolve_user(tg_user, message)
    if user is None:
        return

    await _run_meal_analysis(photo_message, message, user, tg_user, context, silent=False)


async def handle_meal_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Apply a plain-language correction to a meal the bot already analysed.

    Triggered by replying to one of the bot's analysis messages. Rather than matching
    on message text, the replied-to message ID is looked up in log_messages — if a
    mapping exists, it is a meal analysis.

    Returns True if the message was consumed as a correction, so the caller can fall
    through to conversational handling when it was not.
    """
    message = update.effective_message

    if not message.reply_to_message:
        return False

    chat_id    = message.chat_id
    replied_id = message.reply_to_message.message_id

    log_row = await db.get_log_by_message(chat_id, replied_id)
    if log_row is None:
        return False  # Not a meal analysis message

    correction_text = (message.text or "").strip()
    if not correction_text:
        return False

    # Anyone in this chat may correct any meal analysed here — the person who
    # cooked or shared the food often knows it better than whoever photographed
    # it. The boundary is the chat, not the owner: get_log_by_message is keyed by
    # (chat_id, message_id), so only meals logged in this chat are reachable.
    editor = await _resolve_user(update.effective_user, message)
    if editor is None:
        return True
    is_cross_edit = editor["id"] != log_row["user_id"]

    original_data = _log_data(log_row)

    processing = await message.reply_text(
        formatter.escape("✏️ Recalculating…"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    corrected_raw = await nutrition.parse_correction(original_data, correction_text)
    if corrected_raw is None:
        await processing.edit_text(
            formatter.escape(
                "⚠️ Could not apply that correction. Try:\n"
                "• Add or remove an item: \"add one fried chicken wing\"\n"
                "• Fix the dish: \"it's satay, not rendang\"\n"
                "• Set values: \"calories 350, protein 28g\""
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    change_summary = str(corrected_raw.get("change_summary", "")).strip()
    if change_summary == "NO_CHANGE":
        # Not actually a correction — probably a question or a comment about the meal.
        # Drop the placeholder and let the conversational handler take the message.
        try:
            await processing.delete()
        except Exception:
            pass
        return False

    try:
        corrected_data = nutrition.normalise_nutrition(corrected_raw)
        await db.update_log_data(log_row["id"], corrected_data)
    except Exception:
        logger.exception("Failed to update meal log %s", log_row["id"])
        await processing.edit_text(
            formatter.escape("⚠️ Correction understood but could not be saved. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    footer = f"✏️ _{formatter.escape(change_summary)}_" if change_summary else "✏️ _Updated_"
    if is_cross_edit:
        # In a group it must be obvious whose log just moved, and who moved it.
        owner_handle = (log_row.get("owner_username") or "").strip()
        owner = f"@{owner_handle}" if owner_handle else (log_row.get("owner_name") or "someone else")
        editor_handle = (editor.get("username") or "").strip()
        editor_name = f"@{editor_handle}" if editor_handle else editor["name"]
        footer += (
            f"\n_{formatter.escape(owner)}'s meal, corrected by "
            f"{formatter.escape(editor_name)}_"
        )
    status = await _calorie_status(
        log_row["user_id"], int(corrected_data.get("calories", 0) or 0))
    await processing.edit_text(
        formatter.format_meal_analysis(corrected_data) + status + "\n\n" + footer,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Point the mapping at the new message too, so the user can keep correcting
    # by replying to the latest analysis rather than hunting for the original.
    try:
        await db.save_log_message(log_row["id"], processing.chat_id, processing.message_id)
    except Exception:
        logger.warning("Could not re-map log_message for log %s", log_row["id"])

    return True
