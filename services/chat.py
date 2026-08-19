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

# Chatting is "read the snapshot and answer warmly" — it does not need Opus-tier
# reasoning, and every reply is billed at the output rate. Override with the
# CHAT_MODEL env var if you want to trade cost for sharper answers.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5").strip()

# Hard ceiling on a reply. Thinking is disabled for chat, so this bounds the
# entire output cost of a turn — a 3-sentence answer needs nowhere near this.
MAX_REPLY_TOKENS = 600

# Meal times in the snapshot are rendered in Singapore time.
_SGT = timezone(timedelta(hours=8))

# Turns of conversation kept per chat (user + assistant messages combined).
MAX_HISTORY = 6

# Meals listed individually in the snapshot before it collapses to a count.
_MAX_SNAPSHOT_MEALS = 6

SYSTEM_PROMPT = (
    "You are MakanLens, a fitness and nutrition assistant living in a Telegram chat. "
    "Your users are in Singapore, so local food (chicken rice, nasi lemak, kaya toast, "
    "teh tarik) comes up constantly.\n\n"

    "Answer in at most three short sentences. Plain text only, no markdown or bullets; "
    "a little emoji is fine. Get straight to the point.\n\n"

    "A snapshot of what this user has logged is below. Use those exact numbers and never "
    "invent one — if something is not logged, say so. General nutrition and training "
    "questions you may answer from your own knowledge; just be clear when you are "
    "estimating rather than reading their data.\n\n"

    "You cannot write to their log. To record something they use a food photo (or /meal), "
    "or /weight, /steps, /sleep, /water, /energy, /workout. To fix a meal they reply to its "
    "analysis in plain language, e.g. 'add one fried chicken wing'. To review: /today, "
    "/dailymeals, /weeklymeals, /myreport.\n\n"

    "If asked about something unrelated to food, training, or their data, say briefly that "
    "it is not your thing."
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
        # Long meal lists are the one part of the snapshot that can grow without
        # bound, so show the most recent few and collapse the rest to a count.
        shown = meals[-_MAX_SNAPSHOT_MEALS:]
        hidden = len(meals) - len(shown)
        if hidden:
            lines.append(f"Meals ({len(meals)}, {hidden} earlier not listed):")
        else:
            lines.append(f"Meals ({len(meals)}):")
        lines.extend(f"  - {m}" for m in shown)
        lines.append(
            f"Totals: {int(cal)} kcal, {round(pro, 1)}g protein, "
            f"{round(carbs, 1)}g carbs, {round(fat, 1)}g fat"
        )
    else:
        lines.append("Meals: none logged yet")

    # Only spend tokens on metrics that exist; name the rest once, together.
    missing = []
    if steps:
        lines.append(f"Steps: {steps:,}")
    else:
        missing.append("steps")
    if water:
        lines.append(f"Water: {water} ml")
    else:
        missing.append("water")
    if weight is not None:
        lines.append(f"Weight: {weight} kg")
    else:
        missing.append("weight")
    if sleep is not None:
        lines.append(f"Sleep: {sleep} hrs")
    else:
        missing.append("sleep")
    if energy is not None:
        lines.append(f"Energy: {energy}/10")
    else:
        missing.append("energy")
    if workouts:
        lines.append("Workout: " + ", ".join(workouts))
    else:
        missing.append("workout")
    if missing:
        lines.append("Not logged today: " + ", ".join(missing))

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
        # A per-day table costs ~7 lines for information a single line conveys.
        totals = list(by_day.values())
        lines.append("")
        lines.append(
            f"PAST 7 DAYS: {len(totals)} days logged, "
            f"avg {round(sum(totals) / len(totals))} kcal/day "
            f"(low {min(totals)}, high {max(totals)})"
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
            max_tokens=MAX_REPLY_TOKENS,
            system=SYSTEM_PROMPT,
            # Thinking is billed at the output rate and buys nothing here — the
            # answer is read off the snapshot, not reasoned out.
            thinking={"type": "disabled"},
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
