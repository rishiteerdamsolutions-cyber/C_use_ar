"""
Training Engine Server — Web Agency Trainer
Run: python3 dashboard.py
Open: http://localhost:7788
"""
import json, os, re, shutil, tempfile, threading, webbrowser, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

try:
    from dotenv import load_dotenv  # type: ignore

    _env_root = Path(__file__).resolve().parent
    load_dotenv(_env_root / ".env.local")
    load_dotenv(_env_root / ".env")
except Exception:
    pass

BASE_DIR        = Path(__file__).parent
WORKFLOWS_DIR   = BASE_DIR / "workflows"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
WORKFLOWS_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)
PORT = 7788

# First token of each “shell” step must match one of these (override via TRAINER_SHELL_ALLOWLIST).
# Covers: git/GitHub, Cursor, OpenAI CLI, Node/npm builds, Vercel, Firebase, Google Cloud, MongoDB.
_TRAINER_SHELL_ALLOWLIST_DEFAULT = (
    "git,gh,cursor,openai,npm,pnpm,yarn,node,vercel,firebase,gcloud,gsutil,mongosh,mongo,atlas"
)

# Click-step vision: OpenAI and/or Anthropic (see analyse_screenshot_for_click).
def _vision_keys_available() -> bool:
    return bool(
        (os.environ.get("OPENAI_API_KEY") or "").strip()
        or (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    )


def _trainer_vision_provider() -> str:
    v = (os.environ.get("TRAINER_VISION_PROVIDER") or "auto").strip().lower()
    return v if v in ("openai", "anthropic", "auto") else "auto"


def _capture_screen_png(path: Path) -> None:
    """Save a full-screen PNG for runtime vision (same monitors as mss/PIL in vision_engine)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        with mss.mss() as sct:
            mon = sct.monitors[0]
            raw = sct.grab(mon)
            mss.tools.to_png(raw.rgb, raw.size, output=str(path))
    except ImportError:
        from PIL import ImageGrab  # type: ignore

        ImageGrab.grab().save(str(path))


def _trainer_use_live_vision_click(step: dict, x: int, y: int) -> bool:
    """
    Use OpenAI/Anthropic on a fresh full-screen capture at run time instead of saved x,y.

    True when:
      - TRAINER_LIVE_VISION_CLICKS=1 — every click step uses live vision
      - step['live_vision'] is true — per-step checkbox in the trainer
      - saved coordinates are (0,0) and a vision API key is set — automatic fallback
    """
    if (os.environ.get("TRAINER_LIVE_VISION_CLICKS") or "").strip().lower() in ("1", "true", "yes"):
        return True
    if step.get("live_vision") or step.get("use_live_vision"):
        return True
    if (x or 0) == 0 and (y or 0) == 0 and _vision_keys_available():
        return True
    return False

# Serialize all workflow JSON read-modify-write (concurrent Add Step clicks used to drop steps).
_WORKFLOW_IO_LOCK = threading.Lock()


def _activate_trainer_target_app_if_configured() -> None:
    """
    Bring TRAINER_ACTIVATE_APP to the foreground (macOS only).

    Used before Type and Click so keystrokes and pixel clicks hit the browser, not the terminal.
    """
    import platform
    import subprocess
    import time as _time

    if platform.system() != "Darwin":
        return
    activate = (os.environ.get("TRAINER_ACTIVATE_APP") or "").strip()
    if not activate:
        return
    subprocess.run(
        ["osascript", "-e", f'tell application "{activate}" to activate'],
        check=False,
        capture_output=True,
    )
    _time.sleep(float((os.environ.get("TRAINER_ACTIVATE_DELAY") or "0.5").strip() or "0.5"))


def _trainer_run_shell_step(step: dict) -> None:
    """
    Run a single argv-only command (no shell=True). Stable alternative to clicking web UIs.

    Requires TRAINER_ALLOW_SHELL=1. First token must match TRAINER_SHELL_ALLOWLIST unless
    TRAINER_SHELL_UNRESTRICTED=1 (disables allowlist — localhost / expert use only).

    Default allowlist: git, gh, cursor, openai, npm, pnpm, yarn, node, vercel, firebase,
    gcloud, gsutil, mongosh, mongo, atlas.
    """
    import shlex
    import subprocess

    if os.environ.get("TRAINER_ALLOW_SHELL", "").strip().lower() not in ("1", "true", "yes"):
        raise RuntimeError(
            "Shell steps are disabled. Set TRAINER_ALLOW_SHELL=1 in .env.local and restart dashboard.py."
        )
    raw = (step.get("shell_command") or "").strip()
    if not raw:
        raise ValueError("shell step has empty shell_command")

    unrestricted = os.environ.get("TRAINER_SHELL_UNRESTRICTED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    try:
        argv = shlex.split(raw, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"invalid shell_command quoting: {exc}") from exc
    if not argv:
        raise ValueError("shell_command parsed to empty argv")

    if not unrestricted:
        allow_raw = (os.environ.get("TRAINER_SHELL_ALLOWLIST") or _TRAINER_SHELL_ALLOWLIST_DEFAULT).strip()
        allowed = {x.strip().lower() for x in allow_raw.split(",") if x.strip()}
        if not allowed:
            allowed = {
                x.strip().lower() for x in _TRAINER_SHELL_ALLOWLIST_DEFAULT.split(",") if x.strip()
            }

        exe = Path(argv[0]).name.lower()
        if os.name == "nt" and exe.endswith(".exe"):
            exe = exe[:-4]
        if exe not in allowed:
            raise RuntimeError(
                f"Command {exe!r} is not allowed. TRAINER_SHELL_ALLOWLIST={sorted(allowed)}. "
                "Add the binary name in .env.local, or set TRAINER_SHELL_UNRESTRICTED=1 (high risk)."
            )

    timeout = float((os.environ.get("TRAINER_SHELL_TIMEOUT") or "180").strip() or "180")
    timeout = max(1.0, min(timeout, 600.0))
    cwd = (os.environ.get("TRAINER_SHELL_CWD") or "").strip() or None

    sn = step.get("step", "?")
    preview = raw if len(raw) <= 140 else raw[:137] + "…"
    print(f"  ▶ Step {sn}: shell  {preview}")

    proc = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if proc.stdout:
        for line in proc.stdout.strip().splitlines()[:20]:
            print(f"      | {line[:240]}")
    if proc.stderr:
        for line in proc.stderr.strip().splitlines()[:20]:
            print(f"      ! {line[:240]}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[:800]
        raise RuntimeError(f"shell exited with code {proc.returncode}: {tail}")
    print(f"  ✓ Step {sn}: shell OK (exit 0)")


def _mac_automation_hint() -> str:
    """What to tell users when clicks/typing do nothing on macOS."""
    return (
        "macOS is probably blocking automation. Open System Settings → Privacy & Security → "
        "Accessibility, and enable the app that actually runs the server — usually Cursor.app "
        "(if you started python3 from Cursor's terminal) or Terminal.app. Also enable Python "
        "if it appears separately in the list. If keys/paste still fail, add the same app under "
        "Input Monitoring. Restart the trainer after toggling. Run: python3 dashboard.py "
        "(avoid double-clicking .py files; that bounces Python in the Dock)."
    )


def _mac_shell_context_hint() -> Optional[str]:
    """Detect editor-integrated terminal — users often enable the wrong .app for Accessibility."""
    term = (os.environ.get("TERM_PROGRAM") or "").strip().lower()
    in_cursor = bool(os.environ.get("CURSOR_TRACE_ID") or os.environ.get("CURSOR_AGENT"))
    if in_cursor or term in ("vscode", "cursor"):
        return (
            "This shell is inside Cursor or VS Code (integrated terminal). "
            "Enable Accessibility (and Input Monitoring) for Cursor.app or Visual Studio Code.app — "
            "enabling Terminal.app alone will NOT fix automation started from here."
        )
    return None


def _mac_typing_troubleshooting() -> str:
    return (
        "Typing still blocked on Mac? Check: (1) Accessibility + Input Monitoring for the app that "
        "started python3 (Cursor if you use its terminal). (2) Secure Input: close ANY password field "
        "or Keychain prompt in any app — it blocks synthetic keys everywhere until dismissed. "
        "(3) Click the target field in a dry run first; the Type step sends Cmd+A then Cmd+V. "
        "(4) Chrome: ensure the address bar or field is focused before Type runs."
    )


def _darwin_pbcopy(text: str) -> None:
    """Put UTF-8 text on the macOS pasteboard (more reliable than pyperclip for paste)."""
    import subprocess

    proc = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pbcopy failed ({proc.returncode}): {err or 'no stderr'}")


def _darwin_select_all_paste_system_events() -> None:
    """Cmd+A then Cmd+V via System Events — works when PyAutoGUI key events are blocked."""
    import subprocess
    import time

    script = (
        'tell application "System Events"\n'
        '  keystroke "a" using command down\n'
        "  delay 0.22\n"
        '  keystroke "v" using command down\n'
        "end tell\n"
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or "osascript System Events keystroke failed")
    time.sleep(0.35)


def _type_via_clipboard_pyautogui(pyautogui_mod, text: str, darwin: bool) -> None:
    """Select-all then paste from clipboard (Cmd/Ctrl+A, Cmd/Ctrl+V)."""
    import time

    if darwin:
        _darwin_pbcopy(text)
    else:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
    time.sleep(0.28)
    if darwin:
        pyautogui_mod.hotkey("command", "a")
    else:
        pyautogui_mod.hotkey("ctrl", "a")
    time.sleep(0.22)
    if darwin:
        pyautogui_mod.hotkey("command", "v")
    else:
        pyautogui_mod.hotkey("ctrl", "v")
    time.sleep(0.4)


def _type_via_write_ascii(pyautogui_mod, text: str, darwin: bool) -> None:
    """Fallback: type printable ASCII with pyautogui (no clipboard). Newlines as Enter."""
    import time

    interval = 0.02
    if darwin:
        pyautogui_mod.hotkey("command", "a")
    else:
        pyautogui_mod.hotkey("ctrl", "a")
    time.sleep(0.15)
    pyautogui_mod.press("backspace")
    time.sleep(0.1)
    for ch in text:
        if ch == "\n":
            pyautogui_mod.press("enter")
            time.sleep(0.05)
        elif ch == "\r":
            continue
        elif ch == "\t":
            pyautogui_mod.press("tab")
            time.sleep(0.03)
        elif 32 <= ord(ch) <= 126:
            pyautogui_mod.write(ch, interval=interval)
        else:
            raise ValueError(f"non-ASCII character in fallback typing: {ch!r}")
    time.sleep(0.2)


def parse_multipart(body: bytes, boundary: str) -> dict:
    result = {}
    sep = ("--" + boundary).encode()
    for part in body.split(sep)[1:]:
        if not part.strip() or part.strip() == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_bytes, _, data = part.partition(b"\r\n\r\n")
        # Strip only trailing CRLF from the part (never strip "-" — can corrupt binary uploads).
        while data.endswith(b"\r\n"):
            data = data[:-2]
        headers = header_bytes.decode(errors="ignore")
        cd = next((l for l in headers.splitlines() if "Content-Disposition" in l), "")
        nm = re.search(r'name="([^"]+)"', cd)
        fn = re.search(r'filename="([^"]+)"', cd)
        if not nm:
            continue
        name = nm.group(1)
        if fn:
            filename = fn.group(1).strip()
            result.setdefault(name, []).append({"filename": filename, "data": data})
        else:
            result[name] = data.decode(errors="utf-8")
    return result


def save_workflow(name, steps):
    """Save workflow JSON from trained steps."""
    import datetime
    wf = {
        "workflow_name": name,
        "total_steps": len(steps),
        "taught_at": datetime.datetime.utcnow().isoformat(),
        "steps": steps,
    }
    path = WORKFLOWS_DIR / f"{name}.json"
    path.write_text(json.dumps(wf, indent=2))
    return path


def run_workflow(name, dry_run=False):
    """Load workflow and replay each step (pyautogui UI actions + optional shell argv steps)."""
    import time, subprocess
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Workflow '{name}' not found")
    wf = json.loads(path.read_text())
    steps = wf.get("steps", [])
    results = []
    for step in steps:
        action = step.get("action_type") or step.get("action", "click")
        x      = step.get("x") or step.get("trained_x", 0)
        y      = step.get("y") or step.get("trained_y", 0)
        desc   = step.get("description", f"Step {step.get('step','')}")
        if dry_run:
            if action == "wait":
                try:
                    wsv = float(step.get("wait_seconds", 2))
                except (TypeError, ValueError):
                    wsv = 2.0
                wsv = max(0.0, min(wsv, 120.0))
                print(f"  [DRY RUN] Step {step.get('step')}: wait {wsv:g}s — {desc}")
            elif action == "click" and _trainer_use_live_vision_click(step, int(x or 0), int(y or 0)):
                print(f"  [DRY RUN] Step {step.get('step')}: click — LIVE VISION (fresh screen + API) — {desc}")
            elif action == "shell":
                sc = (step.get("shell_command") or "").strip() or desc
                print(f"  [DRY RUN] Step {step.get('step')}: shell — {sc[:160]}{'…' if len(sc) > 160 else ''}")
            else:
                print(f"  [DRY RUN] Step {step.get('step')}: {action} — {desc}")
            results.append({"step": step.get("step"), "action": action, "status": "dry_run"})
            time.sleep(0.1)
            continue
        try:
            if action == "shell":
                time.sleep(0.6)
                _trainer_run_shell_step(step)
                results.append({"step": step.get("step"), "action": action, "status": "ok"})
                continue

            import pyautogui
            pyautogui.FAILSAFE = True
            time.sleep(0.6)
            if action == "minimize":
                import platform
                if platform.system() == "Darwin":
                    pyautogui.hotkey("command", "m")
                else:
                    pyautogui.hotkey("win", "down")
                print(f"  ✓ Step {step.get('step')}: Minimized window")
            elif action == "maximize":
                import platform
                if platform.system() == "Darwin":
                    pyautogui.hotkey("ctrl", "command", "f")   # fullscreen on Mac
                else:
                    pyautogui.hotkey("win", "up")              # maximize on Windows
                print(f"  ✓ Step {step.get('step')}: Maximized window")
            elif action == "open_chrome":
                import platform
                sys_name = platform.system()
                if sys_name == "Darwin":
                    subprocess.Popen(["open", "-a", "Google Chrome"])
                elif sys_name == "Windows":
                    subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
                else:  # Linux
                    subprocess.Popen(["google-chrome"])
                time.sleep(1.5)
                print(f"  ✓ Step {step.get('step')}: Opened Chrome")
            elif action == "press_enter":
                pyautogui.press("enter")
                print(f"  ✓ Step {step.get('step')}: Pressed Enter — {desc}")
            elif action == "wait":
                try:
                    ws = float(step.get("wait_seconds", 2))
                except (TypeError, ValueError):
                    ws = 2.0
                ws = max(0.0, min(ws, 120.0))
                print(f"  ✓ Step {step.get('step')}: Wait {ws:g}s — {desc}")
                time.sleep(ws)
            elif action == "copy":
                import platform as _plat

                if _plat.system() == "Darwin":
                    pyautogui.hotkey("command", "c")
                else:
                    pyautogui.hotkey("ctrl", "c")
                time.sleep(0.28)
                print(f"  ✓ Step {step.get('step')}: Copy (selection → clipboard) — {desc}")
            elif action == "paste":
                import platform as _plat

                if _plat.system() == "Darwin":
                    pyautogui.hotkey("command", "v")
                else:
                    pyautogui.hotkey("ctrl", "v")
                time.sleep(0.35)
                print(f"  ✓ Step {step.get('step')}: Paste (clipboard → focus) — {desc}")
            elif action == "open_url":
                import webbrowser

                raw = (step.get("url") or "").strip()
                if not raw:
                    raise ValueError("open_url step missing url")
                url = raw
                if not re.match(r"^[a-zA-Z][-a-zA-Z0-9+.]*:", url):
                    url = "https://" + url.lstrip("/")
                webbrowser.open(url)
                time.sleep(1.0)
                print(f"  ✓ Step {step.get('step')}: Open URL — {url}")
            elif action == "type":
                import platform as _plat

                text = step.get("type_text")
                if text is None:
                    text = step.get("description") or ""
                if not text:
                    raise ValueError("type step has empty type_text")
                sysn = _plat.system()
                darwin = sysn == "Darwin"

                _type_focus_delay = float(
                    (os.environ.get("TRAINER_TYPE_FOCUS_DELAY") or "0").strip() or "0"
                )
                if _type_focus_delay > 0:
                    time.sleep(_type_focus_delay)

                if darwin:
                    _activate_trainer_target_app_if_configured()
                    time.sleep(0.45)

                old_pause = getattr(pyautogui, "PAUSE", 0.1)
                try:
                    pyautogui.PAUSE = 0.05
                    try:
                        _type_via_clipboard_pyautogui(pyautogui, text, darwin)
                    except Exception as paste_err:
                        if darwin and os.environ.get(
                            "TRAINER_NO_OSASCRIPT_TYPE", ""
                        ).strip().lower() not in ("1", "true", "yes"):
                            try:
                                _darwin_pbcopy(text)
                                _darwin_select_all_paste_system_events()
                                print("      (used System Events for Cmd+A / Cmd+V after PyAutoGUI failed)")
                            except Exception as ose:
                                paste_err = ose
                            else:
                                paste_err = None
                        if paste_err is not None:
                            try:
                                text.encode("ascii")
                            except UnicodeEncodeError:
                                raise RuntimeError(
                                    f"Paste typing failed ({paste_err!r}). Text is not ASCII-only, "
                                    "so key-by-key fallback cannot run. Fix macOS Accessibility / paste."
                                ) from paste_err
                            print(f"      (paste failed {paste_err!r}; retrying ASCII keystrokes)")
                            _type_via_write_ascii(pyautogui, text, darwin)
                finally:
                    pyautogui.PAUSE = old_pause

                print(f"  ✓ Step {step.get('step')}: Typed {len(text)} character(s)")
            elif action == "hotkey":
                keys = desc.replace("+", " ").split()
                pyautogui.hotkey(*keys)
                print(f"  ✓ Step {step.get('step')}: Hotkey {desc}")
            else:  # click
                _activate_trainer_target_app_if_configured()
                ix, iy = int(x or 0), int(y or 0)
                use_live = _trainer_use_live_vision_click(step, ix, iy)
                if use_live:
                    if not _vision_keys_available():
                        raise ValueError(
                            "Live vision click needs OPENAI_API_KEY or ANTHROPIC_API_KEY in the environment"
                        )
                    if not (desc or "").strip():
                        raise ValueError("Click step needs a description so vision knows what to find")
                    cap_path = SCREENSHOTS_DIR / "_runtime_vision_last.png"
                    time.sleep(float(os.environ.get("TRAINER_LIVE_VISION_DELAY", "0.35") or "0.35"))
                    _capture_screen_png(cap_path)
                    coords = analyse_screenshot_for_click(cap_path, desc)
                    ix = int(coords.get("x") or 0)
                    iy = int(coords.get("y") or 0)
                    if ix == 0 and iy == 0:
                        raise ValueError(
                            "Live vision returned (0,0) — improve the step description or check the screen"
                        )
                    print(
                        f"  ◆ Step {step.get('step')}: Live vision → ({ix},{iy}) "
                        f"conf={coords.get('confidence', '?')} — {desc}"
                    )
                elif ix == 0 and iy == 0:
                    raise ValueError(
                        "Click has no saved coordinates — enable “Live screen at run” for this step, "
                        "set TRAINER_LIVE_VISION_CLICKS=1, or re-save the step with a vision API key"
                    )
                pyautogui.click(ix, iy)
                if not use_live:
                    print(f"  ✓ Step {step.get('step')}: Clicked ({ix},{iy}) — {desc}")
            results.append({"step": step.get("step"), "action": action, "status": "ok"})
        except Exception as e:
            print(f"  ✗ Step {step.get('step')}: {e}")
            import platform as _pf

            if _pf.system() == "Darwin":
                print(f"      ℹ {_mac_automation_hint()}")
                if action == "type":
                    print(f"      ℹ {_mac_typing_troubleshooting()}")
                    sh = _mac_shell_context_hint()
                    if sh:
                        print(f"      ℹ {sh}")
            results.append({"step": step.get("step"), "action": action, "status": "error", "error": str(e)})
    return results


_CLICK_VISION_PROMPT = (
    "Return JSON only: {\"x\": <pixel_x>, \"y\": <pixel_y>, \"action\": \"click\", \"confidence\": <0-1>}\n"
    "x,y = center of the element to click. No markdown, no extra text."
)


def _parse_click_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    return json.loads(m.group()) if m else {"x": 0, "y": 0, "action": "click", "confidence": 0}


def _analyse_click_openai(image_path: Path, description: str) -> dict:
    """GPT-4o (or TRAINER_OPENAI_VISION_MODEL) → click center from screenshot."""
    import base64

    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY not set")

    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode()
    ext = image_path.suffix.lstrip(".").lower()
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    model = (os.environ.get("TRAINER_OPENAI_VISION_MODEL") or "gpt-4o").strip()
    client = OpenAI(api_key=key)
    user_text = f"This screenshot shows: {description}\n\n{_CLICK_VISION_PROMPT}"
    r = client.chat.completions.create(
        model=model,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )
    raw = (r.choices[0].message.content or "").strip()
    return _parse_click_json(raw)


def _analyse_click_anthropic(image_path: Path, description: str) -> dict:
    """Claude Vision → click center from screenshot."""
    import anthropic
    import base64

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=key)
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode()
    ext = image_path.suffix.lstrip(".")
    media = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else 'png'}"
    model = (os.environ.get("TRAINER_ANTHROPIC_VISION_MODEL") or "claude-3-5-sonnet-20241022").strip()
    msg = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media, "data": img_b64}},
                    {
                        "type": "text",
                        "text": f"This screenshot shows: {description}\n\n{_CLICK_VISION_PROMPT}",
                    },
                ],
            }
        ],
    )
    raw = msg.content[0].text.strip()
    return _parse_click_json(raw)


def analyse_screenshot_for_click(image_path: Path, description: str) -> dict:
    """
    Find click coordinates from a step screenshot using OpenAI and/or Anthropic.

    TRAINER_VISION_PROVIDER:
      - auto (default): OpenAI if OPENAI_API_KEY is set, else Anthropic; on OpenAI failure, Anthropic if set.
      - openai: require OPENAI_API_KEY
      - anthropic: require ANTHROPIC_API_KEY
    """
    prov = _trainer_vision_provider()
    has_oai = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    has_ant = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())

    if prov == "openai":
        if not has_oai:
            raise ValueError("TRAINER_VISION_PROVIDER=openai but OPENAI_API_KEY is not set")
        return _analyse_click_openai(image_path, description)

    if prov == "anthropic":
        if not has_ant:
            raise ValueError("TRAINER_VISION_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        return _analyse_click_anthropic(image_path, description)

    # auto
    if has_oai:
        try:
            return _analyse_click_openai(image_path, description)
        except Exception as e:
            if has_ant:
                print(f"  ⚠ OpenAI vision failed ({e}); trying Anthropic…")
                return _analyse_click_anthropic(image_path, description)
            raise
    if has_ant:
        return _analyse_click_anthropic(image_path, description)
    raise ValueError("Set OPENAI_API_KEY and/or ANTHROPIC_API_KEY for click training")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")

    def _json(self, data, status=200, no_cache: bool = False):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if no_cache:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            html = BASE_DIR / "TRAINER.html"
            body = html.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif p == "/health":
            self._json({"status": "ok"})
        elif p == "/workflows":
            wfs = []
            for f in sorted(WORKFLOWS_DIR.glob("*.json")):
                try:
                    d = json.loads(f.read_text())
                    wfs.append({"name": f.stem, "total_steps": d.get("total_steps", 0)})
                except Exception:
                    pass
            self._json({"workflows": wfs}, no_cache=True)
        elif p.startswith("/workflow/"):
            name = unquote(p[len("/workflow/"):])
            fp = WORKFLOWS_DIR / f"{name}.json"
            self._json(
                json.loads(fp.read_text()) if fp.exists() else {"error": "not found"},
                200 if fp.exists() else 404,
                no_cache=True,
            )
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = self.path.split("?")[0]
        if "/step/" in p and p.startswith("/workflow/"):
            # DELETE /workflow/<name>/step/<num>
            parts = p.split("/step/")
            wf_name = unquote(parts[0][len("/workflow/"):])
            try:
                step_num = int(parts[1])
            except (ValueError, IndexError):
                self._json({"error": "invalid step"}, 400); return
            fp = WORKFLOWS_DIR / f"{wf_name}.json"
            if not fp.exists():
                self._json({"error": "not found"}, 404); return
            with _WORKFLOW_IO_LOCK:
                wf = json.loads(fp.read_text())
                wf["steps"] = [s for s in wf["steps"] if s.get("step") != step_num]
                for i, s in enumerate(wf["steps"], 1):
                    s["step"] = i
                wf["total_steps"] = len(wf["steps"])
                fp.write_text(json.dumps(wf, indent=2))
                n = len(wf["steps"])
            self._json({"deleted": True, "total_steps": n})
        elif p.startswith("/workflow/"):
            name = unquote(p[len("/workflow/"):])
            fp = WORKFLOWS_DIR / f"{name}.json"
            if fp.exists():
                fp.unlink()
                self._json({"deleted": True})
            else:
                self._json({"error": "not found"}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        import traceback
        p       = self.path.split("?")[0]
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        ct      = self.headers.get("Content-Type", "")

        # ── TEACH ────────────────────────────────────────────────────────────
        if p == "/teach":
            try:
                bnd = re.search(r'boundary=([^\s;]+)', ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400); return
                fields       = parse_multipart(body, bnd.group(1))
                wf_name      = fields.get("workflow_name", "untitled").strip()
                instructions = json.loads(fields.get("instructions", "[]"))
                screenshots  = fields.get("screenshots", [])
                if not screenshots:
                    self._json({"error": "no screenshots"}, 400); return

                inst_map = {item["step"]: item["description"] for item in instructions}
                tmp = Path(tempfile.mkdtemp())
                saved = []
                for i, sc in enumerate(screenshots):
                    fname = re.sub(r'[^\w\-_. ]', '_', sc["filename"]) or f"{i+1}.png"
                    fp = tmp / fname
                    fp.write_bytes(sc["data"])
                    saved.append((i + 1, fp, inst_map.get(i + 1, f"Step {i+1}")))

                def _teach():
                    steps = []
                    for step_num, img_path, desc in saved:
                        try:
                            result = analyse_screenshot_for_click(img_path, desc)
                            steps.append({
                                "step":        step_num,
                                "description": desc,
                                "x":           result.get("x", 0),
                                "y":           result.get("y", 0),
                                "action":      result.get("action", "click"),
                                "confidence":  result.get("confidence", 0),
                            })
                            print(f"  ✓ Step {step_num} analysed: ({result.get('x')},{result.get('y')})")
                        except Exception as e:
                            print(f"  ✗ Step {step_num} error: {e}")
                            steps.append({"step": step_num, "description": desc,
                                          "x": 0, "y": 0, "action": "click", "confidence": 0})
                    with _WORKFLOW_IO_LOCK:
                        save_workflow(wf_name, steps)
                    shutil.rmtree(tmp, ignore_errors=True)
                    print(f"  ✓ Workflow '{wf_name}' saved ({len(steps)} steps)")

                threading.Thread(target=_teach, daemon=True).start()
                self._json({"status": "teaching", "workflow_name": wf_name,
                            "total_steps": len(saved)})
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── TEACH ONE STEP ───────────────────────────────────────────────────
        elif p == "/teach/step":
            try:
                bnd = re.search(r'boundary=([^\s;]+)', ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400); return
                fields      = parse_multipart(body, bnd.group(1))
                wf_name     = fields.get("workflow_name", "").strip()
                description = fields.get("description", "").strip()
                action_type = fields.get("action_type", "click").strip()
                type_text   = fields.get("type_text", "")
                url_field   = fields.get("url", "")
                if isinstance(type_text, str):
                    type_text = type_text.replace("\r\n", "\n")
                else:
                    type_text = ""
                if isinstance(url_field, str):
                    open_url = url_field.strip()
                else:
                    open_url = ""
                if not wf_name:
                    self._json({"error": "workflow_name required"}, 400); return
                if action_type == "type" and not type_text:
                    self._json({"error": "type_text required for type action"}, 400); return
                if action_type == "open_url" and not open_url:
                    self._json({"error": "url required for open_url action"}, 400); return
                if action_type == "wait":
                    try:
                        _ws = float(str(fields.get("wait_seconds", "2") or "2").strip())
                    except ValueError:
                        self._json({"error": "wait_seconds must be a number"}, 400); return
                    if not (0.0 <= _ws <= 120.0):
                        self._json({"error": "wait_seconds must be between 0 and 120"}, 400); return
                shell_cmd_f = fields.get("shell_command", "")
                shell_cmd = shell_cmd_f.strip() if isinstance(shell_cmd_f, str) else ""
                if action_type == "shell" and not shell_cmd:
                    self._json({"error": "shell_command required for shell action"}, 400); return

                live_vis = str(fields.get("live_vision", "")).strip().lower() in ("1", "true", "yes")
                screenshots_early = fields.get("screenshot", [])
                if action_type == "click" and not screenshots_early and live_vis and not description:
                    self._json(
                        {"error": "Describe what to click — live vision uses this text at run time"},
                        400,
                    )
                    return
                if action_type == "click" and not screenshots_early and not live_vis:
                    self._json(
                        {
                            "error": "Click step: upload a training screenshot, or enable "
                            "“Live screen at run” to find the target on the real screen when you Run."
                        },
                        400,
                    )
                    return

                with _WORKFLOW_IO_LOCK:
                    # Load or create workflow
                    wf_path = WORKFLOWS_DIR / f"{wf_name}.json"
                    if wf_path.exists():
                        wf = json.loads(wf_path.read_text())
                    else:
                        wf = {"workflow_name": wf_name, "steps": [],
                              "taught_at": datetime.datetime.utcnow().isoformat()}
                    if not isinstance(wf.get("steps"), list):
                        wf["steps"] = []
                    step_num = len(wf["steps"]) + 1

                    step = {
                        "step": step_num,
                        "action_type": action_type,
                        "description": description,
                        "x": 0, "y": 0,
                        "status": "saved"
                    }
                    if action_type == "type":
                        step["type_text"] = type_text
                        preview = type_text.replace("\n", " ").strip()
                        if len(preview) > 100:
                            preview = preview[:97] + "..."
                        step["description"] = preview or "Type text"
                    elif action_type == "open_url":
                        step["url"] = open_url
                        step["description"] = open_url if len(open_url) <= 120 else open_url[:117] + "..."
                    elif action_type == "wait":
                        ws_save = float(str(fields.get("wait_seconds", "2") or "2").strip())
                        step["wait_seconds"] = ws_save
                        note = description.strip()
                        step["description"] = (
                            (f"Wait {ws_save:g}s — {note}" if note else f"Wait {ws_save:g}s")[:220]
                        )
                    elif action_type == "shell":
                        step["shell_command"] = shell_cmd
                        step["description"] = (shell_cmd[:117] + "...") if len(shell_cmd) > 120 else shell_cmd

                    if action_type == "click":
                        step["live_vision"] = live_vis
                        screenshots = fields.get("screenshot", [])
                        if screenshots:
                            img_data = screenshots[0]["data"]
                            img_path = SCREENSHOTS_DIR / f"{wf_name}_step{step_num}.png"
                            img_path.write_bytes(img_data)
                            step["screenshot"] = img_path.name
                            if _vision_keys_available():
                                try:
                                    coords = analyse_screenshot_for_click(img_path, description)
                                    step["x"] = coords.get("x", 0)
                                    step["y"] = coords.get("y", 0)
                                    step["status"] = "analysed"
                                    print(
                                        f"  ✓ Step {step_num} analysed: ({step['x']},{step['y']}) — {description}"
                                    )
                                except Exception as ve:
                                    print(f"  ⚠ Vision failed step {step_num}: {ve}")
                                    step["status"] = "saved_no_vision"
                            else:
                                step["status"] = "saved_no_api_key"
                                print(
                                    f"  ⚠ No OPENAI_API_KEY / ANTHROPIC_API_KEY — step {step_num} "
                                    "saved without coordinates"
                                )
                        else:
                            # live_vis only: no training image — OpenAI finds target on live screen at run
                            step["x"] = 0
                            step["y"] = 0
                            step["status"] = "live_vision_run"
                            print(f"  ✓ Step {step_num} saved: click (live vision at run) — {description}")
                    elif action_type != "click":
                        # Non-click actions don't need coordinates
                        step["status"] = "saved"
                        if action_type == "open_url":
                            print(f"  ✓ Step {step_num} saved: open_url — {open_url}")
                        elif action_type == "wait":
                            print(f"  ✓ Step {step_num} saved: wait {step.get('wait_seconds', '?')}s")
                        elif action_type == "shell":
                            print(f"  ✓ Step {step_num} saved: shell — {step.get('shell_command', '')[:80]}")
                        else:
                            print(f"  ✓ Step {step_num} saved: {action_type} — {description}")

                    wf["steps"].append(step)
                    wf["total_steps"] = len(wf["steps"])
                    wf_path.write_text(json.dumps(wf, indent=2))
                    out = {
                        "success": True, "step": step_num,
                        "total_steps": len(wf["steps"]),
                        "x": step["x"], "y": step["y"],
                        "status": step["status"],
                    }
                self._json(out)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── DELETE ONE STEP ───────────────────────────────────────────────────
        elif p.startswith("/workflow/") and "/step/" in p:
            # handled in do_DELETE
            self._json({"error": "use DELETE method"}, 405)

        # ── RUN ──────────────────────────────────────────────────────────────
        elif p == "/run":
            try:
                data    = json.loads(body)
                name    = data.get("workflow_name", "")
                dry_run = data.get("dry_run", False)
                if not name:
                    self._json({"error": "workflow_name required"}, 400); return
                results = run_workflow(name, dry_run=dry_run)
                self._json({"success": True, "steps": results})
            except FileNotFoundError as e:
                self._json({"error": str(e)}, 404)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    import platform as _platform
    import sys as _sys

    server = HTTPServer(("localhost", PORT), Handler)
    print(f"\n{'━'*50}")
    print(f"  ⚡ Web Agency Trainer  →  http://localhost:{PORT}")
    print(f"  Stop: Ctrl+C")
    _o = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    _a = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    print(
        f"  Click training vision: TRAINER_VISION_PROVIDER={_trainer_vision_provider()} "
        f"· OPENAI_API_KEY={'yes' if _o else 'no'} · ANTHROPIC_API_KEY={'yes' if _a else 'no'}"
    )
    if not _o and not _a:
        print("  ⚠ Set OPENAI_API_KEY and/or ANTHROPIC_API_KEY for vision (training + live-screen clicks).")
    print(
        "  Live vision at run: TRAINER_LIVE_VISION_CLICKS=1 (all clicks) · "
        "TRAINER_LIVE_VISION_DELAY=0.35 (seconds before capture)"
    )
    _sh = os.environ.get("TRAINER_ALLOW_SHELL", "").strip().lower() in ("1", "true", "yes")
    _al = (os.environ.get("TRAINER_SHELL_ALLOWLIST") or _TRAINER_SHELL_ALLOWLIST_DEFAULT).strip()
    _ur = os.environ.get("TRAINER_SHELL_UNRESTRICTED", "").strip().lower() in ("1", "true", "yes")
    print(
        f"  Shell steps: TRAINER_ALLOW_SHELL={'on' if _sh else 'off'}"
        f" · unrestricted={'YES (any binary)' if _ur and _sh else 'no'}"
        f" · allowlist={_al or _TRAINER_SHELL_ALLOWLIST_DEFAULT}"
    )
    if _sh and _ur:
        print(
            "  ⚠ TRAINER_SHELL_UNRESTRICTED=1 — allowlist ignored. Workflows can run arbitrary commands. "
            "Use only on a trusted dev machine."
        )
    if not _sh:
        print(
            "  Tip: TRAINER_ALLOW_SHELL=1 + Shell steps use CLIs (git, cursor, vercel, firebase, …) "
            "instead of fragile browser clicks."
        )
    if _platform.system() == "Darwin":
        print(f"  Python executable: {_sys.executable}")
        print(f"  macOS: {_mac_automation_hint()}")
        ctx = _mac_shell_context_hint()
        if ctx:
            print(f"  {ctx}")
        print(f"  Tip: TRAINER_NO_OPEN_BROWSER=1  → do not auto-open a browser tab on start")
        print(
            "  Tip: TRAINER_ACTIVATE_APP='Google Chrome'  → activate Chrome before each Type and Click "
            "(so URL/Enter and the next click hit the browser, not the terminal)"
        )
    print(f"{'━'*50}\n")
    if os.environ.get("TRAINER_NO_OPEN_BROWSER", "").strip().lower() not in ("1", "true", "yes"):
        try:
            webbrowser.open(f"http://localhost:{PORT}")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
