"""
Vision Engine — cusear™ Agent v1.0
Screenshot capture + AI-powered element finder.
Uses Claude Vision (primary) and GPT-4o Vision (fallback).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
SCREENSHOT_DIR = BASE_DIR / "screenshots"
SCREENSHOT_PATH = SCREENSHOT_DIR / "current.png"

# ─── Constants ─────────────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.7
CLAUDE_MODEL = "claude-sonnet-4-6"
GPT_MODEL = "gpt-4o"


# ─── Custom Exceptions ─────────────────────────────────────────────────────────
class LowConfidenceError(Exception):
    """Raised when the vision model confidence is below CONFIDENCE_THRESHOLD."""
    def __init__(self, confidence: float, intent: str) -> None:
        self.confidence = confidence
        self.intent = intent
        super().__init__(
            f"Low confidence ({confidence:.2f}) finding element for intent: '{intent}'"
        )


class VisionEngineError(Exception):
    """General vision engine failure."""


# ─── Screenshot ────────────────────────────────────────────────────────────────
def take_screenshot() -> str:
    """
    Capture a full-screen screenshot, save to screenshots/current.png,
    and return its base64-encoded content.

    Returns:
        Base64 string of the PNG screenshot.

    Raises:
        VisionEngineError: If screenshot capture fails.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        import mss                              # type: ignore
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[0]           # full virtual screen
            raw = sct.grab(monitor)
            mss.tools.to_png(raw.rgb, raw.size, output=str(SCREENSHOT_PATH))
            logger.debug("Screenshot saved → %s", SCREENSHOT_PATH)

    except ImportError:
        # Fallback: try PIL
        try:
            from PIL import ImageGrab           # type: ignore
            img = ImageGrab.grab()
            img.save(str(SCREENSHOT_PATH))
            logger.debug("Screenshot (PIL fallback) saved → %s", SCREENSHOT_PATH)
        except Exception as exc:
            raise VisionEngineError(f"Screenshot failed: {exc}") from exc

    except Exception as exc:
        raise VisionEngineError(f"Screenshot failed: {exc}") from exc

    with SCREENSHOT_PATH.open("rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("utf-8")

    logger.info("Screenshot captured (%d bytes encoded)", len(encoded))
    return encoded


# ─── Vision Prompt ─────────────────────────────────────────────────────────────
_VISION_SYSTEM_PROMPT = (
    "You are a precise GUI element locator. "
    "Given a screenshot and a target intent, return the center pixel coordinates "
    "of the best matching UI element. "
    "Respond ONLY with valid JSON: "
    '{"found": true/false, "x": int, "y": int, "confidence": float 0-1, '
    '"label_found": "text seen", "reasoning": "brief explanation"}. '
    "If not found: {\"found\": false, \"x\": 0, \"y\": 0, \"confidence\": 0, "
    "\"label_found\": \"\", \"reasoning\": \"why not found\"}."
)

def _build_vision_user_message(
    intent: str,
    labels: list[str],
    position_hint: str,
) -> str:
    label_str = ", ".join(f'"{l}"' for l in labels) if labels else "none provided"
    hint_str = position_hint or "anywhere on screen"
    return (
        f"TARGET INTENT: {intent}\n"
        f"POSSIBLE BUTTON LABELS: [{label_str}]\n"
        f"POSITION HINT: {hint_str}\n\n"
        "Find this element in the screenshot and return its exact center pixel "
        "coordinates as JSON."
    )


# ─── Claude Vision ─────────────────────────────────────────────────────────────
def _find_with_claude(
    intent: str,
    labels: list[str],
    position_hint: str,
    screenshot_b64: str,
) -> dict[str, Any]:
    """Call Claude Vision to locate a UI element."""
    import anthropic                            # type: ignore
    import json as _json

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise VisionEngineError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    user_text = _build_vision_user_message(intent, labels, position_hint)

    for attempt in range(1, 4):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                system=_VISION_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": screenshot_b64,
                                },
                            },
                            {"type": "text", "text": user_text},
                        ],
                    }
                ],
            )
            raw = response.content[0].text.strip()
            result: dict[str, Any] = _json.loads(raw)
            result.setdefault("provider", "claude")
            logger.info(
                "Claude Vision result — found=%s confidence=%.2f label='%s'",
                result.get("found"),
                result.get("confidence", 0),
                result.get("label_found", ""),
            )
            return result

        except _json.JSONDecodeError as exc:
            logger.warning("Claude Vision: JSON parse error attempt %d — %s", attempt, exc)
        except Exception as exc:
            logger.warning("Claude Vision: API error attempt %d — %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)

    raise VisionEngineError("Claude Vision failed after 3 attempts")


# ─── GPT-4o Vision Fallback ────────────────────────────────────────────────────
def _find_with_gpt4o(
    intent: str,
    labels: list[str],
    position_hint: str,
    screenshot_b64: str,
) -> dict[str, Any]:
    """Call GPT-4o Vision to locate a UI element (fallback)."""
    import json as _json

    try:
        from openai import OpenAI              # type: ignore
    except ImportError as exc:
        raise VisionEngineError("openai package not installed") from exc

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise VisionEngineError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)
    user_text = _build_vision_user_message(intent, labels, position_hint)

    for attempt in range(1, 4):
        try:
            response = client.chat.completions.create(
                model=GPT_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _VISION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{screenshot_b64}"
                                },
                            },
                            {"type": "text", "text": user_text},
                        ],
                    },
                ],
            )
            raw = response.choices[0].message.content.strip()
            result: dict[str, Any] = _json.loads(raw)
            result.setdefault("provider", "gpt4o")
            logger.info(
                "GPT-4o Vision result — found=%s confidence=%.2f label='%s'",
                result.get("found"),
                result.get("confidence", 0),
                result.get("label_found", ""),
            )
            return result

        except _json.JSONDecodeError as exc:
            logger.warning("GPT-4o Vision: JSON parse error attempt %d — %s", attempt, exc)
        except Exception as exc:
            logger.warning("GPT-4o Vision: API error attempt %d — %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)

    raise VisionEngineError("GPT-4o Vision failed after 3 attempts")


# ─── Public API ────────────────────────────────────────────────────────────────
def find_element(
    intent: str,
    labels: list[str] | None = None,
    position_hint: str = "",
    screenshot_b64: str | None = None,
) -> dict[str, Any]:
    """
    Locate a UI element on the screen using AI vision.

    Args:
        intent:         Natural-language description of the element to find.
        labels:         Known button/label text candidates (helps accuracy).
        position_hint:  Where to look: "top-right", "bottom-center", etc.
        screenshot_b64: Pre-encoded screenshot; captured fresh if None.

    Returns:
        dict with keys:
            found (bool), x (int), y (int), confidence (float),
            label_found (str), reasoning (str), provider (str)

    Raises:
        LowConfidenceError: Confidence below CONFIDENCE_THRESHOLD.
        VisionEngineError:  Both providers failed.
    """
    if screenshot_b64 is None:
        screenshot_b64 = take_screenshot()

    labels = labels or []

    # ── Primary: Claude ────────────────────────────────────────────────────────
    result: dict[str, Any] | None = None
    try:
        result = _find_with_claude(intent, labels, position_hint, screenshot_b64)
    except VisionEngineError as exc:
        logger.warning("Claude Vision unavailable, trying GPT-4o — %s", exc)

    # ── Fallback: GPT-4o ───────────────────────────────────────────────────────
    if result is None:
        try:
            result = _find_with_gpt4o(intent, labels, position_hint, screenshot_b64)
        except VisionEngineError as exc:
            raise VisionEngineError(
                "Both Claude and GPT-4o Vision failed to find element."
            ) from exc

    # ── Confidence gate ────────────────────────────────────────────────────────
    confidence = float(result.get("confidence", 0))
    if confidence < CONFIDENCE_THRESHOLD:
        raise LowConfidenceError(confidence, intent)

    return result
