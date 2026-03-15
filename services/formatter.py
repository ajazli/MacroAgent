"""
Message formatting helpers for Telegram MarkdownV2 replies.
All public functions return ready-to-send MarkdownV2 strings.
"""

from datetime import date, timedelta
from typing import Optional

from services.tz import today_sgt


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
# Helpers
# ---------------------------------------------------------------------------

def _seconds_to_mmss(total_seconds: int) -> str:
    m = total_seconds // 60
    s = total_seconds % 60
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_today(logs: list) -> dict:
    """Collapse a list of log rows into today's summary totals."""
    import json as _json
    totals = {
        "calories": 0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "meal_count": 0,
        "steps": 0,
        "water_ml": 0,
        "weight_kg": None,
        "workouts": [],
        "exercises": {
            "pushup": [],  # [{reps, sets}]
            "situp":  [],
            "plank":  [],
            "run":    [],  # [{distance_km, timing_seconds, timing_str}]
            "jog":    [],
        },
        "personal_bests": {
            "pushup": None,   # max_reps (int)
            "situp":  None,   # max_reps (int)
            "2_4km":  None,   # timing_seconds (int)
            "2_4km_str": None,
        },
    }
    for row in logs:
        raw = row["data"]
        if isinstance(raw, str):
            try:
                raw = _json.loads(raw)
            except Exception:
                raw = {}
        data = raw if isinstance(raw, dict) else {}
        t = row["type"]
        if t == "meal":
            totals["calories"] += data.get("calories", 0)
            totals["protein"]  += data.get("protein", 0)
            totals["carbs"]    += data.get("carbs", 0)
            totals["fat"]      += data.get("fat", 0)
            totals["meal_count"] += 1
        elif t == "steps":
            totals["steps"] += data.get("count", 0)
        elif t == "water":
            totals["water_ml"] += data.get("ml", 0)
        elif t == "weight":
            totals["weight_kg"] = data.get("kg")
        elif t == "workout":
            desc = data.get("description", "")
            if desc:
                totals["workouts"].append(desc)
        elif t.startswith("exercise_"):
            ex_name = t.replace("exercise_", "")
            if ex_name in totals["exercises"]:
                totals["exercises"][ex_name].append(data)
        elif t == "pb_pushup":
            totals["personal_bests"]["pushup"] = data.get("max_reps")
        elif t == "pb_situp":
            totals["personal_bests"]["situp"] = data.get("max_reps")
        elif t == "pb_2_4km":
            totals["personal_bests"]["2_4km"] = data.get("timing_seconds")
            totals["personal_bests"]["2_4km_str"] = data.get("timing_str")
    return totals


def _aggregate_exercise_weeks(logs: list, num_weeks: int = 4) -> list:
    """
    Group exercise logs into weekly buckets (newest first).
    Returns list of dicts: {week_start, week_end, pushups, situps, planks, runs}.
    """
    import json as _json
    today = today_sgt()
    weeks = []
    for w in range(num_weeks):
        week_end   = today - timedelta(days=w * 7)
        week_start = week_end - timedelta(days=6)
        pushups, situps, planks, runs = [], [], [], []
        pb_pushup, pb_situp, pb_24km = [], [], []

        for row in logs:
            row_date = row["date"]
            if not isinstance(row_date, date):
                continue
            if not (week_start <= row_date <= week_end):
                continue
            t = row["type"]
            raw = row["data"]
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = {}
            data = raw if isinstance(raw, dict) else {}

            if t == "exercise_pushup":
                pushups.append(data)
            elif t == "exercise_situp":
                situps.append(data)
            elif t == "exercise_plank":
                planks.append(data)
            elif t in ("exercise_run", "exercise_jog"):
                runs.append(data)
            elif t == "pb_pushup":
                pb_pushup.append(data)
            elif t == "pb_situp":
                pb_situp.append(data)
            elif t == "pb_2_4km":
                pb_24km.append(data)

        weeks.append({
            "week_start": week_start,
            "week_end":   week_end,
            "pushups":    pushups,
            "situps":     situps,
            "planks":     planks,
            "runs":       runs,
            "pb_pushup":  pb_pushup,
            "pb_situp":   pb_situp,
            "pb_24km":    pb_24km,
        })
    return weeks


