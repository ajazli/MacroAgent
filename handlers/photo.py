"""
Photo message handler — analyses meal photos via Claude vision API.

Triggered two ways:
  1. Auto: user sends any photo (private chat) or a photo with a meal keyword caption (groups)
  2. Command: /meal — send a photo with /meal as caption, or reply to a photo with /meal
     Works in groups even when bot privacy mode is enabled, since commands are always received.

Corrections:
  Reply to the bot's meal analysis message with the corrected values
  (e.g. "actually 350 calories and 28g protein") and the bot will update the log.
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
    "Try `/meal` with a clearer photo or log manually with `/log meal`\\."
)

_MEAL_ANALYSIS_MARKER = "🍽️ *Meal logged*"


def _is_meal_photo(update: Update) -> bool:
    """Return True if the photo should trigger auto-analysis.

    Private chats: any photo (no caption = meal).
    Group chats: only if caption contains a meal keyword (avoids analysing
    every meme/photo sent in the group).
    """
    msg = update.effective_message
    if not msg.photo:
        return False

    chat = update.effective_chat
    caption = (msg.caption or "").strip().lower()

    if chat and chat.type in ("group", "supergroup"):
        return any(kw in caption for kw in _MEAL_KEYWORDS)

    if not caption:
        return True
    return any(kw in caption for kw in _MEAL_KEYWORDS)


async def _run_meal_analysis(
    photo_message,
    reply_message,
    user: dict,
    tg_user,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Download the photo from photo_message, run Claude analysis, persist, and reply."""
    processing_msg = await reply_message.reply_text(
        formatter.escape("🔍 Analysing your meal photo…"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    photo = photo_message.photo[-1]
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()
        image_bytes = bytes(image_bytes)
    except Exception:
        logger.exception("Failed to download photo from Telegram for user %s", tg_user.id)
        await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    file_path: str = tg_file.file_path or ""
    if file_path.lower().endswith(".png"):
        media_type = "image/png"
    elif file_path.lower().endswith(".webp"):
        media_type = "image/webp"
    else:
        media_type = "image/jpeg"

    try:
        raw_result = await nutrition.analyse_meal_photo(image_bytes, media_type=media_type)
    except Exception:
        logger.exception("Nutrition analysis raised unexpectedly for user %s", tg_user.id)
        raw_result = None

    if raw_result is None:
        await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if raw_result and "_debug_error" in raw_result:
        from services.formatter import escape
        await processing_msg.edit_text(
            escape(f"⚠️ API Error: {raw_result['_debug_error']}"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if "error" in raw_result:
        await processing_msg.edit_text(ANALYSIS_ERROR_MSG, parse_mode=ParseMode.MARKDOWN_V2)
        return

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

    reply_text = formatter.format_meal_analysis(meal_data)
    result_msg = await processing_msg.edit_text(reply_text, parse_mode=ParseMode.MARKDOWN_V2)

    # Store the message→log mapping so users can reply to correct the analysis
    try:
        await db.save_log_message(log_row["id"], result_msg.chat.id, result_msg.message_id)
    except Exception:
        logger.warning("Could not save log_message mapping for log %s", log_row["id"])


async def _resolve_user(tg_user, message):
    """Auto-register user and return DB row, or send error and return None."""
    try:
        display_name = (
            tg_user.username.lower() if tg_user.username
            else (tg_user.first_name or "user").lower()
        )
        return await db.get_or_create_user(tg_user.id, display_name)
    except Exception:
        logger.exception("DB error auto-registering user %s", tg_user.id)
        await message.reply_text(
            formatter.escape("⚠️ Could not register you. Please try /start first."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-triggered when a photo message is received."""
    if not _is_meal_photo(update):
        return

    tg_user = update.effective_user
    message = update.effective_message

    user = await _resolve_user(tg_user, message)
    if user is None:
        return

    await _run_meal_analysis(message, message, user, tg_user, context)


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

    await _run_meal_analysis(photo_message, message, user, tg_user, context)


async def handle_meal_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles a text reply to one of the bot's meal analysis messages.
    Parses the correction with Claude and updates the stored log entry.
    """
    message = update.effective_message

    # Must be a text reply to the bot's own message
    if not message.reply_to_message:
        return
    if message.reply_to_message.from_user is None:
        return
    if message.reply_to_message.from_user.id != context.bot.id:
        return

    # Must be replying to a meal analysis (identified by the marker line)
    replied_text = message.reply_to_message.text or ""
    if _MEAL_ANALYSIS_MARKER not in replied_text:
        return

    chat_id    = message.chat.id
    replied_id = message.reply_to_message.message_id

    log_row = await db.get_log_by_message(chat_id, replied_id)
    if log_row is None:
        await message.reply_text(
            formatter.escape("⚠️ Could not find the original meal log to update."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    correction_text = message.text or ""
    original_data   = log_row["data"] if isinstance(log_row["data"], dict) else {}

    processing = await message.reply_text(
        formatter.escape("✏️ Applying correction…"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    corrected_raw = await nutrition.parse_correction(original_data, correction_text)
    if corrected_raw is None:
        await processing.edit_text(
            formatter.escape(
                "⚠️ Could not parse the correction. Try:\n"
                "• Food identity: \"it's satay, not rendang\"\n"
                "• Specific values: \"calories: 350, protein: 28g\""
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        corrected_data = nutrition.normalise_nutrition(corrected_raw)
        await db.update_log_data(log_row["id"], corrected_data)
    except Exception:
        logger.exception("Failed to update meal log %s", log_row["id"])
        await processing.edit_text(
            formatter.escape("⚠️ Correction parsed but could not be saved. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    reply = formatter.format_meal_analysis(corrected_data)
    await processing.edit_text(
        reply + "\n\n✏️ _Updated_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Update the message→log mapping to point to the same log (mapping unchanged, but
    # save again in case the original mapping was for a different message)
    try:
        await db.save_log_message(log_row["id"], chat_id, replied_id)
    except Exception:
        pass
