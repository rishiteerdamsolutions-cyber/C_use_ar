"""
Teach CLI — Autonomous Web Agency Agent v1.0

Interactive command-line interface for teaching the agent new workflows
via numbered screenshots.

Usage:
    python teach/teach_cli.py

Or from main:
    python main.py --teach
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

SCREENSHOTS_INPUT = BASE_DIR / "teach" / "screenshots_input"


# ─── Colours (cross-platform ANSI) ────────────────────────────────────────────
def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

GOLD   = lambda t: _c(t, "33")
GREEN  = lambda t: _c(t, "32")
RED    = lambda t: _c(t, "31")
BOLD   = lambda t: _c(t, "1")
DIM    = lambda t: _c(t, "2")


# ─── Banner ───────────────────────────────────────────────────────────────────
TEACH_BANNER = """
╔══════════════════════════════════════════════════════╗
║   🎓  Workflow Teaching Mode                          ║
║   Teach the agent by showing it screenshots          ║
╚══════════════════════════════════════════════════════╝
"""


def _print_instructions() -> None:
    print(GOLD("HOW IT WORKS:"))
    print("""
  1. Take screenshots of each step you want the agent to learn.
  2. Name them: 1.png, 2.png, 3.png ... (in order)
  3. Drop them into:  teach/screenshots_input/
  4. Run this tool — type a name for the workflow, then describe
     what to do in each screenshot.
  5. The agent analyses every screenshot with Claude Vision
     and saves a replayable workflow JSON.
  6. Next time, just run:  python main.py --run-workflow <name>
""")


def _list_screenshots(folder: Path) -> list[Path]:
    """Return sorted numbered screenshots from the input folder."""
    images: list[tuple[int, Path]] = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        for p in sorted(folder.glob(ext)):
            try:
                images.append((int(p.stem), p))
            except ValueError:
                pass
    images.sort(key=lambda x: x[0])
    return [p for _, p in images]


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N] ").strip().lower()
    return ans in ("y", "yes")


# ─── Main CLI flow ────────────────────────────────────────────────────────────
def run_teach_cli() -> None:
    print(TEACH_BANNER)
    _print_instructions()

    SCREENSHOTS_INPUT.mkdir(parents=True, exist_ok=True)

    # ── Step A: Workflow name ─────────────────────────────────────────────────
    print(BOLD("Step 1 of 3 — Name your workflow"))
    print(DIM("  Examples: deploy_to_vercel, push_github, create_mongo_db\n"))
    while True:
        name = input("  Workflow name: ").strip().lower().replace(" ", "_")
        if name:
            break
        print(RED("  Please enter a name."))

    # Check if already exists
    wf_file = BASE_DIR / "workflows" / f"{name}.json"
    if wf_file.exists():
        if not _confirm(f"\n  Workflow '{name}' already exists. Overwrite?"):
            print("Cancelled.")
            return

    # ── Step B: Detect / confirm screenshots ─────────────────────────────────
    print(f"\n{BOLD('Step 2 of 3 — Screenshots')}")
    print(f"  Looking in: {SCREENSHOTS_INPUT}\n")

    images = _list_screenshots(SCREENSHOTS_INPUT)

    if not images:
        print(RED(f"  No numbered screenshots found in {SCREENSHOTS_INPUT}"))
        print(DIM("  Add 1.png, 2.png, 3.png ... then re-run.\n"))
        return

    print(GREEN(f"  Found {len(images)} screenshot(s):"))
    for img in images:
        print(f"    {img.name}")

    if not _confirm("\n  Use these screenshots?"):
        alt = input("  Enter full path to screenshot folder: ").strip()
        alt_path = Path(alt)
        if not alt_path.exists():
            print(RED("  Folder not found."))
            return
        images = _list_screenshots(alt_path)
        if not images:
            print(RED("  No numbered screenshots in that folder."))
            return

    # ── Step C: Descriptions ──────────────────────────────────────────────────
    print(f"\n{BOLD('Step 3 of 3 — Describe each step')}")
    print(DIM("  Be specific. E.g. 'Click the green Deploy button in the top right'\n"))

    steps = []
    for img in images:
        step_num = int(img.stem)
        print(f"  {GOLD(f'Screenshot {img.name}')}:")
        instr = input(f"  What should the agent do here? ").strip()
        if not instr:
            instr = f"Perform step {step_num} as shown in screenshot"
        steps.append({"screenshot": str(img), "instruction": instr})
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{BOLD('Summary')}")
    print(f"  Workflow name : {GOLD(name)}")
    print(f"  Total steps   : {len(steps)}")
    for i, s in enumerate(steps, 1):
        img_name = Path(s["screenshot"]).name
        print(f"    {i}. [{img_name}]  {s['instruction']}")

    if not _confirm(f"\n  Teach this workflow now? (Claude will analyse each screenshot)"):
        print("Cancelled.")
        return

    # ── Teach ─────────────────────────────────────────────────────────────────
    from teach.screenshot_teacher import teach_from_screenshots
    workflow = teach_from_screenshots(
        workflow_name=name,
        steps=steps,
        screenshot_dir=None,   # steps already have absolute paths
    )

    # ── Done ──────────────────────────────────────────────────────────────────
    print(GREEN(f"\n✅  Workflow '{name}' is ready!"))
    print(f"\n  To replay any time, run:")
    print(GOLD(f"    python main.py --run-workflow {name}"))
    print(f"\n  To replay with variable substitution (e.g. different client name):")
    print(GOLD(f"    python main.py --run-workflow {name} --var CLIENT_NAME=PriyaSalon"))
    print()


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_teach_cli()
