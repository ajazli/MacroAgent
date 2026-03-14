"""
Nutrition analysis service using Google Gemini vision API.
"""

import io
import json
import logging
import os
from typing import Optional

import google.generativeai as genai
import PIL.Image

logger = logging.getLogger(__name__)

PROMPT = (
    "You are a nutrition analysis assistant. Analyze this meal photo and return ONLY a valid JSON object "
    "with no extra text, markdown, or explanation. "
    'Schema: { "description": string, "calories": number, "protein_g": number, "carbs_g": number, '
    '"fat_g": number, "fiber_g": number, "confidence": "low"|"medium"|"high", "notes": string }. '
    "Estimate conservatively for home-cooked portions. "
    'If you cannot identify food, return { "error": "Could not identify food in image" }.'
)

MODEL = "gemini-2.0-flash"


async def analyse_meal_photo(image_bytes: bytes, media_type: str = "image/jpeg") -> Optional[dict]:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(MODEL)

    try:
        img = PIL.Image.open(io.BytesIO(image_bytes))
        response = await model.generate_content_async(
            [PROMPT, img],
            generation_config=genai.GenerationConfig(max_output_tokens=512),
        )
    except Exception as exc:
        logger.error("Gemini API error during meal analysis: %s", exc)
        return {"_debug_error": str(exc)}

    raw_text = response.text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error("Gemini returned non-JSON for meal analysis: %r", raw_text)
        return None

    return data


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
