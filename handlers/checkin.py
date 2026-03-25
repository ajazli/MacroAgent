"""
Weekly check-in conversation handler.
Flow: /checkin → front photo → side photo → back photo → nutrition score → stress score → waist (optional)
"""

import logging
import os
from typing import Optional

from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from services import db, formatter

logger = logging.getLogger(__name__)

# Conversation states
CI_FRONT, CI_SIDE, CI_BACK, CI_NUTRITION, CI_STRESS, CI_WAIST = range(6)

_KEY_FRONT     = "ci_front_file_id"
_KEY_SIDE      = "ci_side_file_id"
_KEY_BACK      = "ci_back_file_id"
_KEY_NUTRITION = "ci_nutrition_score"
_KEY_STRESS    = "ci_stress_score"
_KEY_DATE      = "ci_scheduled_date"


def _user_display_name(update: Update) -> str:
    user = update.effective_user
    return user.username.lower() if user.username else (user.first_name or "user").lower()


async def _ensure_registered(update: Update) -> dict:
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, _user_display_name(update))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await _ensure_registered(update)
    schedule = await db.get_check_in_schedule(user["id"])

    if schedule:
        date_str = schedule["scheduled_date"].strftime("%d %b %Y")
        context.user_data[_KEY_DATE] = schedule["scheduled_date"]
        intro = f"📋 *Weekly Check\\-In* \\({formatter.escape(date_str)}\\)\n\n"
    else:
        intro = "📋 *Weekly Check\\-In*\n\n"

    await update.message.reply_text(
        intro + formatter.escape("📸 Let's start! Please send your FRONT profile photo."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_FRONT


# ---------------------------------------------------------------------------
# Photo states
# ---------------------------------------------------------------------------

async def received_front_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[_KEY_FRONT] = update.message.photo[-1].file_id
    await update.message.reply_text(
        formatter.escape("✅ Front photo saved!\n\n📸 Now send your SIDE profile photo."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_SIDE


async def received_side_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[_KEY_SIDE] = update.message.photo[-1].file_id
    await update.message.reply_text(
        formatter.escape("✅ Side photo saved!\n\n📸 Now send your BACK profile photo."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_BACK


async def received_back_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[_KEY_BACK] = update.message.photo[-1].file_id
    await update.message.reply_text(
        formatter.escape(
            "✅ All 3 photos saved!\n\n"
            "🥗 How closely did you follow your nutritional plan this week? (1–10)"
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_NUTRITION


# ---------------------------------------------------------------------------
# Score states
# ---------------------------------------------------------------------------

async def received_nutrition_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        score = int(update.message.text.strip())
        if not 1 <= score <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter a number between 1 and 10."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return CI_NUTRITION

    context.user_data[_KEY_NUTRITION] = score
    await update.message.reply_text(
        formatter.escape("😤 Average stress level this week? (1–10, where 10 = very stressed)"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_STRESS


async def received_stress_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        score = int(update.message.text.strip())
        if not 1 <= score <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Please enter a number between 1 and 10."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return CI_STRESS

    context.user_data[_KEY_STRESS] = score
    await update.message.reply_text(
        formatter.escape("📏 Waist measurement in cm? (e.g. 82)  — or /skip to skip."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return CI_WAIST


# ---------------------------------------------------------------------------
# Waist (optional)
# ---------------------------------------------------------------------------

async def received_waist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip().lower().replace("cm", "").strip()
    try:
        waist_cm = round(float(text), 1)
        if waist_cm <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            formatter.escape("⚠️ Enter your waist in cm (e.g. 82), or /skip."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return CI_WAIST
    return await _complete_checkin(update, context, waist_cm)


async def skip_waist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _complete_checkin(update, context, None)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

async def _complete_checkin(
    update: Update, context: ContextTypes.DEFAULT_TYPE, waist_cm: Optional[float]
) -> int:
    user = await _ensure_registered(update)

    weight_entries = await db.get_weight_logs_for_user(user["id"], days=7)
    avg_weight = None
    if weight_entries:
        avg_weight = round(sum(e["kg"] for e in weight_entries) / len(weight_entries), 2)

    data = {
        "front_file_id":   context.user_data.get(_KEY_FRONT),
        "side_file_id":    context.user_data.get(_KEY_SIDE),
        "back_file_id":    context.user_data.get(_KEY_BACK),
        "nutrition_score": context.user_data.get(_KEY_NUTRITION),
        "stress_score":    context.user_data.get(_KEY_STRESS),
        "avg_weight_kg":   avg_weight,
    }
    if waist_cm is not None:
        data["waist_cm"] = waist_cm

    await db.insert_log(user["id"], "check_in", data)

    scheduled_date = context.user_data.get(_KEY_DATE)
    if scheduled_date:
        await db.mark_check_in_completed(user["id"], scheduled_date)

    # Send photo album back to user
    try:
        media = [
            InputMediaPhoto(data["front_file_id"], caption="Front"),
            InputMediaPhoto(data["side_file_id"],  caption="Side"),
            InputMediaPhoto(data["back_file_id"],  caption="Back"),
        ]
        await update.message.reply_media_group(media=media)
    except Exception:
        logger.warning("Could not send photo album for check-in")

    # Summary message
    name_esc   = formatter.escape(user["name"])
    avg_w_str  = formatter.escape(f"{avg_weight}kg") if avg_weight else formatter.escape("No data")
    waist_str  = formatter.escape(f"{waist_cm}cm") if waist_cm else formatter.escape("—")
    nutrition  = formatter.escape(str(data["nutrition_score"]))
    stress     = formatter.escape(str(data["stress_score"]))

    summary = (
        f"✅ *Check\\-In Complete — {name_esc}*\n\n"
        f"⚖️ Avg weight \\(7 days\\): *{avg_w_str}*\n"
        f"🥗 Nutrition adherence: *{nutrition}/10*\n"
        f"😤 Stress level: *{stress}/10*\n"
        f"📏 Waist: {waist_str}"
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN_V2)

    # Announce in Clocker topic
    group_chat_id_str = os.environ.get("GROUP_CHAT_ID", "").strip()
    if group_chat_id_str:
        from services.scheduler import get_clocker_topic_id
        clocker_topic_id = get_clocker_topic_id()
        announce = f"🎉 *{name_esc}* completed their weekly check\\-in\\!"
        try:
            kwargs: dict = {
                "chat_id": int(group_chat_id_str),
                "text": announce,
                "parse_mode": "MarkdownV2",
            }
            if clocker_topic_id:
                kwargs["message_thread_id"] = clocker_topic_id
            await context.bot.send_message(**kwargs)
        except Exception as exc:
            logger.warning("Could not announce check-in completion: %s", exc)

    # Clean up
    for k in [_KEY_FRONT, _KEY_SIDE, _KEY_BACK, _KEY_NUTRITION, _KEY_STRESS, _KEY_DATE]:
        context.user_data.pop(k, None)

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

async def cancel_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    for k in [_KEY_FRONT, _KEY_SIDE, _KEY_BACK, _KEY_NUTRITION, _KEY_STRESS, _KEY_DATE]:
        context.user_data.pop(k, None)
    await update.message.reply_text(
        formatter.escape("❌ Check-in cancelled."), parse_mode=ParseMode.MARKDOWN_V2
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Build ConversationHandler
# ---------------------------------------------------------------------------

def build_checkin_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("checkin", cmd_checkin)],
        states={
            CI_FRONT:     [MessageHandler(filters.PHOTO, received_front_photo)],
            CI_SIDE:      [MessageHandler(filters.PHOTO, received_side_photo)],
            CI_BACK:      [MessageHandler(filters.PHOTO, received_back_photo)],
            CI_NUTRITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_nutrition_score)],
            CI_STRESS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, received_stress_score)],
            CI_WAIST:     [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_waist),
                CommandHandler("skip", skip_waist),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_checkin)],
        per_user=True,
        per_chat=True,
    )
