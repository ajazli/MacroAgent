"""
Natural-language conversation service.

Turns a free-form message into a friendly reply that is grounded in whatever the
user has actually logged, so the bot can answer things like "how much protein am I
still short today?" without the user learning a command.

Replies are returned as PLAIN TEXT (no MarkdownV2) — natural language is full of
characters Telegram's MarkdownV2 parser rejects, and escaping them all makes the
reply unreadable.
"""

import json
import logging
import os
from datetime import timedelta, timezone
from typing import Optional

import anthropic

from services.tz import today_sgt

logger = logging.getLogger(__name__)

CHAT_MODEL = "claude-opus-5"

# Meal times in the snapshot are rendered in Singapore time.
_SGT = timezone(timedelta(hours=8))

# Turns of conversation kept per chat (user + assistant messages combined).
MAX_HISTORY = 12

SYSTEM_PROMPT = (
    "You are MakanLens, a friendly fitness and nutrition assistant living in a Telegram chat. "
    "Your users are in Singapore, so local dishes (chicken rice, nasi lemak, mee goreng, "
    "kaya toast, teh tarik) come up constantly — talk about food the way a Singaporean would.\n\n"

    "How to reply:\n"
    "• Keep it short. Two or three sentences is usually right; this is a chat, not an article.\n"
    "• Plain text only. No markdown, no bold, no bullet characters, no headings. "
    "  A couple of emoji are fine.\n"
    "• Be warm and direct. Skip the preamble and answer the question.\n\n"

    "Using their data:\n"
    "• A snapshot of what this user has logged is provided below. Use those exact numbers.\n"
    "• Never invent a number that is not in the snapshot. If something has not been logged, "
    "  say so plainly and tell them how to log it.\n"
    "• General nutrition and training questions are fine to answer from your own knowledge — "
    "  just be clear when you are giving a general estimate rather than reading their data.\n\n"

    "Logging:\n"
    "• You cannot write to their log. When they want something recorded, point them at the command: "
    "  send a food photo (or reply /meal to one) for meals, /weight, /steps, /sleep, /water, "
    "  /energy, /workout for everything else.\n"
    "• To fix a meal you already analysed, they reply to that analysis message with the correction "
    "  in plain language, e.g. 'add one fried chicken wing' or \"it's satay not rendang\".\n"
    "• Useful views: /today, /dailymeals, /weeklymeals, /myreport, /leaderboard.\n\n"

    "Stay on fitness, food, and the tracking data. If asked about something unrelated, "
    "say briefly that it is not your thing and steer back."
)


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _log_data(row: dict) -> dict:
    """Extract a log row's JSONB payload as a dict, tolerating a raw JSON string."""
    raw = row.get("data", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


async def build_user_snapshot(user: dict) -> str:
    """Assemble a compact plain-text summary of what this user has logged.

    Kept small on purpose — it is prepended to every chat turn.
    """
    from services import db

    today = today_sgt()
    lines = [f"User: {user['name']}", f"Today: {today.strftime('%a %d %b %Y')} (Singapore time)"]

    try:
        todays_logs = await db.get_logs_for_user_today(user["id"])
    except Exception:
        logger.exception("Snapshot: could not load today's logs for user %s", user["id"])
        return "\n".join(lines + ["(Their logged data is temporarily unavailable.)"])

    cal = pro = carbs = fat = 0.0
    meals, steps, water = [], 0, 0
    weight = sleep = energy = None
    workouts = []

    for row in todays_logs:
        data = _log_data(row)
        kind = row["type"]
        if kind == "meal":
            cal   += float(data.get("calories", 0) or 0)
            pro   += float(data.get("protein", 0) or 0)
            carbs += float(data.get("carbs", 0) or 0)
            fat   += float(data.get("fat", 0) or 0)
            try:
                when = row["created_at"].astimezone(_SGT).strftime("%H:%M")
            except Exception:
                when = "?"
            meals.append(f"{when} {data.get('description', 'meal')} "
                         f"({int(data.get('calories', 0) or 0)} kcal)")
        elif kind == "steps":
            steps += int(float(data.get("count", 0) or 0))
        elif kind == "water":
            water += int(float(data.get("ml", 0) or 0))
        elif kind == "weight":
            weight = data.get("kg")
        elif kind == "sleep":
            sleep = data.get("hours")
        elif kind == "energy":
            energy = data.get("level")
        elif kind == "workout":
            if data.get("description"):
                workouts.append(data["description"])

    lines.append("")
    lines.append("TODAY SO FAR")
    if meals:
        lines.append(f"Meals ({len(meals)}):")
        lines.extend(f"  - {m}" for m in meals)
        lines.append(
            f"Totals: {int(cal)} kcal, {round(pro, 1)}g protein, "
            f"{round(carbs, 1)}g carbs, {round(fat, 1)}g fat"
        )
    else:
        lines.append("Meals: none logged yet")

    lines.append(f"Steps: {steps:,}" if steps else "Steps: not logged")
    lines.append(f"Water: {water} ml" if water else "Water: not logged")
    lines.append(f"Weight: {weight} kg" if weight is not None else "Weight: not logged today")
    lines.append(f"Sleep: {sleep} hrs" if sleep is not None else "Sleep: not logged")
    lines.append(f"Energy: {energy}/10" if energy is not None else "Energy: not logged")
    if workouts:
        lines.append("Workout: " + ", ".join(workouts))

    # Past week of calories, for trend questions.
    try:
        week_logs = await db.get_logs_for_user_date_range(
            user["id"], today - timedelta(days=6), today, log_type="meal"
        )
    except Exception:
        week_logs = []

    if week_logs:
        by_day: dict = {}
        for row in week_logs:
            by_day.setdefault(row["date"], 0)
            by_day[row["date"]] += int(_log_data(row).get("calories", 0) or 0)
        lines.append("")
        lines.append("PAST 7 DAYS (calories per day)")
        lines.extend(
            f"  {d.strftime('%a %d %b')}: {by_day[d]} kcal" for d in sorted(by_day)
        )

    return "\n".join(lines)



async def generate_reply(
    user: dict,
    message_text: str,
    history: Optional[list] = None,
    is_group: bool = False,
) -> Optional[str]:
    """Produce a conversational reply grounded in the user's logged data.

    history: prior [{'role', 'content'}] turns for this chat (oldest first).
    Returns plain text, or None if the model could not answer.
    """
    client = _get_client()
    snapshot = await build_user_snapshot(user)

    context_note = (
        "This is a group chat — several people log meals here. The snapshot below belongs "
        f"to {user['name']}, the person who just spoke. Address them directly and do not "
        "speculate about anyone else's numbers.\n\n"
        if is_group else
        "This is a one-to-one chat.\n\n"
    )

    messages = list(history or [])
    messages.append({
        "role": "user",
        "content": f"{context_note}{snapshot}\n\n---\n\n{user['name']} says: {message_text}",
    })

    try:
        response = await client.messages.create(
            model=CHAT_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            messages=messages,
        )
    except Exception as exc:
        logger.error("Claude API error during chat reply: %s", exc)
        return None

    if getattr(response, "stop_reason", None) == "refusal":
        logger.warning("Claude refused a chat turn for user %s", user["id"])
        return "I'd rather not get into that one. Ask me about your meals or training instead 🙂"

    for block in response.content:
        if block.type == "text" and block.text.strip():
            return block.text.strip()

    return None


def trim_history(history: list) -> list:
    """Keep conversation memory bounded, always starting on a user turn."""
    trimmed = history[-MAX_HISTORY:]
    while trimmed and trimmed[0]["role"] != "user":
        trimmed.pop(0)
    return trimmed
