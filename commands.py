"""
User-facing command handlers: /start, /log, /today, /health
"""

import io
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from services import db, formatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_display_name(update: Update) -> str:
    user = update.effective_user
    if user.username:
        return user.username.lower()
    return (user.first_name or "user").lower()


async def _ensure_registered(update: Update) -> dict:
    """Auto-register user if not yet in DB and return the user row."""
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, _user_display_name(update))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _ensure_registered(update)
    name = formatter.escape(user["name"])
    msg = (
        f"👋 Hey *{name}*\\! Welcome to *Jazli's Macro Agent* 🏋️\n\n"
        "I'll help you track meals, workouts, weight, steps, and water\\.\n\n"
        "*Nutrition & basics:*\n"
        "`/log weight 74\\.2` — log weight\n"
        "`/log steps 8500` — log steps\n"
        "`/log water 500` — log water \\(ml\\)\n"
        "`/log workout chest day 45min` — free\\-form workout note\n"
        "`/today` — today's summary\n"
        "`/weightgraph` — weight chart \\(past 7 days\\)\n"
        "`/weightavg` — weight average & trend\n\n"
        "*Tracked exercises:*\n"
        "`/pushups` — log push\\-ups \\(reps × sets\\)\n"
        "`/situps` — log sit\\-ups \\(reps × sets\\)\n"
        "`/planks` — log planks \\(reps × sets\\)\n"
        "`/run` — log a run \\(distance → timing\\)\n"
        "`/jog` — log a jog \\(distance → timing\\)\n"
        "`/cancel` — cancel a pending exercise entry\n\n"
        "*Personal bests:*\n"
        "`/maxpushups` — log max push\\-up reps in one set\n"
        "`/maxsitups` — log max sit\\-up reps in one minute\n"
        "`/pb24` — log your 2\\.4km best timing\n\n"
        "📸 Send any photo and I'll analyse it as a meal\\!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ Bot is running")


# ---------------------------------------------------------------------------
# /log
# ---------------------------------------------------------------------------

