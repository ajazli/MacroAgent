"""
Message formatting helpers for Telegram MarkdownV2 replies.
All public functions return ready-to-send MarkdownV2 strings.
"""

from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------

_MDV2_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def escape(text: str) -> str:
    """Escape special MarkdownV2 characters."""
    result = []
    for ch in str(text):
        if ch in _MDV2_SPECIAL:
            result.append(f"\\{ch}")
        else:
            result.append(ch)
    return "".join(result)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_today(logs: list[dict]) -> dict:
    """Collapse a list of log rows into today's summary totals."""
    totals = {
        "calories": 0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "meal_count": 0,
        "steps": 0,
        "water_ml": 0,
        "weight_kg": None,
    }
    for row in logs:
        data = row["data"] if isinstance(row["data"], dict) else {}
        t = row["type"]
        if t == "meal":
            totals["calories"] += data.get("calories", 0)
            totals["protein"] += data.get("protein", 0)
            totals["carbs"] += data.get("carbs", 0)
            totals["fat"] += data.get("fat", 0)
            totals["meal_count"] += 1
        elif t == "steps":
            totals["steps"] += data.get("count", 0)
        elif t == "water":
            totals["water_ml"] += data.get("ml", 0)
        elif t == "weight":
            # Keep the last weight logged
            totals["weight_kg"] = data.get("kg")
    return totals


# ---------------------------------------------------------------------------
# /today formatter
# ---------------------------------------------------------------------------