def _format_exercise_weeks_lines(weeks: list) -> list:
    """Return MarkdownV2 lines for the 4-week exercise + PB section, or [] if no data."""
    has_exercise = any(w["pushups"] or w["situps"] or w["planks"] or w["runs"] for w in weeks)
    has_pb = any(w["pb_pushup"] or w["pb_situp"] or w["pb_24km"] for w in weeks)
    if not has_exercise and not has_pb:
        return []

    lines = []

    # ---- Weekly exercise averages ----
    if has_exercise:
        lines += [
            "",
            escape("━━━━━━━━━━━━━━━"),
            "🏋️ *Exercise Progress \\(4 weeks\\)*",
            "",
            escape("Week          | Push-ups   | Sit-ups    | Best Run"),
            escape("--------------|------------|------------|----------"),
        ]
        for w in weeks:
            label = f"{w['week_start'].strftime('%d %b')}-{w['week_end'].strftime('%d %b')}"

            if w["pushups"]:
                avg_r = round(sum(s.get("reps", 0) for s in w["pushups"]) / len(w["pushups"]))
                avg_s = round(sum(s.get("sets", 0) for s in w["pushups"]) / len(w["pushups"]))
                pu_str = f"{avg_r}r x{avg_s}s"
            else:
                pu_str = "-"

            if w["situps"]:
                avg_r = round(sum(s.get("reps", 0) for s in w["situps"]) / len(w["situps"]))
                avg_s = round(sum(s.get("sets", 0) for s in w["situps"]) / len(w["situps"]))
                su_str = f"{avg_r}r x{avg_s}s"
            else:
                su_str = "-"

            if w["runs"]:
                best_sec = min(s.get("timing_seconds", 99999) for s in w["runs"])
                run_str = _seconds_to_mmss(best_sec)
            else:
                run_str = "-"

            row_str = f"{label:<14} | {pu_str:<10} | {su_str:<10} | {run_str}"
            lines.append(escape(row_str))

    # ---- Personal bests per week ----
    if has_pb:
        lines += [
            "",
            escape("━━━━━━━━━━━━━━━"),
            "🏆 *Personal Bests \\(4 weeks\\)*",
            "",
            escape("Week          | Max Push-ups | Max Sit-ups | 2.4km PB"),
            escape("--------------|--------------|-------------|----------"),
        ]
        for w in weeks:
            label = f"{w['week_start'].strftime('%d %b')}-{w['week_end'].strftime('%d %b')}"

            pu_pb = max((d.get("max_reps", 0) for d in w["pb_pushup"]), default=None)
            su_pb = max((d.get("max_reps", 0) for d in w["pb_situp"]), default=None)
            run_pb = min((d.get("timing_seconds", 99999) for d in w["pb_24km"]), default=None)

            pu_str  = f"{pu_pb} reps" if pu_pb is not None else "-"
            su_str  = f"{su_pb} reps" if su_pb is not None else "-"
            run_str = _seconds_to_mmss(run_pb) if run_pb is not None else "-"

            row_str = f"{label:<14} | {pu_str:<12} | {su_str:<11} | {run_str}"
            lines.append(escape(row_str))

    return lines


# ---------------------------------------------------------------------------
# /today formatter
# ---------------------------------------------------------------------------

