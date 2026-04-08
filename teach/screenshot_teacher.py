"""
Screenshot Teacher — Autonomous Web Agency Agent v1.0

You give it numbered screenshots (1.png, 2.png, ...) + a plain-English
instruction for each step.  Claude Vision analyses every screenshot,
understands WHAT element to interact with and HOW, and saves a
fully replayable workflow JSON.

Usage:
    from teach.screenshot_teacher import teach_from_screenshots

    steps = [
        {"screenshot": "1.png", "instruction": "Click the New Project button"},
        {"screenshot": "2.png", "instruction": "Type the project name 'Salon Site'"},
        {"screenshot": "3.png", "instruction": "Click Continue"},
    ]
    workflow = teach_from_screenshots("deploy_to_vercel", steps)
    # → saves workflows/deploy_to_vercel.json
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
SCREENSHOTS_INPUT_DIR = BASE_DIR / "teach" / "screenshots_input"
WORKFLOWS_DIR = BASE_DIR / "workflows"

# ─── Claude model ──────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-sonnet-4-6"


# ─── Encode image to base64 ────────────────────────────────────────────────────
def _encode_image(image_path: Path) -> str:
    with image_path.open("rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _ext_to_mime(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/png"
    )


# ─── Claude Vision analysis ────────────────────────────────────────────────────
_ANALYSIS_SYSTEM = """
You are an expert GUI automation analyst.
Given a screenshot and a plain-English instruction, you must return a JSON object
describing the EXACT automation action to perform.

Return ONLY valid JSON — no markdown, no explanation.

JSON schema:
{
  "action_type":    "click" | "type" | "scroll" | "hotkey" | "wait" | "open_url",
  "intent":         "natural language description of the target element",
  "labels":         ["list", "of", "possible", "button/link/label", "texts"],
  "position_hint":  "top-left | top-right | center | bottom-left | bottom-right | anywhere",
  "trained_x":      512,
  "trained_y":      304,
  "type_text":      "text to type (only when action_type is 'type')",
  "hotkey_keys":    ["ctrl","s"],
  "url":            "https://... (only when action_type is 'open_url')",
  "wait_seconds":   2,
  "clear_first":    true,
  "reasoning":      "one sentence why you chose this action",
  "confidence":     0.95
}

