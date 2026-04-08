"""
GPT ↔ Gemini Validation Loop — Autonomous Web Agency Agent v1.0
GPT-4o refines the prompt; Gemini validates and approves.
Max 3 iterations → best version returned with status.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ─── Status literals ───────────────────────────────────────────────────────────
ValidationStatus = Literal[
    "approved",
    "max_iterations_reached",
    "gpt_failed",
    "gemini_failed",
    "both_approved",
]

# ─── Prompts ───────────────────────────────────────────────────────────────────
_GPT_SYSTEM = (
    "You are an expert web project prompt engineer. "
    "Your job is to refine AI coding prompts to be clear, complete, and production-ready. "
    "Return ONLY the improved prompt text — no preamble, no explanation."
)

_GEMINI_SYSTEM = (
    "You are a strict quality reviewer for AI coding prompts. "
    "Evaluate whether the given prompt is complete and production-ready. "
    "Reply with ONLY valid JSON: "
    '{"verdict": "APPROVED" or "NEEDS_IMPROVEMENT", '
    '"reason": "one-sentence explanation", '
    '"suggestions": ["optional", "list", "of", "improvements"]}'
)

_GPT_REFINE_TEMPLATE = (
    "Original prompt:\n"
    "---\n"
    "{original}\n"
    "---\n\n"
    "Reviewer feedback:\n"
    "{feedback}\n\n"
    "Please improve the prompt based on this feedback. Return only the improved prompt."
)


# ─── GPT-4o call ──────────────────────────────────────────────────────────────
def _call_gpt(prompt_text: str, system: str = _GPT_SYSTEM) -> str:
    """Send a prompt to GPT-4o and return the response text."""
    from openai import OpenAI               # type: ignore

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=api_key)

    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt_text},
                ],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("GPT-4o attempt %d failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)

    raise RuntimeError("GPT-4o failed after 3 attempts")


# ─── Gemini call ──────────────────────────────────────────────────────────────
def _call_gemini(prompt_text: str) -> dict[str, Any]:
    """Send a prompt to Gemini and return parsed JSON verdict."""
    import json as _json

    try:
        import google.generativeai as genai   # type: ignore
    except ImportError as exc:
        raise ImportError(
            "google-generativeai package not installed. "
            "Run: pip install google-generativeai"
        ) from exc

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    full_prompt = (
        f"{_GEMINI_SYSTEM}\n\n"
        f"Prompt to evaluate:\n---\n{prompt_text}\n---"
    )

    for attempt in range(1, 4):
        try:
            resp = model.generate_content(full_prompt)
            raw = resp.text.strip()

            # Strip markdown code blocks if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            return _json.loads(raw)

        except _json.JSONDecodeError as exc:
            logger.warning("Gemini JSON parse error attempt %d: %s", attempt, exc)
        except Exception as exc:
            logger.warning("Gemini attempt %d failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)

    raise RuntimeError("Gemini failed after 3 attempts")


# ─── Public API ────────────────────────────────────────────────────────────────
def validate_prompt(
    initial_prompt: str,
    max_iterations: int = 3,
) -> dict[str, Any]:
    """
    Iteratively refine a coding prompt using GPT-4o + Gemini validation.

    Flow:
        1. GPT-4o refines the prompt.
        2. Gemini validates → APPROVED or NEEDS_IMPROVEMENT.
        3. If NEEDS_IMPROVEMENT and iterations remain, repeat from step 1.
        4. After max_iterations, return the best GPT version.

    Args:
        initial_prompt: Raw user prompt (e.g. "build salon website").
        max_iterations: Maximum GPT↔Gemini cycles before giving up.

    Returns:
        dict:
            validated_prompt (str)  — final prompt text
            iterations       (int)  — how many cycles ran
            status           (str)  — 'approved' | 'max_iterations_reached'
                                       | 'gpt_failed' | 'gemini_failed'
            gemini_feedback  (dict) — last Gemini verdict
    """
    current_prompt = initial_prompt
    last_feedback = ""
    gemini_verdict: dict[str, Any] = {}
    best_gpt_version = initial_prompt

    for iteration in range(1, max_iterations + 1):
        logger.info("Validation iteration %d/%d", iteration, max_iterations)

        # ── Step 1: GPT refines ───────────────────────────────────────────────
        try:
            if iteration == 1:
                gpt_prompt = (
                    f"Improve this web project prompt to be detailed and production-ready:\n\n"
                    f"{current_prompt}"
                )
            else:
                gpt_prompt = _GPT_REFINE_TEMPLATE.format(
                    original=current_prompt,
                    feedback=last_feedback,
                )
            refined = _call_gpt(gpt_prompt)
            current_prompt = refined
            best_gpt_version = refined
            logger.info("GPT refined prompt (%d chars)", len(refined))

        except Exception as exc:
            logger.error("GPT failed on iteration %d: %s", iteration, exc)
            return {
                "validated_prompt": best_gpt_version,
                "iterations":       iteration,
                "status":           "gpt_failed",
                "gemini_feedback":  gemini_verdict,
            }

        # ── Step 2: Gemini validates ──────────────────────────────────────────
        try:
            gemini_verdict = _call_gemini(current_prompt)
            verdict = gemini_verdict.get("verdict", "NEEDS_IMPROVEMENT").upper()
            reason = gemini_verdict.get("reason", "")
            logger.info("Gemini verdict: %s — %s", verdict, reason)

        except Exception as exc:
            logger.error("Gemini failed on iteration %d: %s", iteration, exc)
            return {
                "validated_prompt": best_gpt_version,
                "iterations":       iteration,
                "status":           "gemini_failed",
                "gemini_feedback":  gemini_verdict,
            }

        # ── APPROVED ──────────────────────────────────────────────────────────
        if verdict == "APPROVED":
            logger.info("Prompt approved after %d iteration(s)", iteration)
            return {
                "validated_prompt": current_prompt,
                "iterations":       iteration,
                "status":           "approved",
                "gemini_feedback":  gemini_verdict,
            }

        # ── NEEDS_IMPROVEMENT — collect feedback for next round ───────────────
        suggestions = gemini_verdict.get("suggestions", [])
        last_feedback = reason
        if suggestions:
            last_feedback += "\nSuggestions:\n" + "\n".join(f"- {s}" for s in suggestions)

        logger.info(
            "Prompt needs improvement (iteration %d). Suggestions: %s",
            iteration, suggestions,
        )
        time.sleep(1)   # throttle

    # ── Max iterations reached ─────────────────────────────────────────────────
    logger.warning(
        "Max iterations (%d) reached — returning best GPT version", max_iterations
    )
    return {
        "validated_prompt": best_gpt_version,
        "iterations":       max_iterations,
        "status":           "max_iterations_reached",
        "gemini_feedback":  gemini_verdict,
    }