def format_today_summary(
    user_name: str,
    logs: list,
    prev_weight_kg: Optional[float] = None,
) -> str:
    t = _aggregate_today(logs)
    name_esc  = escape(user_name)
    today_str = escape(today_sgt().strftime("%d %b %Y"))

    # Weight with change indicator
    if t["weight_kg"] is not None:
        w = t["weight_kg"]
        if prev_weight_kg is not None:
            delta = round(w - prev_weight_kg, 2)
            if delta > 0:
                weight_line = escape(f"{w}kg (") + f"↑{escape(str(delta))}kg" + escape(")")
            elif delta < 0:
                weight_line = escape(f"{w}kg (") + f"↓{escape(str(abs(delta)))}kg" + escape(")")
            else:
                weight_line = escape(f"{w}kg (no change)")
        else:
            weight_line = escape(f"{w}kg")
    else:
        weight_line = escape("—")

    steps       = escape(f"{t['steps']:,}") if t["steps"] else escape("—")
    water       = escape(f"{t['water_ml']} ml") if t["water_ml"] else escape("—")
    workout_line = escape(", ".join(t["workouts"])) if t["workouts"] else escape("—")

    lines = [
        f"📊 *Today's Summary — {name_esc}*",
        f"_{today_str}_",
        escape("━━━━━━━━━━━━━━━"),
        f"🍽 Calories: *{escape(str(t['calories']))} kcal* \\({escape(str(t['meal_count']))} meals\\)",
        f"💪 Protein: *{escape(str(round(t['protein'], 1)))}g*",
        f"🥗 Carbs: {escape(str(round(t['carbs'], 1)))}g \\| Fat: {escape(str(round(t['fat'], 1)))}g",
        f"👟 Steps: {steps}",
        f"💧 Water: {water}",
        f"⚖️ Weight: {weight_line}",
        f"🏋️ Workout: {workout_line}",
    ]

    # Specific exercise entries for today
    ex = t["exercises"]
    exercise_lines = []

    if ex["pushup"]:
        detail = " | ".join(f"{s.get('reps', 0)}x{s.get('sets', 0)}" for s in ex["pushup"])
        exercise_lines.append(f"💪 {escape('Push-ups:')} {escape(detail)}")

    if ex["situp"]:
        detail = " | ".join(f"{s.get('reps', 0)}x{s.get('sets', 0)}" for s in ex["situp"])
        exercise_lines.append(f"🔥 {escape('Sit-ups:')} {escape(detail)}")

    if ex["plank"]:
        detail = " | ".join(f"{s.get('reps', 0)}x{s.get('sets', 0)}" for s in ex["plank"])
        exercise_lines.append(f"🧱 {escape('Planks:')} {escape(detail)}")

    runs = ex["run"] + ex["jog"]
    if runs:
        for r in runs:
            dist   = r.get("distance_km", 0)
            timing = r.get("timing_str", "?")
            exercise_lines.append(f"🏃 {escape(f'Run: {dist}km in {timing}')}")

    if exercise_lines:
        lines.append(escape("━━━━━━━━━━━━━━━"))
        lines.extend(exercise_lines)

    # Personal bests logged today
    pb = t["personal_bests"]
    pb_lines = []
    if pb["pushup"] is not None:
        pu_reps = pb["pushup"]
        pb_lines.append(f"💪 {escape(f'Max push-ups: {pu_reps} reps')}")
    if pb["situp"] is not None:
        su_reps = pb["situp"]
        pb_lines.append(f"🔥 {escape(f'Max sit-ups: {su_reps} reps')}")
    if pb["2_4km"] is not None:
        timing_disp = pb["2_4km_str"] or _seconds_to_mmss(pb["2_4km"])
        pb_lines.append(f"🏃 {escape(f'2.4km PB: {timing_disp}')}")
    if pb_lines:
        lines.append(escape("━━━━━━━━━━━━━━━"))
        lines.append("🏆 *Personal Bests \\(today\\)*")
        lines.extend(pb_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Meal photo analysis formatter
# ---------------------------------------------------------------------------

def format_meal_analysis(data: dict) -> str:
    confidence = data.get("confidence", "medium")
    conf_emoji = "⚠️" if confidence == "low" else ("✅" if confidence == "high" else "🔶")

    description = escape(data.get("description", "Unknown meal"))
    cal     = escape(str(data.get("calories", 0)))
    protein = escape(str(data.get("protein", 0)))
    carbs   = escape(str(data.get("carbs", 0)))
    fat     = escape(str(data.get("fat", 0)))
    fiber   = escape(str(data.get("fiber", 0)))
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
# /stats formatter (today's totals per user)
# ---------------------------------------------------------------------------

def format_stats_today(users_logs: dict) -> str:
    """users_logs: mapping of user_name -> list of log rows for today."""
    if not users_logs:
        return escape("No data logged today.")

    lines = ["📊 *Today's Stats*", ""]
    for name, logs in sorted(users_logs.items()):
        t = _aggregate_today(logs)
        name_esc = escape(name)
        cal      = escape(str(t["calories"]))
        protein  = escape(str(round(t["protein"], 1)))
        steps    = escape(f"{t['steps']:,}") if t["steps"] else escape("—")
        weight   = escape(f"{t['weight_kg']}kg") if t["weight_kg"] is not None else escape("—")

        lines.append(f"👤 *{name_esc}*")
        lines.append(
            f"  🍽 {cal} kcal \\| 💪 {protein}g protein \\| 👟 {steps} steps \\| ⚖️ {weight}"
        )

        # Exercise summary for today
        ex = t["exercises"]
        ex_parts = []
        if ex["pushup"]:
            avg_r = round(sum(s.get("reps", 0) for s in ex["pushup"]) / len(ex["pushup"]))
            avg_s = round(sum(s.get("sets", 0) for s in ex["pushup"]) / len(ex["pushup"]))
            ex_parts.append(f"PU {avg_r}x{avg_s}")
        if ex["situp"]:
            avg_r = round(sum(s.get("reps", 0) for s in ex["situp"]) / len(ex["situp"]))
            avg_s = round(sum(s.get("sets", 0) for s in ex["situp"]) / len(ex["situp"]))
            ex_parts.append(f"SU {avg_r}x{avg_s}")
        if ex["plank"]:
            avg_r = round(sum(s.get("reps", 0) for s in ex["plank"]) / len(ex["plank"]))
            ex_parts.append(f"Plank {avg_r}x")
        runs = ex["run"] + ex["jog"]
        if runs:
            best_sec = min(s.get("timing_seconds", 99999) for s in runs)
            ex_parts.append(f"Run {_seconds_to_mmss(best_sec)}")
        if ex_parts:
            lines.append(f"  🏃 {escape(' | '.join(ex_parts))}")

        lines.append("")

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# /report formatter (7-day nutrition table + 4-week exercise breakdown)
# ---------------------------------------------------------------------------

def format_report(user_name: str, logs: list, days: int = 7) -> str:
    """
    Build a day-by-day nutrition table for the past `days` days,
    plus a 4-week exercise progress section.
    `logs` should cover at least 28 days so the exercise section has full data.
    """
    today = today_sgt()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    # Bucket logs by date (nutrition table — last 7 days only)
    by_date: dict = {d: [] for d in dates}
    for row in logs:
        d = row["date"]
        if isinstance(d, date) and d in by_date:
            by_date[d].append(row)

    name_esc = escape(user_name)
    lines = [
        f"📅 *7\\-Day Report — {name_esc}*",
        "",
        escape("Date       | Cal  | Protein | Steps  | Weight | Workout"),
        escape("-----------|------|---------|--------|--------|--------"),
    ]

    for d in dates:
        t = _aggregate_today(by_date[d])
        date_str = d.strftime("%d %b")
        cal     = str(t["calories"]) if t["calories"] else "—"
        protein = f"{round(t['protein'], 1)}g" if t["protein"] else "—"
        steps   = f"{t['steps']:,}" if t["steps"] else "—"
        weight  = f"{t['weight_kg']}kg" if t["weight_kg"] is not None else "—"
        workout = ", ".join(t["workouts"]) if t["workouts"] else "—"
        row_str = f"{date_str:<10} | {cal:<4} | {protein:<7} | {steps:<6} | {weight:<6} | {workout}"
        lines.append(escape(row_str))

    # 4-week exercise section
    weeks = _aggregate_exercise_weeks(logs, num_weeks=4)
    lines.extend(_format_exercise_weeks_lines(weeks))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /meals formatter (today's meals for a user)
# ---------------------------------------------------------------------------

def format_meals_today(user_name: str, meal_logs: list) -> str:
    name_esc  = escape(user_name)
    today_str = escape(today_sgt().strftime("%d %b %Y"))

    if not meal_logs:
        return f"🍽️ *Meals — {name_esc}* \\({today_str}\\)\n\n_{escape('No meals logged today.')}_"

    lines = [f"🍽️ *Meals — {name_esc}* \\({today_str}\\)", ""]
    for i, row in enumerate(meal_logs, 1):
        data    = row["data"] if isinstance(row["data"], dict) else {}
        desc    = escape(data.get("description", "Unknown"))
        cal     = escape(str(data.get("calories", 0)))
        protein = escape(str(data.get("protein", 0)))
        carbs   = escape(str(data.get("carbs", 0)))
        fat     = escape(str(data.get("fat", 0)))
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
    elif log_type == "exercise_pushup":
        return f"✅ Push\\-ups logged: *{escape(str(data.get('reps', 0)))} reps × {escape(str(data.get('sets', 0)))} sets*"
    elif log_type == "exercise_situp":
        return f"✅ Sit\\-ups logged: *{escape(str(data.get('reps', 0)))} reps × {escape(str(data.get('sets', 0)))} sets*"
    elif log_type == "exercise_plank":
        return f"✅ Planks logged: *{escape(str(data.get('reps', 0)))} reps × {escape(str(data.get('sets', 0)))} sets*"
    elif log_type in ("exercise_run", "exercise_jog"):
        label = "Jog" if "jog" in log_type else "Run"
        dist   = escape(str(data.get("distance_km", 0)))
        timing = escape(data.get("timing_str", "?"))
        return f"✅ {label} logged: *{dist}km in {timing}*"
    elif log_type == "pb_pushup":
        return f"🏆 Push\\-up PB logged: *{escape(str(data.get('max_reps', 0)))} reps*"
    elif log_type == "pb_situp":
        return f"🏆 Sit\\-up PB logged: *{escape(str(data.get('max_reps', 0)))} reps*"
    elif log_type == "pb_2_4km":
        timing = escape(data.get("timing_str", "?"))
        return f"🏆 2\\.4km PB logged: *{timing}*"
    return "✅ Logged\\."
