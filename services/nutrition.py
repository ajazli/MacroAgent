"""
Nutrition analysis service using Claude vision API.
"""

import base64
import json
import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a nutrition analysis assistant. The user has sent a photo of their meal. "
    "Analyze the image and return ONLY a valid JSON object with no extra text, markdown, or explanation. "
    'Schema: { "description": string, "calories": number, "protein_g": number, "carbs_g": number, '
    '"fat_g": number, "fiber_g": number, "confidence": "low"|"medium"|"high", "notes": string }. '
    "Estimate conservatively for home-cooked portions. "
    'If you cannot identify food, return { "error": "Could not identify food in image" }.'
)

MODEL = "claude-sonnet-4-6"


def _get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def analyse_meal_photo(image_bytes: bytes, media_type: str = "image/jpeg") -> Optional[dict]:
    client = _get_client()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=512,
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

    raw_text = response.content[0].text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON for meal analysis: %r", raw_text)
        return None

    return data


CORRECTION_PROMPT = (
    "You are a nutrition data assistant. The user is correcting an inaccurate meal analysis. "
    "Given the original nutrition JSON and the user's correction text, return ONLY a valid JSON object "
    "with the corrected values merged in. Keep any fields the user did not mention unchanged. "
    'Schema: { "description": string, "calories": number, "protein_g": number, "carbs_g": number, '
    '"fat_g": number, "fiber_g": number, "confidence": "low"|"medium"|"high", "notes": string }. '
    "Return only the JSON, no extra text."
)


async def parse_correction(original: dict, correction_text: str) -> Optional[dict]:
    """Use Claude to apply a freeform correction to an existing nutrition dict."""
    client = _get_client()
    prompt = (
        f"Original analysis:\n{json.dumps(original)}\n\n"
        f"User correction: {correction_text}\n\n"
        "Return the corrected JSON."
    )
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=CORRECTION_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("Claude API error during correction parsing: %s", exc)
        return None

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = "\n".join(
            line for line in raw_text.splitlines() if not line.startswith("```")
        ).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON for correction: %r", raw_text)
        return None


def normalise_nutrition(raw: dict) -> dict:
    return {
        "description": raw.get("description", "Unknown meal"),
        "calories": int(raw.get("calories", 0)),
        "protein": round(float(raw.get("protein_g", raw.get("protein", 0))), 1),
        "carbs": round(float(raw.get("carbs_g", raw.get("carbs", 0))), 1),
        "fat": round(float(raw.get("fat_g", raw.get("fat", 0))), 1),
        "fiber": round(float(raw.get("fiber_g", raw.get("fiber", 0))), 1),
        "confidence": raw.get("confidence", "medium"),
        "notes": raw.get("notes", ""),
        "image_url": None,
    }
