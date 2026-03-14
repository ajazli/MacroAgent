"""
Photo message handler — analyses meal photos via Claude vision API.
Triggered when a user sends a photo (with or without a meal-related caption).
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter, nutrition

logger = logging.getLogger(__name__)

_MEAL_KEYWORDS = {"meal", "food", "makan", "lunch", "dinner", "breakfast", "snack"}

ANALYSIS_ERROR_MSG = (
    "⚠️ Could not analyse this meal\\. "
    "Try `/log meal` manually or resend a clearer photo\\."
)


def _is_meal_photo(update: Update) -> bool:
    """Return True if the photo should be treated as a meal entry."""
    msg = update.effective_message
    if not msg.photo:
        return False
    caption = (msg.caption or "").strip().lower()
    # No caption → treat as meal; caption contains a meal keyword → treat as meal
    if not caption:
        return True
    return any(kw in caption for kw in _MEAL_KEYWORDS)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_meal_photo(update):
        return  # Ignore non-meal photos silently

    tg_user = update.effective_user
    message = update.effective_message

    # Auto-register if needed
    try:
        display_name = tg_user.username.lower() if tg_user.username else (tg_user.first_name or "user").lower()
        user = await db.get_or_create_user(tg_user.id, display_name)
    except Exception:
        logger.exception("DB error auto-registering user %s during photo handler", tg_user.id)
        await message.reply_text(
            formatter.escape("⚠️ Could not register you in the database. Please try /start first."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Send a "processing" placeholder so the user knows we're working
    processing_msg = await message.reply_text(
        formatter.escape("🔍 Analysing your meal photo…"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Download the highest-resolution photo available
    photo = message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()
        image_bytes = bytes(image_bytes)
    except Exception:
        logger.exception("Failed to download photo from Telegram for user %s", tg_user.id)
        await processing_msg.edit_text(
            ANALYSIS_ERROR_MSG,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Determine MIME type from Telegram file path (usually .jpg)
    file_path: str = tg_file.file_path or ""
    if file_path.lower().endswith(".png"):
        media_type = "image/png"
    elif file_path.lower().endswith(".webp"):
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    # Call Claude vision
    try:
        raw_result = await nutrition.analyse_meal_photo(image_bytes, media_type=media_type)
    except Exception:
        logger.exception("Nutrition analysis raised unexpectedly for user %s", tg_user.id)
        raw_result = None

    if raw_result is None:
        await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Debug: surface API errors temporarily
    if raw_result and "_debug_error" in raw_result:
        from services.formatter import escape
        await processing_msg.edit_text(
            escape(f"⚠️ API Error: {raw_result['_debug_error']}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Claude indicated it couldn't identify food
    if "error" in raw_result:
        await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Normalise and persist
    try:
        meal_data = nutrition.normalise_nutrition(raw_result)
        await db.insert_log(user["id"], "meal", meal_data)
    except Exception:
        logger.exception("Failed to save meal log for user %s", tg_user.id)
        await processing_msg.edit_text(
            formatter.escape("⚠️ Meal analysed but could not be saved. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Format and send the result
    reply = formatter.format_meal_analysis(meal_data)
    await processing_msg.edit_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