Rules:
- For click: fill in intent, labels, position_hint AND trained_x/trained_y
  (the pixel coordinates of the element's centre in THIS screenshot)
- For type: fill in type_text, set clear_first=true if replacing existing text
- For hotkey: fill in hotkey_keys (e.g. ["ctrl","c"])
- For scroll: fill in position_hint ("down" or "up")
- For wait: fill in wait_seconds
- For open_url: fill in url
- trained_x and trained_y are the EXACT pixel centre of the target element
- Always set confidence between 0 and 1
"""


def _analyse_step_with_claude(
    screenshot_b64: str,
    mime_type: str,
    instruction: str,
    step_number: int,
) -> dict[str, Any]:
    """Ask Claude to analyse one screenshot + instruction → action dict."""
    import anthropic  # type: ignore

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set — run: python main.py --setup")

    client = anthropic.Anthropic(api_key=api_key)

    user_msg = (
        f"Step {step_number} instruction: {instruction}\n\n"
        "Analyse the screenshot and return the JSON action object."
    )

    for attempt in range(1, 4):
        try:
            resp = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=800,
                system=_ANALYSIS_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": screenshot_b64,
                            },
                        },
                        {"type": "text", "text": user_msg},
                    ],
                }],
            )
            raw = resp.content[0].text.strip()
            # Strip markdown fences if Claude added them
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())

        except json.JSONDecodeError as e:
            logger.warning("Step %d: JSON parse error (attempt %d): %s", step_number, attempt, e)
        except Exception as e:
            logger.warning("Step %d: Claude error (attempt %d): %s", step_number, attempt, e)
            if attempt < 3:
                time.sleep(2 ** attempt)

    # Fallback minimal action
    logger.error("Step %d: Analysis failed — storing raw instruction as fallback", step_number)
    return {
        "action_type": "click",
        "intent": instruction,
        "labels": [instruction],
        "position_hint": "anywhere",
        "reasoning": "Fallback — analysis failed",
        "confidence": 0.5,
    }


# ─── Public API ────────────────────────────────────────────────────────────────
def teach_from_screenshots(
    workflow_name: str,
    steps: list[dict[str, Any]],
    screenshot_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Analyse a list of numbered screenshots + instructions and save
    a replayable workflow JSON.

    Args:
        workflow_name:  Short name, e.g. 'deploy_vercel' or 'push_github'.
        steps:          List of dicts, each with:
                            screenshot  (str)  — filename like '1.png' or full path
                            instruction (str)  — plain English: "Click New Project"
        screenshot_dir: Folder containing the screenshots.
                        Defaults to teach/screenshots_input/.

    Returns:
        The full workflow dict (also saved to workflows/<name>.json).

    Example:
        steps = [
            {"screenshot": "1.png", "instruction": "Click New Project button"},
            {"screenshot": "2.png", "instruction": "Type 'salon-website' in the name field"},
            {"screenshot": "3.png", "instruction": "Click the blue Deploy button"},
        ]
        workflow = teach_from_screenshots("vercel_deploy", steps)
    """
    src_dir = screenshot_dir or SCREENSHOTS_INPUT_DIR
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*60}")
    print(f"  Teaching workflow: '{workflow_name}'")
    print(f"  Steps to learn: {len(steps)}")
    print(f"{'═'*60}\n")

    learned_steps: list[dict[str, Any]] = []

    for i, step_def in enumerate(steps, start=1):
        screenshot_name = step_def.get("screenshot", f"{i}.png")
        instruction = step_def.get("instruction", "")

        # Resolve path
        screenshot_path = Path(screenshot_name)
        if not screenshot_path.is_absolute():
            screenshot_path = src_dir / screenshot_name

        if not screenshot_path.exists():
            logger.error("Step %d: Screenshot not found: %s", i, screenshot_path)
            print(f"  [WARN] Step {i}: Screenshot '{screenshot_name}' not found — skipping")
            continue

        print(f"  Step {i}/{len(steps)}: Analysing '{screenshot_name}'")
        print(f"           Instruction: {instruction}")

        b64 = _encode_image(screenshot_path)
        mime = _ext_to_mime(screenshot_path)

        action = _analyse_step_with_claude(b64, mime, instruction, i)

        learned_step = {
            "step":        i,
            "screenshot":  screenshot_name,
            "instruction": instruction,
            "action":      action,
        }
        learned_steps.append(learned_step)

        conf = action.get("confidence", 0)
        act  = action.get("action_type", "?")
        print(f"           → Action: {act}  |  Confidence: {conf:.0%}  ✓\n")

    workflow = {
        "workflow_name":  workflow_name,
        "version":        "1.0",
        "total_steps":    len(learned_steps),
        "taught_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps":          learned_steps,
    }

    out_path = WORKFLOWS_DIR / f"{workflow_name}.json"
    out_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")

    print(f"{'═'*60}")
    print(f"  ✅  Workflow saved → workflows/{workflow_name}.json")
    print(f"  To replay: python main.py --run-workflow {workflow_name}")
    print(f"{'═'*60}\n")

    return workflow


# ─── Load from folder (auto-detect numbered screenshots) ──────────────────────
def teach_from_folder(
    workflow_name: str,
    folder: str | Path,
    instructions: dict[int, str] | None = None,
) -> dict[str, Any]:
    """
    Auto-detect all numbered screenshots in a folder and teach a workflow.

    Screenshots must be named: 1.png, 2.png, 3.jpg, etc.
    Instructions can be provided as {step_number: "instruction text"} or
    entered interactively via CLI.

    Args:
        workflow_name: Name for the saved workflow.
        folder:        Path to folder containing numbered screenshots.
        instructions:  Optional dict {1: "Click ...", 2: "Type ...", ...}
                       If None, prompts user in terminal for each step.

    Returns:
        Workflow dict.
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Screenshot folder not found: {folder}")

    # Find numbered images
    images: list[tuple[int, Path]] = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        for p in sorted(folder.glob(ext)):
            try:
                num = int(p.stem)
                images.append((num, p))
            except ValueError:
                pass  # skip non-numeric filenames

    images.sort(key=lambda x: x[0])

    if not images:
        raise ValueError(
            f"No numbered screenshots found in {folder}. "
            "Name them 1.png, 2.png, 3.png, etc."
        )

    print(f"\nFound {len(images)} screenshots: {[p.name for _, p in images]}")

    steps = []
    for num, img_path in images:
        if instructions and num in instructions:
            instr = instructions[num]
        else:
            instr = input(f"  Step {num} ({img_path.name}) — what should the agent do? ").strip()
            if not instr:
                instr = f"Perform step {num}"

        steps.append({
            "screenshot":  str(img_path),
            "instruction": instr,
        })

    return teach_from_screenshots(
        workflow_name=workflow_name,
        steps=steps,
        screenshot_dir=folder,
    )
