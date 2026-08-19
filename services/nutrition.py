"""
Nutrition analysis service using the Claude API.

Three entry points:
  • analyse_meal_photo — vision pass over a meal photo → nutrition JSON
  • parse_correction   — apply a freeform user correction to an existing meal
  • normalise_nutrition — coerce Claude's JSON into our stored log shape
"""

import base64
import json
import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# Vision pass — runs on every photo, so it favours cost/latency.
VISION_MODEL = "claude-sonnet-4-6"
# Correction pass — needs real reasoning (re-estimating macros after an edit).
REASONING_MODEL = "claude-opus-5"

# Backwards-compatible alias (older imports referenced nutrition.MODEL).
MODEL = VISION_MODEL

SYSTEM_PROMPT = (
    "You are a nutrition analysis assistant. The user has sent a photo of their meal. "
    "Analyze the image and return ONLY a valid JSON object with no extra text, markdown, or explanation. "
    'Schema: { "description": string, "calories": number, "protein_g": number, "carbs_g": number, '
    '"fat_g": number, "fiber_g": number, "confidence": "low"|"medium"|"high", "notes": string }. '
    "Keep the description short and food-first — name the dish and its main components "
    '(e.g. "Chicken rice with cucumber"), not a paragraph. '
    "Estimate conservatively for home-cooked portions. "
    'If you cannot identify food, return { "error": "Could not identify food in image" }.'
)

# JSON Schema used to constrain correction output — guarantees parseable JSON.
_NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "description":    {"type": "string"},
        "calories":       {"type": "number"},
        "protein_g":      {"type": "number"},
        "carbs_g":        {"type": "number"},
        "fat_g":          {"type": "number"},
        "fiber_g":        {"type": "number"},
        "confidence":     {"type": "string", "enum": ["low", "medium", "high"]},
        "notes":          {"type": "string"},
        "change_summary": {
            "type": "string",
            "description": (
                "One short sentence describing what changed and the calorie delta, "
                "e.g. 'Added 1 fried chicken wing (+180 kcal)'."
            ),
        },
    },
    "required": [
        "description", "calories", "protein_g", "carbs_g",
        "fat_g", "fiber_g", "confidence", "notes", "change_summary",
    ],
    "additionalProperties": False,
}


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _first_text(response) -> Optional[str]:
    """Return the first text block of a response, or None if it refused / returned nothing."""
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        logger.warning("Claude refused the request: %s", getattr(details, "category", None))
        return None
    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return None


def _strip_code_fence(raw_text: str) -> str:
    """Remove a ```json … ``` wrapper if the model added one."""
    if raw_text.startswith("```"):
        return "\n".join(
            line for line in raw_text.splitlines() if not line.startswith("```")
        ).strip()
    return raw_text


async def analyse_meal_photo(image_bytes: bytes, media_type: str = "image/jpeg") -> Optional[dict]:
    client = _get_client()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = await client.messages.create(
            model=VISION_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Please analyse this meal photo and return the nutrition JSON.",
                        },
                    ],
                }
            ],
        )
    except Exception as exc:
        logger.error("Claude API error during meal analysis: %s", exc)
        return {"_debug_error": str(exc)}

    raw_text = _first_text(response)
    if raw_text is None:
        return None

    try:
        return json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON for meal analysis: %r", raw_text)
        return None


CORRECTION_PROMPT = (
    "You are a nutrition data assistant. A user is correcting a meal analysis they were given. "
    "You receive the current nutrition JSON and the user's correction in plain language. "
    "Work out which kind of correction it is, apply it, and return the FULL updated nutrition JSON "
    "(every field, not just the changed ones).\n\n"

    "ADD AN ITEM — 'add one fried chicken wing', 'I also had a teh tarik', 'plus two eggs'.\n"
    "  Keep everything already in the meal. Estimate the added item's macros and ADD them to the "
    "  existing totals. Extend the description to include the new item.\n\n"

    "REMOVE AN ITEM — 'no rice', 'I didn't eat the egg', 'skip the sauce'.\n"
    "  Subtract that item's estimated macros from the totals and drop it from the description. "
    "  Never let any value go below zero.\n\n"

    "CHANGE A QUANTITY — 'there were 2 wings not 1', 'it was a large portion', 'only half of it'.\n"
    "  Rescale the affected item's macros, then recompute the totals.\n\n"

    "FIX THE FOOD IDENTITY — 'it's satay not rendang', 'that's nasi lemak'.\n"
    "  Re-estimate ALL values from scratch for the correct dish, reusing the original portion size "
    "  as a reference unless the user gives a new one. Replace the description.\n\n"

    "OVERRIDE A VALUE — 'calories should be 350', 'protein is 28g'.\n"
    "  Set exactly the fields the user named and leave every other field untouched.\n\n"

    "Rules:\n"
    "• Totals must always describe the WHOLE meal, never just the change.\n"
    "• Keep the description short and food-first — dish plus main components, not a paragraph.\n"
    "• Set confidence to reflect the corrected estimate: 'high' when the user gave exact numbers, "
    "  'medium' for a clear named item, 'low' when the addition is vague.\n"
    "• change_summary is one short sentence naming what changed and the calorie delta.\n"
    "• If the correction is unintelligible or unrelated to food, return the original values unchanged "
    "  with change_summary set to exactly 'NO_CHANGE'."
)


async def parse_correction(original: dict, correction_text: str) -> Optional[dict]:
    """Apply a freeform correction to an existing nutrition dict.

    Returns the corrected raw dict (including 'change_summary'), or None on failure.
    """
    client = _get_client()
    prompt = (
        f"Current meal analysis:\n{json.dumps(original, sort_keys=True)}\n\n"
        f"User's correction: {correction_text}\n\n"
        "Return the full updated nutrition JSON."
    )
    try:
        response = await client.messages.create(
            model=REASONING_MODEL,
            max_tokens=4096,
            system=CORRECTION_PROMPT,
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _NUTRITION_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Claude API error during correction parsing: %s", exc)
        return None

    raw_text = _first_text(response)
    if raw_text is None:
        return None

    try:
        return json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON for correction: %r", raw_text)
        return None


def normalise_nutrition(raw: dict) -> dict:
    """Coerce Claude's JSON into the shape stored in the logs table."""
    def _num(*keys, default=0.0):
        for key in keys:
            if raw.get(key) is not None:
                try:
                    return max(0.0, float(raw[key]))
                except (TypeError, ValueError):
                    continue
        return default

    return {
        "description": raw.get("description", "Unknown meal"),
        "calories": int(_num("calories")),
        "protein": round(_num("protein_g", "protein"), 1),
        "carbs": round(_num("carbs_g", "carbs"), 1),
        "fat": round(_num("fat_g", "fat"), 1),
        "fiber": round(_num("fiber_g", "fiber"), 1),
        "confidence": raw.get("confidence", "medium"),
        "notes": raw.get("notes", ""),
        "image_url": None,
    }