def format_today_summary(user_name: str, logs: list[dict]) -> str:
    t = _aggregate_today(logs)
    name_esc = escape(user_name)
    today_str = escape(date.today().strftime("%d %b %Y"))

    weight = escape(f"{t['weight_kg']}kg") if t["weight_kg"] is not None else escape("—")
    steps = escape(f"{t['steps']:,}") if t["steps"] else escape("—")
    water = escape(f"{t['water_ml']} ml") if t["water_ml"] else escape("—")

    lines = [
        f"📊 *Today's Summary — {name_esc}*",
        f"_{today_str}_",
        escape("━━━━━━━━━━━━━━━"),
        f"🍽 Calories: *{escape(str(t['calories']))} kcal* \\({escape(str(t['meal_count']))} meals\\)",
        f"💪 Protein: *{escape(str(round(t['protein'], 1)))}g*",
        f"🥗 Carbs: {escape(str(round(t['carbs'], 1)))}g \\| Fat: {escape(str(round(t['fat'], 1)))}g",
        f"👟 Steps: {steps}",
        f"💧 Water: {water}",
        f"⚖️ Weight: {weight}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Meal photo analysis formatter
# ---------------------------------------------------------------------------

def format_meal_analysis(data: dict) -> str:
    confidence = data.get("confidence", "medium")
    conf_emoji = "⚠️" if confidence == "low" else ("✅" if confidence == "high" else "🔶")

    description = escape(data.get("description", "Unknown meal"))
    cal = escape(str(data.get("calories", 0)))
    protein = escape(str(data.get("protein", 0)))
    carbs = escape(str(data.get("carbs", 0)))
    fat = escape(str(data.get("fat", 0)))
    fiber = escape(str(data.get("fiber", 0)))
    conf_text = escape(confidence)
    notes = escape(data.get("notes", ""))

    lines = [
        "🍽️ *Meal logged*",
        f"📋 {description}",
        "",
        f"Calories: *{cal} kcal*",
        f"Protein: {protein}g \\| Carbs: {carbs}g \\| Fat: {fat}g \\| Fiber: {fiber}g",
        "",
        f"{conf_emoji} Confidence: {conf_text}",
    ]
    if notes:
        lines.append(f"_{notes}_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /stats formatter (instructor — today's totals per user)
# ---------------------------------------------------------------------------

def format_stats_today(users_logs: dict[str, list]) -> str:
    """
    users_logs: mapping of user_name -> list of log rows for today
    """
    if not users_logs:
        return escape("No data logged today.")

    lines = ["📊 *Today's Stats*", ""]
    for name, logs in sorted(users_logs.items()):
        t = _aggregate_today(logs)
        name_esc = escape(name)
        cal = escape(str(t["calories"]))
        protein = escape(str(round(t["protein"], 1)))
        steps = escape(f"{t['steps']:,}") if t["steps"] else escape("—")
        weight = escape(f"{t['weight_kg']}kg") if t["weight_kg"] is not None else escape("—")
        lines.append(f"👤 *{name_esc}*")
        lines.append(
            f"  🍽 {cal} kcal \\| 💪 {protein}g protein \\| 👟 {steps} steps \\| ⚖️ {weight}"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# /report formatter (instructor — 7-day day-by-day table)
# ---------------------------------------------------------------------------

def format_report(user_name: str, logs: list[dict], days: int = 7) -> str:
    """
    Build a day-by-day table for the past `days` days.
    logs: list of log rows for the user in the date range (already filtered).
    """
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    # Bucket logs by date
    by_date: dict[date, list] = {d: [] for d in dates}
    for row in logs:
        d = row["date"] if isinstance(row["date"], date) else row["date"]
        if d in by_date:
            by_date[d].append(row)

    name_esc = escape(user_name)
    lines = [
        f"📅 *7\\-Day Report — {name_esc}*",
        "",
        escape("Date       | Cal  | Protein | Steps  | Weight"),
        escape("-----------|------|---------|--------|-------"),
    ]

    for d in dates:
        t = _aggregate_today(by_date[d])
        date_str = d.strftime("%d %b")
        cal = str(t["calories"]) if t["calories"] else "—"
        protein = f"{round(t['protein'], 1)}g" if t["protein"] else "—"
        steps = f"{t['steps']:,}" if t["steps"] else "—"
        weight = f"{t['weight_kg']}kg" if t["weight_kg"] is not None else "—"
        row_str = f"{date_str:<10} | {cal:<4} | {protein:<7} | {steps:<6} | {weight}"
        lines.append(escape(row_str))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /meals formatter (instructor — today's meals for a user)
# ---------------------------------------------------------------------------

def format_meals_today(user_name: str, meal_logs: list[dict]) -> str:
    name_esc = escape(user_name)
    today_str = escape(date.today().strftime("%d %b %Y"))

    if not meal_logs:
        return f"🍽️ *Meals — {name_esc}* \\({today_str}\\)\n\n_{escape('No meals logged today.')}_"

    lines = [f"🍽️ *Meals — {name_esc}* \\({today_str}\\)", ""]
    for i, row in enumerate(meal_logs, 1):
        data = row["data"] if isinstance(row["data"], dict) else {}
        desc = escape(data.get("description", "Unknown"))
        cal = escape(str(data.get("calories", 0)))
        protein = escape(str(data.get("protein", 0)))
        carbs = escape(str(data.get("carbs", 0)))
        fat = escape(str(data.get("fat", 0)))
        lines.append(f"*{escape(str(i))}\\.* {desc}")
        lines.append(f"   {cal} kcal \\| 💪 {protein}g \\| 🥗 {carbs}g \\| 🧈 {fat}g")
        lines.append("")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Generic error / info helpers
# ---------------------------------------------------------------------------

def format_log_confirmation(log_type: str, data: dict) -> str:
    if log_type == "weight":
        return f"✅ Weight logged: *{escape(str(data['kg']))} kg*"
    elif log_type == "steps":
        steps_str = f"{data['count']:,}"
        return f"✅ Steps logged: *{escape(steps_str)}*"
    elif log_type == "water":
        return f"✅ Water logged: *{escape(str(data['ml']))} ml*"
    elif log_type == "workout":
        return f"✅ Workout logged: _{escape(data.get('description', ''))}_"
    return "✅ Logged\\."
