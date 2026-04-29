#!/usr/bin/env python3
"""
Best AI™ — open one AI site in Chrome (persistent profile), submit a text query, save the reply.

Usage (from repo root):
  python3 best_ai/run_platform.py --platform chatgpt --query "Your question"

MVP platforms (browser only): chatgpt, gemini, claude

Environment:
  BEST_AI_USER_DATA_DIR — Playwright persistent Chrome profile (default: ~/.cusear/best_ai_chrome_profile)
  BEST_AI_RUN_ID        — Optional shared folder name under sessions/best_ai/<id>/ for bundle runs
  BEST_AI_HEADLESS      — Set to 1 for headless (many sites block it; default off)

Outputs:
  sessions/best_ai/<run_id>/<platform>.txt
  sessions/best_ai/<run_id>/meta.json (append platform entry)

Also prints a single line to stdout for the Trainer API:
  BEST_AI_SESSION_DIR=<absolute path>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _agency_root() -> Path:
    try:
        from config.local_paths import agency_root

        return agency_root()
    except Exception:
        return Path(__file__).resolve().parent.parent


def _run_id() -> str:
    ext = (os.environ.get("BEST_AI_RUN_ID") or "").strip()
    if ext:
        return re.sub(r"[^\w.\-]+", "_", ext)[:120]
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _session_dir(agency: Path, rid: str) -> Path:
    root = agency / "sessions" / "best_ai" / rid
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _profile_dir() -> Path:
    default = Path.home() / ".cusear" / "best_ai_chrome_profile"
    p = Path(os.environ.get("BEST_AI_USER_DATA_DIR") or default).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


PLATFORMS: dict[str, dict[str, str]] = {
    "chatgpt": {"home": "https://chatgpt.com/"},
    "gemini": {"home": "https://gemini.google.com/app"},
    "claude": {"home": "https://claude.ai/new"},
}


def _fill_prompt(page, query: str) -> None:
    """Focus the main composer and enter ``query`` (best-effort across UIs)."""
    candidates = [
        "textarea#prompt-textarea",
        "textarea[data-testid='chat-input']",
        "div#prompt-textarea[contenteditable='true']",
        "textarea[placeholder*='Ask']",
        "textarea[placeholder*='Message']",
        "textarea[placeholder*='Enter']",
        "form textarea",
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "textarea",
        "div[contenteditable='true']",
    ]
    last_err: Exception | None = None
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.click(timeout=4000)
            time.sleep(0.15)
            loc.fill(query, timeout=15000)
            return
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not find a prompt input ({last_err})")


def _press_send(page) -> None:
    page.keyboard.press("Enter")


def _extract_chatgpt(page) -> str:
    try:
        loc = page.locator('[data-message-author-role="assistant"]').last
        loc.wait_for(state="attached", timeout=5000)
        t = loc.inner_text(timeout=20000)
        return (t or "").strip()
    except Exception:
        return ""


def _extract_gemini(page) -> str:
    for sel in ("model-response message-content", "message-content", "model-response"):
        try:
            loc = page.locator(sel).last
            if loc.count() < 1:
                continue
            t = loc.inner_text(timeout=12000)
            if (t or "").strip():
                return t.strip()
        except Exception:
            continue
    return ""


def _extract_claude(page) -> str:
    for sel in (
        "[data-testid='assistant-message']",
        "div[data-is-streaming='false'] div[class*='prose']",
        "[data-testid='conversation-turn']",
        "article",
    ):
        try:
            loc = page.locator(sel).last
            if loc.count() < 1:
                continue
            t = loc.inner_text(timeout=12000)
            if len((t or "").strip()) > 40:
                return t.strip()
        except Exception:
            continue
    return ""


EXTRACTORS = {
    "chatgpt": _extract_chatgpt,
    "gemini": _extract_gemini,
    "claude": _extract_claude,
}


def _append_meta(sess: Path, platform: str, query: str, ok: bool, note: str = "") -> None:
    meta_path = sess / "meta.json"
    entry = {
        "platform": platform,
        "query": query[:5000],
        "ok": ok,
        "note": note[:2000],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    rows: list[dict] = []
    if meta_path.is_file():
        try:
            rows = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception:
            rows = []
    rows.append(entry)
    meta_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Best AI™ single-platform browser run")
    parser.add_argument(
        "--platform",
        required=True,
        choices=sorted(PLATFORMS.keys()),
        help="AI site to use",
    )
    parser.add_argument("--query", required=True, help="Question / CURRENT_TOPIC text")
    parser.add_argument("--headless", action="store_true", help="Run headless (often blocked by sites)")
    args = parser.parse_args()
    query = (args.query or "").strip()
    if not query:
        print("ERROR: empty --query", file=sys.stderr)
        return 2
    if len(query) > 20000:
        print("ERROR: query too long (max 20000)", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "ERROR: Playwright not installed. Run:\n"
            "  python3 -m pip install -r best_ai/requirements-best-ai.txt\n"
            "  python3 -m playwright install chrome",
            file=sys.stderr,
        )
        return 1

    agency = _agency_root()
    rid = _run_id()
    sess = _session_dir(agency, rid)
    out_txt = sess / f"{args.platform}.txt"
    print(f"BEST_AI_SESSION_DIR={sess}", flush=True)

    headless = bool(args.headless) or (os.environ.get("BEST_AI_HEADLESS") or "").strip() in ("1", "true", "yes")
    profile = _profile_dir()
    meta = PLATFORMS[args.platform]
    extract = EXTRACTORS[args.platform]

    text = ""
    err_note = ""
    try:
        with sync_playwright() as p:
            context = None
            try:
                try:
                    context = p.chromium.launch_persistent_context(
                        str(profile),
                        channel="chrome",
                        headless=headless,
                        viewport={"width": 1360, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                except Exception:
                    context = p.chromium.launch_persistent_context(
                        str(profile),
                        headless=headless,
                        viewport={"width": 1360, "height": 900},
                    )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(120000)
                page.goto(meta["home"], wait_until="domcontentloaded", timeout=120000)
                time.sleep(2.5)
                _fill_prompt(page, query)
                time.sleep(0.25)
                _press_send(page)
                for _ in range(36):
                    time.sleep(5)
                    text = extract(page)
                    if len(text.strip()) > 40:
                        break
                if not text.strip():
                    try:
                        body = page.inner_text("body", timeout=30000)
                        text = (body or "").strip()[-200000:]
                        err_note = "fallback: full body text (selectors missed assistant bubble)"
                    except Exception as exc:
                        err_note = f"no text extracted: {exc}"
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
    except Exception as exc:
        err_note = str(exc)
        text = ""

    out_txt.write_text(text or "", encoding="utf-8")
    ok = len((text or "").strip()) > 0
    _append_meta(sess, args.platform, query, ok, err_note)
    result = {
        "ok": ok,
        "platform": args.platform,
        "chars": len(text or ""),
        "session_dir": str(sess),
        "note": err_note[:500] if err_note else "",
    }
    print(f"BEST_AI_RESULT_JSON={json.dumps(result, separators=(',', ':'))}", flush=True)
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