async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args  # list of strings after /log
    user = await _ensure_registered(update)

    if not args:
        await update.message.reply_text(
            formatter.escape(
                "Usage:\n"
                "/log weight 74.2\n"
                "/log steps 8500\n"
                "/log water 500\n"
                "/log workout <description>"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    sub = args[0].lower()

    try:
        if sub == "weight":
            await _log_weight(update, user, args[1:])
        elif sub == "steps":
            await _log_steps(update, user, args[1:])
        elif sub == "water":
            await _log_water(update, user, args[1:])
        elif sub == "workout":
            await _log_workout(update, user, args[1:])
        else:
            await update.message.reply_text(
                formatter.escape(f"Unknown log type '{sub}'. Use: weight, steps, water, workout."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception:
        logger.exception("Error in cmd_log sub=%s user_id=%s", sub, user["id"])
        await update.message.reply_text(
            formatter.escape("⚠️ Something went wrong saving your log. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _log_weight(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log weight 74.2"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        kg = float(args[0])
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"kg": round(kg, 2)}
    await db.insert_log(user["id"], "weight", data)
    reply = formatter.format_log_confirmation("weight", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_steps(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log steps 8500"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        count = int(float(args[0]))
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"count": count}
    await db.insert_log(user["id"], "steps", data)
    reply = formatter.format_log_confirmation("steps", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_water(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log water 500"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    try:
        ml = int(float(args[0]))
    except ValueError:
        await update.message.reply_text(
            formatter.escape(f"'{args[0]}' is not a valid number."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = {"ml": ml}
    await db.insert_log(user["id"], "water", data)
    reply = formatter.format_log_confirmation("water", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


async def _log_workout(update: Update, user: dict, args: list[str]) -> None:
    if not args:
        await update.message.reply_text(
            formatter.escape("Usage: /log workout chest and triceps 45min"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    description = " ".join(args)
    data = {"description": description, "exercises": []}
    await db.insert_log(user["id"], "workout", data)
    reply = formatter.format_log_confirmation("workout", data)
    await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /today
# ---------------------------------------------------------------------------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = await _ensure_registered(update)
        logs = await db.get_logs_for_user_today(user["id"])
        prev_weight = await db.get_last_weight_before_today(user["id"])
        reply = formatter.format_today_summary(user["name"], logs, prev_weight_kg=prev_weight)
        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        logger.exception("Error in cmd_today for telegram_id=%s", update.effective_user.id)
        await update.message.reply_text(
            formatter.escape("⚠️ Could not retrieve today's summary. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# /weightgraph — weight chart over the past 7 days
# ---------------------------------------------------------------------------

async def cmd_weight_graph(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = await _ensure_registered(update)
        entries = await db.get_weight_logs_for_user(user["id"], days=7)

        if not entries:
            await update.message.reply_text(
                formatter.escape("No weight entries in the past 7 days. Log with /log weight <kg>."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime

        dates = [datetime.combine(e["date"], datetime.min.time()) for e in entries]
        weights = [e["kg"] for e in entries]

        fig, ax = plt.subplots(figsize=(8, 4))
        fig.patch.set_facecolor("#1e1e2e")
        ax.set_facecolor("#1e1e2e")

        ax.plot(dates, weights, color="#89b4fa", linewidth=2.5, marker="o",
                markersize=7, markerfacecolor="#cba6f7", markeredgewidth=0)
        ax.fill_between(dates, weights, min(weights) - 0.5,
                        alpha=0.15, color="#89b4fa")

        for x, y in zip(dates, weights):
            ax.annotate(f"{y}kg", (x, y), textcoords="offset points",
                        xytext=(0, 10), ha="center", fontsize=9,
                        color="#cdd6f4")

        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        plt.xticks(rotation=30, ha="right", color="#cdd6f4", fontsize=9)
        plt.yticks(color="#cdd6f4", fontsize=9)
        ax.tick_params(colors="#cdd6f4")
        for spine in ax.spines.values():
            spine.set_edgecolor("#45475a")

        ax.set_title(f"⚖️ {user['name']}'s Weight — Past 7 Days",
                     color="#cdd6f4", fontsize=12, pad=12)
        ax.set_ylabel("kg", color="#cdd6f4", fontsize=10)
        ax.yaxis.label.set_color("#cdd6f4")
        ax.grid(axis="y", color="#45475a", linestyle="--", alpha=0.5)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)

        await update.message.reply_photo(photo=buf, caption=f"Weight trend for {user['name']} 📈")

    except Exception:
        logger.exception("Error in cmd_weight_graph for telegram_id=%s", update.effective_user.id)
        await update.message.reply_text(
            formatter.escape("⚠️ Could not generate weight graph. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# /weightavg — average weight stats
# ---------------------------------------------------------------------------

async def cmd_weight_avg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = await _ensure_registered(update)
        entries = await db.get_weight_logs_for_user(user["id"], days=7)

        if not entries:
            await update.message.reply_text(
                formatter.escape("No weight entries in the past 7 days. Log with /log weight <kg>."),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        weights = [e["kg"] for e in entries]
        avg = round(sum(weights) / len(weights), 2)
        low = min(weights)
        high = max(weights)
        latest = weights[-1]
        trend = round(latest - weights[0], 2)
        trend_icon = "📈" if trend > 0 else ("📉" if trend < 0 else "➡️")

        name_esc = formatter.escape(user["name"])
        msg = (
            f"⚖️ *{name_esc}'s Weight \\(past 7 days\\)*\n"
            f"{formatter.escape('━━━━━━━━━━━━━━━')}\n"
            f"📊 Average: *{formatter.escape(str(avg))} kg*\n"
            f"🔽 Lowest:  {formatter.escape(str(low))} kg\n"
            f"🔼 Highest: {formatter.escape(str(high))} kg\n"
            f"📌 Latest:  {formatter.escape(str(latest))} kg\n"
            f"{trend_icon} Trend:   *{formatter.escape(str(abs(trend)))} kg "
            f"{'gain' if trend > 0 else ('loss' if trend < 0 else 'no change')}*\n"
            f"{formatter.escape('━━━━━━━━━━━━━━━')}\n"
            f"_{formatter.escape(str(len(entries)))} entries over 7 days_"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception:
        logger.exception("Error in cmd_weight_avg for telegram_id=%s", update.effective_user.id)
        await update.message.reply_text(
            formatter.escape("⚠️ Could not calculate weight average. Please try again."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
