"""
Training Engine Server — cusear™
Run: python3 dashboard.py
Open: http://localhost:7788  (public marketing site **only** from ``CUSEAR WEBSITE  UX UI`` when it has a home page; else ``portal/`` + API)
Control Center: http://localhost:7788/trainer
"""
from __future__ import annotations

import errno, json, os, platform, re, shlex, shutil, signal, socket, sys, tempfile, threading, webbrowser, datetime, time, mimetypes, secrets, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse, parse_qs, quote

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

from config.local_paths import agency_root
from cusear.media_folders import (
    apply_calendar_runtime_tokens,
    bootstrap_cusear_content_folders,
    calendar_total_days_env,
    select_calendar_asset_for_upload,
    write_calendar_slot_media,
)
from cusear.storage_vault import (
    PLATFORM_DIR,
    PLAN_DIR,
    bootstrap_storage_vault,
    cleanup_cusear_legacy_top_level,
    ensure_plan_vault,
    list_cusear_legacy_top_level,
    slot_path,
    vault_root,
)

# Last web.whatsapp.com/send URL opened in the current workflow (used to skip a second Chrome navigation
# when notify would open the same chat again).
_TRAINER_LAST_WHATSAPP_WEB_SEND_URL: Optional[str] = None

# Scheduler session start timestamp: used to prevent "catch-up" auto-runs when the tool was closed.
_TRAINER_SCHEDULER_SESSION_STARTED_AT: Optional[datetime.datetime] = None


def _load_env_file_literal(path: Path, *, override: bool) -> None:
    """
    Lightweight .env parser that keeps values literal.

    This avoids edge cases where dotenv interpolation can mangle certain API key strings.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        if not override and key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        os.environ[key] = val


def _app_paths() -> tuple[Path, Path]:
    """
    (writable_root, static_root).
    writable_root: workflows/, screenshots/ — AGENCY_HOME or repo / bundle dir.
    static_root: TRAINER.html from bundle (_MEIPASS) when PyInstaller-frozen.
    """
    writable = agency_root()
    if getattr(sys, "frozen", False):
        return writable, Path(getattr(sys, "_MEIPASS", str(writable)))
    return writable, writable


BASE_DIR, _STATIC_DIR = _app_paths()
WORKFLOWS_DIR   = BASE_DIR / "workflows"
AR_BUNDLES_DIR  = BASE_DIR / "bundles"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
RUN_AUDIT_DIR   = BASE_DIR / "logs" / "workflow_runs"
MEDIA_LIBRARY_DIR = BASE_DIR / "media_library"
MEDIA_IMAGES_DIR = MEDIA_LIBRARY_DIR / "images"
MEDIA_VIDEOS_DIR = MEDIA_LIBRARY_DIR / "videos"
MEDIA_INDEX_FILE = MEDIA_LIBRARY_DIR / "index.json"
WORKFLOWS_DIR.mkdir(exist_ok=True)
AR_BUNDLES_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR.mkdir(exist_ok=True)
RUN_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
if load_dotenv:
    try:
        # Use literal values (no ${...} interpolation); local file should win.
        load_dotenv(BASE_DIR / ".env.local", override=True, interpolate=False)
        load_dotenv(BASE_DIR / ".env", override=False, interpolate=False)
    except Exception:
        pass
# Always apply a literal parse fallback for key material.
_load_env_file_literal(BASE_DIR / ".env.local", override=True)
_load_env_file_literal(BASE_DIR / ".env", override=False)
# Shipped single-AR desktop bundles set CUSEAR_DEFAULT_AR_SLUG — do not allow a stray trainer mode.
if (os.environ.get("CUSEAR_DEFAULT_AR_SLUG") or "").strip():
    os.environ["AGENCY_USER_MODE"] = "consumer"
PORT = 7788

PORTAL_ROOT = (BASE_DIR / "portal").resolve()
# Public marketing site — **only** this folder (see ``vercel.json``). Not repo-root ``cusear-website/``.
MARKETING_UX_ROOT = (BASE_DIR / "CUSEAR WEBSITE  UX UI").resolve()
DOWNLOADS_ROOT = (BASE_DIR / "downloads").resolve()
DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)


def _trainer_html_path() -> Path:
    th = _STATIC_DIR / "TRAINER.html"
    return th if th.is_file() else (BASE_DIR / "TRAINER.html")


def _safe_leaf_file_under(root: Path, leaf: str) -> Optional[Path]:
    """Serve only single-segment names (e.g. pricing.html) under root — no traversal."""
    leaf = (leaf or "").strip().replace("\\", "/")
    if not leaf or "/" in leaf or leaf.startswith("."):
        return None
    if ".." in leaf:
        return None
    try:
        rr = root.resolve()
        cand = (rr / leaf).resolve()
        cand.relative_to(rr)
    except ValueError:
        return None
    return cand if cand.is_file() else None


def _marketing_home_file(ms: Path) -> Optional[Path]:
    """
    Default document for a marketing root: root ``index.html`` if present,
    else ``cusear_prototype.html``, else ``cusear-website/index.html`` (nested layout).
    """
    for rel in ("index.html", "cusear_prototype.html"):
        p = ms / rel
        if p.is_file():
            return p
    nested = ms / "cusear-website" / "index.html"
    return nested if nested.is_file() else None


def _marketing_site_root() -> Optional[Path]:
    """Product marketing pages live only under ``CUSEAR WEBSITE  UX UI`` (nested or root ``index.html``)."""
    r = MARKETING_UX_ROOT
    return r if r.is_dir() and _marketing_home_file(r) is not None else None


def _marketing_content_root(ms: Path) -> Path:
    """Directory that holds ``index.html`` and sibling assets (e.g. ``…/cusear-website`` when nested)."""
    h = _marketing_home_file(ms)
    return h.parent if h is not None else ms


def _safe_site_file_under(root: Path, rel: str) -> Optional[Path]:
    """
    Serve a file under ``root`` using a relative URL path (may include subdirs).
    Rejects traversal and hidden path segments.
    """
    rel_u = unquote((rel or "").strip().replace("\\", "/").lstrip("/"))
    if not rel_u:
        return None
    parts = [x for x in rel_u.split("/") if x]
    for part in parts:
        if part in (".", "..") or part.startswith("."):
            return None
    try:
        rr = root.resolve()
        cand = rr.joinpath(*parts).resolve()
        cand.relative_to(rr)
    except (ValueError, OSError):
        return None
    return cand if cand.is_file() else None


def _send_local_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
    try:
        body = path.read_bytes()
    except OSError:
        handler.send_response(404)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(b"not found")
        return
    ctype, _enc = mimetypes.guess_type(str(path))
    if not ctype:
        ctype = "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    if path.suffix.lower() in (".html", ".htm"):
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        handler.send_header("Pragma", "no-cache")
    else:
        handler.send_header("Cache-Control", "public, max-age=120")
    handler.end_headers()
    handler.wfile.write(body)

# ── WRA Rekky (v2) background recorder ─────────────────────────────────────────
_WRA_REKKY_LOCK = threading.Lock()
_WRA_REKKY_THREAD: Optional[threading.Thread] = None
_WRA_REKKY_STOP = threading.Event()
_WRA_REKKY_STATUS: dict[str, Any] = {
    "running": False,
    "mode": "",
    "workflow_name": "",
    "error": "",
    "saved_path": "",
    "enrich_path": "",
    "enrich_report": {},
}

# ── Control Center (desktop shell): status, engine log, WRA™ run monitor ─────
_CONTROL_DIR = BASE_DIR / "config"
_CONTROL_SETTINGS_PATH = _CONTROL_DIR / "control_center.json"
_CONTROL_ENGINE_LOG = BASE_DIR / "logs" / "control_engine.log"
_WRA_MONITOR_LOCK = threading.Lock()
_WRA_MONITOR: dict[str, Any] = {
    "running": False,
    "workflow_name": "",
    "lucky": "NOT RUN",
    "agami": "IDLE",
    "aha": "IDLE",
    "signals": [],
    "last_result": None,
    "last_error": "",
}
_LAST_LUCKY_REPORT: dict[str, Any] | None = None
_LUCKY_JOB_LOCK = threading.Lock()
_LUCKY_JOB_STATUS: dict[str, Any] = {"running": False, "error": "", "report": None}
_LUCKY_CANCEL = threading.Event()
_CONTROL_ENGINE_STOP_REQUESTED = False

_PLATFORM_REACHABILITY_HOSTS = (
    ("facebook.com", "https://www.facebook.com/"),
    ("instagram.com", "https://www.instagram.com/"),
    ("linkedin.com", "https://www.linkedin.com/"),
    ("x.com", "https://x.com/"),
    ("whatsapp", "https://web.whatsapp.com/"),
)


def _control_default_settings() -> dict[str, Any]:
    return {
        "auto_start_engine": True,
        "check_updates_on_launch": True,
        "lucky_tab_interval_ms": 80,
        "agami_tab_interval_ms": 60,
        "aha_tab_interval_ms": 50,
        "max_seek_forward": 20,
        "max_seek_backward": 5,
        "move_timeout_sec": 15,
        "done_timeout_sec": 30,
        "chrome_path_override": "",
        "platform_facebook_enabled": True,
        "platform_instagram_enabled": True,
        "platform_linkedin_enabled": True,
        "workflows_path": str(WORKFLOWS_DIR.expanduser()),
        "media_path": str((BASE_DIR / "media_library").resolve()),
        "logs_path": str((BASE_DIR / "logs").resolve()),
        "content_path": str((BASE_DIR / "content").resolve()),
        "whatsapp_notify_number": "",
        "company_endpoint": "",
        "license_key": "",
    }


def _load_control_settings() -> dict[str, Any]:
    base = _control_default_settings()
    try:
        _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        if _CONTROL_SETTINGS_PATH.is_file():
            raw = json.loads(_CONTROL_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                base.update({k: raw[k] for k in base if k in raw})
                for k, v in raw.items():
                    if k not in base:
                        base[k] = v
    except Exception:
        pass
    return base


def _save_control_settings(data: dict[str, Any]) -> None:
    _CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    merged = _control_default_settings()
    merged.update(data)
    _CONTROL_SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _apply_control_center_paths() -> None:
    """Apply workflows/media/logs paths from control_center.json (Settings → Paths)."""
    global WORKFLOWS_DIR, MEDIA_LIBRARY_DIR, MEDIA_IMAGES_DIR, MEDIA_VIDEOS_DIR, MEDIA_INDEX_FILE, RUN_AUDIT_DIR
    try:
        s = _load_control_settings()
        wf = str(s.get("workflows_path") or "").strip()
        if wf:
            WORKFLOWS_DIR = Path(wf).expanduser().resolve()
            WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
        md = str(s.get("media_path") or "").strip()
        if md:
            MEDIA_LIBRARY_DIR = Path(md).expanduser().resolve()
            MEDIA_IMAGES_DIR = MEDIA_LIBRARY_DIR / "images"
            MEDIA_VIDEOS_DIR = MEDIA_LIBRARY_DIR / "videos"
            MEDIA_INDEX_FILE = MEDIA_LIBRARY_DIR / "index.json"
            MEDIA_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            MEDIA_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        lg = str(s.get("logs_path") or "").strip()
        if lg:
            logs_root = Path(lg).expanduser().resolve()
            logs_root.mkdir(parents=True, exist_ok=True)
            RUN_AUDIT_DIR = logs_root / "workflow_runs"
            RUN_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


_apply_control_center_paths()


def _read_app_version() -> str:
    try:
        vf = BASE_DIR / "VERSION"
        if vf.is_file():
            return vf.read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        pass
    return "0.0.0"


def _control_append_engine_log(line: str) -> None:
    try:
        _CONTROL_ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(_CONTROL_ENGINE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except Exception:
        pass


def _control_engine_log_tail(n: int = 20) -> list[str]:
    try:
        if not _CONTROL_ENGINE_LOG.is_file():
            return []
        lines = _CONTROL_ENGINE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max(1, min(200, n)) :]
    except Exception:
        return []


def _control_chrome_ok() -> bool:
    try:
        from cusear.engine.preflight import check_chrome

        return bool(check_chrome())
    except Exception:
        return False


def _control_disk_free_gb() -> float:
    try:
        usage = shutil.disk_usage(str(BASE_DIR.resolve()))
        return round(usage.free / (1024**3), 2)
    except Exception:
        return -1.0


def _control_platform_reachability() -> tuple[int, int]:
    """Return (ok_count, total) for quick HTTPS reachability checks."""
    import urllib.request

    ok = 0
    total = len(_PLATFORM_REACHABILITY_HOSTS)
    for _, url in _PLATFORM_REACHABILITY_HOSTS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cusear-control-center/1.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                code = getattr(resp, "status", 200) or 200
                if 200 <= int(code) < 500:
                    ok += 1
        except Exception:
            pass
    return ok, total


def _control_permissions_summary() -> str:
    """Coarse hint for Trainer automation (full OS checks are user-driven)."""
    if platform.system() == "Darwin":
        return "Accessibility + Screen Recording"
    return "Accessibility / UI automation"


def _wra_monitor_reset() -> None:
    with _WRA_MONITOR_LOCK:
        _WRA_MONITOR.update(
            {
                "running": False,
                "workflow_name": "",
                "lucky": "NOT RUN",
                "agami": "IDLE",
                "aha": "IDLE",
                "signals": [],
                "last_result": None,
                "last_error": "",
            }
        )


def _wra_monitor_update(**kwargs: Any) -> None:
    with _WRA_MONITOR_LOCK:
        _WRA_MONITOR.update(kwargs)


def _control_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        files = [p for p in RUN_AUDIT_DIR.glob("*.json") if p.name != "latest.json"]
        files.sort(key=lambda x: x.name, reverse=True)
        for p in files[:limit]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            wf = str(d.get("workflow_name") or "")
            err = (d.get("error_steps") or 0) > 0 or bool(d.get("error"))
            out.append(
                {
                    "workflow_name": wf,
                    "ok": not err,
                    "audited_at": str(d.get("audited_at") or ""),
                    "path": str(p),
                }
            )
    except Exception:
        pass
    return out


def _infer_workflow_engine(d: dict[str, Any]) -> str:
    ex = str(d.get("engine") or "").strip().lower()
    if ex in ("wra_v2", "trainer_v1"):
        return ex
    steps = d.get("steps") or []
    if not isinstance(steps, list):
        return "trainer_v1"

    def _at(s: dict[str, Any]) -> str:
        return str(s.get("action_type") or "")

    # WRA™ v2 tab-navigation / URL flows (Rekky/Lucky/Agami path).
    _WRA_ACTIONS = frozenset(
        ("press_tab", "press_enter", "press_arrow", "open_url")
    )
    # Legacy trainer-only steps (no Rekky enrich button unless hybrid below resolves to WRA).
    _LEGACY_TRAINER = frozenset(
        ("click", "ai_type", "minimize", "open_cursor", "best_ai_capture_slot_from_clipboard")
    )

    has_wra_nav = any(isinstance(s, dict) and _at(s) in _WRA_ACTIONS for s in steps)
    has_legacy = any(isinstance(s, dict) and _at(s) in _LEGACY_TRAINER for s in steps)

    # Hybrid workflows often start with open_chrome + typing the URL, then press_tab / enter.
    # Old logic treated open_chrome as always trainer_v1 on first hit, hiding Enrich Rekky.
    if has_wra_nav:
        return "wra_v2"
    if has_legacy:
        return "trainer_v1"

    has_anchor = any(isinstance(s, dict) and "focus_target" in s for s in steps)
    has_wra_actions = any(
        isinstance(s, dict) and _at(s) in ("press_tab", "press_enter", "press_arrow")
        for s in steps
    )
    if has_anchor and has_wra_actions:
        return "wra_v2"
    return "trainer_v1"


def _workflow_last_run_meta(wf_name: str) -> dict[str, Any]:
    try:
        for f in sorted(RUN_AUDIT_DIR.glob("*.json"), reverse=True):
            if f.name == "latest.json":
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(d.get("workflow_name") or "") != wf_name:
                continue
            err_steps = int(d.get("error_steps") or 0)
            top_err = str(d.get("error") or "").strip()
            ok = err_steps <= 0 and not top_err
            return {
                "last_run": str(d.get("audited_at") or ""),
                "last_result": "success" if ok else "failed",
                "last_ok": ok,
            }
    except Exception:
        pass
    return {"last_run": "", "last_result": "never", "last_ok": False}


def _control_upcoming_scheduled() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for bfp in sorted(AR_BUNDLES_DIR.glob("*.json")):
            try:
                b = _normalize_bundle(json.loads(bfp.read_text(encoding="utf-8")))
            except Exception:
                continue
            sch = b.get("schedule") or {}
            if not sch.get("enabled"):
                continue
            nxt = str(b.get("next_run_at") or "").strip()
            name = str(b.get("display_name") or b.get("slug") or bfp.stem)
            kids = b.get("children") or []
            plat = str(kids[0]) if kids else ""
            rows.append(
                {
                    "bundle_slug": str(b.get("slug") or bfp.stem),
                    "name": name,
                    "platform_workflow": plat,
                    "scheduled_at": nxt or "—",
                }
            )
    except Exception:
        pass
    return rows[:20]


# Tab + arrow steps repeat using `tab_count` (1–200). Arrow steps may use `tab_press_increment`
# with `repeat_scale_campaign_day` for scheduled runs.
_TRAINER_TAB_COUNT_ACTIONS: frozenset[str] = frozenset(
    {
        "press_tab",
        "press_arrow_left",
        "press_arrow_right",
        "press_arrow_up",
        "press_arrow_down",
    }
)
_TRAINER_ARROW_PY_KEYS: dict[str, str] = {
    "press_arrow_left": "left",
    "press_arrow_right": "right",
    "press_arrow_up": "up",
    "press_arrow_down": "down",
}
_TRAINER_ARROW_LABELS: dict[str, str] = {
    "press_arrow_left": "Left Arrow",
    "press_arrow_right": "Right Arrow",
    "press_arrow_up": "Up Arrow",
    "press_arrow_down": "Down Arrow",
}

# Substrings for press_automation_grid_nav (pyautogui.press names).
_GRID_NAV_DIR_TO_PG: dict[str, str] = {
    "right": "right",
    "left": "left",
    "up": "up",
    "down": "down",
}

# Real shortcuts are usually 2–4 keys; allow 6 for rare chords (e.g. several modifiers + a key).
_TRAINER_HOTKEY_MAX_KEYS = 6
_TRAINER_HOTKEY_TOKEN_ALIASES: dict[str, str] = {
    "cmd": "command",
    "commandorcontrol": "command",
    "meta": "command",
    "super": "win",
    "windows": "win",
    "opt": "option",
    "esc": "escape",
    "spacebar": "space",
    "space": "space",
    "return": "return",
    "ret": "return",
}


def _trainer_parse_hotkey_keys_json(raw: str) -> tuple[list[str], Optional[str]]:
    """
    Parse hotkey_keys_json from the trainer multipart form.
    Returns (normalized_pyautogui_key_names, error_message_or_None).
    """
    if not isinstance(raw, str) or not raw.strip():
        return [], "Add at least one key (up to %d)." % _TRAINER_HOTKEY_MAX_KEYS
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], "Invalid hotkey_keys_json"
    if not isinstance(data, list):
        return [], "hotkey_keys_json must be a JSON array of strings"
    keys: list[str] = []
    for item in data:
        if not isinstance(item, str):
            return [], "Each hotkey key must be a string"
        t = item.strip().lower()
        if not t:
            continue
        t = _TRAINER_HOTKEY_TOKEN_ALIASES.get(t, t)
        if len(t) > 24 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", t):
            return [], f"Unsupported hotkey token: {item!r} (use PyAutoGUI names, e.g. shift, command, o)"
        keys.append(t)
    if not keys:
        return [], "Add at least one key (up to %d)." % _TRAINER_HOTKEY_MAX_KEYS
    if len(keys) > _TRAINER_HOTKEY_MAX_KEYS:
        return [], "At most %d keys per shortcut." % _TRAINER_HOTKEY_MAX_KEYS
    return keys, None


def _trainer_hotkey_keys_for_run(step: dict) -> list[str]:
    """Resolve hotkey key list from saved step (prefers hotkey_keys, else description)."""
    hk = step.get("hotkey_keys")
    if isinstance(hk, list) and hk:
        out: list[str] = []
        for item in hk:
            if not isinstance(item, str):
                continue
            t = item.strip().lower()
            if not t:
                continue
            t = _TRAINER_HOTKEY_TOKEN_ALIASES.get(t, t)
            out.append(t)
        if out:
            return out[:_TRAINER_HOTKEY_MAX_KEYS]
    desc = str(step.get("description") or "").strip()
    if not desc:
        return []
    parts = re.split(r"[\s,+]+", desc.replace("⌘", "command"))
    keys = [p.strip().lower() for p in parts if p.strip()]
    if keys and keys[0] == "hotkey":
        keys = keys[1:]
    keys = [_TRAINER_HOTKEY_TOKEN_ALIASES.get(k, k) for k in keys]
    return keys[:_TRAINER_HOTKEY_MAX_KEYS]


def _trainer_automation_run_index_for_grid_nav(runtime_vars: Optional[dict]) -> int:
    """Scheduled run counter (1, 2, …); falls back to campaign day, then 1."""
    rv = runtime_vars or {}
    ar = str(rv.get("CURRENT_AUTOMATION_RUN") or "").strip()
    if ar.isdigit():
        v = int(ar)
        if v >= 1:
            return v
    day_raw = str(rv.get("CURRENT_CAMPAIGN_DAY") or "").strip()
    if day_raw.isdigit():
        v = int(day_raw)
        if v >= 1:
            return v
    return 1


def _trainer_grid_snake_nav_press_plan(run_n: int, cols: int, rows: int) -> list[tuple[str, int]]:
    """
    Instagram-style cumulative thumbnail navigation for one automation run.

    For defaults cols=6, rows=5 (30 runs): run 1 → 1×Right; … run 6 → 6×Right;
    run 7 → 6×Right + Down; run 8 → 6×Right + Down + 1×Left; … run 13 adds a second Down;
    runs 14–18 add 1…5×Right after that prefix; etc., alternating horizontal direction
    each row (boustrophedon), ``rows`` bands, ``cols`` cells per band.
    """
    c = max(2, min(50, int(cols)))
    h = max(1, min(20, int(rows)))
    max_run = h * c
    r = max(1, min(int(run_n), max_run))
    plan: list[tuple[str, int]] = []
    plan.append(("right", min(r, c)))
    for b in range(h - 1):
        plan.append(("down", 1 if r >= (b + 1) * c + 1 else 0))
        arm = "left" if (b % 2) == 0 else "right"
        plan.append((arm, min(max(0, r - ((b + 1) * c + 1)), c - 1)))
    return plan


def _trainer_parse_grid_nav_cols_rows(step: dict) -> tuple[int, int]:
    try:
        cols = int(step.get("grid_nav_cols", 6))
    except (TypeError, ValueError):
        cols = 6
    try:
        rows = int(step.get("grid_nav_rows", 5))
    except (TypeError, ValueError):
        rows = 5
    return max(2, min(50, cols)), max(1, min(20, rows))


def _trainer_apply_automation_run_range_to_step(
    step: dict, fields: dict, old: Optional[dict] = None
) -> Optional[str]:
    """
    Optional automation_run_min / automation_run_max (inclusive scheduled run indices).
    Mutates step; returns error message or None.
    """
    old = old or {}

    def _raw(key: str) -> str:
        if key in fields:
            v = fields.get(key)
            if isinstance(v, str):
                return v.strip()
            return str(v).strip() if v is not None else ""
        v = old.get(key)
        if v is None:
            return ""
        return str(v).strip()

    rmin_s = _raw("automation_run_min")
    rmax_s = _raw("automation_run_max")
    if not rmin_s and not rmax_s:
        step.pop("automation_run_min", None)
        step.pop("automation_run_max", None)
        return None
    rmin: Optional[int] = None
    rmax: Optional[int] = None
    if rmin_s:
        try:
            rmin = int(rmin_s)
        except ValueError:
            return "automation_run_min must be an integer"
        if not (1 <= rmin <= 50000):
            return "automation_run_min must be between 1 and 50000"
    if rmax_s:
        try:
            rmax = int(rmax_s)
        except ValueError:
            return "automation_run_max must be an integer"
        if not (1 <= rmax <= 50000):
            return "automation_run_max must be between 1 and 50000"
    if rmin is not None and rmax is not None and rmin > rmax:
        return "automation_run_min must be <= automation_run_max"
    if rmin is not None:
        step["automation_run_min"] = rmin
    else:
        step.pop("automation_run_min", None)
    if rmax is not None:
        step["automation_run_max"] = rmax
    else:
        step.pop("automation_run_max", None)
    return None


def _trainer_step_skipped_automation_run_range(step: dict, runtime_vars: dict) -> tuple[bool, str]:
    """True if this scheduled automation run is outside the step's optional [min,max] window."""
    rs = str(runtime_vars.get("RUN_SOURCE") or "").strip().lower()
    if rs != "automation":
        return False, ""
    cr_raw = str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip()
    if not cr_raw.isdigit():
        return False, ""
    cr = int(cr_raw)

    def _bound(key: str) -> Optional[int]:
        v = step.get(key)
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n >= 1 else None

    lo = _bound("automation_run_min")
    hi = _bound("automation_run_max")
    if lo is None and hi is None:
        return False, ""
    if lo is not None and cr < lo:
        return True, f"scheduled run {cr} < min {lo}"
    if hi is not None and cr > hi:
        return True, f"scheduled run {cr} > max {hi}"
    return False, ""


def _trainer_parse_tab_press_increment(fields: dict, default: int = 1) -> int:
    raw = fields.get("tab_press_increment", str(default))
    if isinstance(raw, str):
        t = raw.strip()
    else:
        t = str(raw).strip()
    try:
        v = int(t or str(default))
    except ValueError:
        v = default
    return max(1, min(200, v))


def _trainer_repeat_press_count(step: dict, runtime_vars: Optional[dict]) -> int:
    """
    Effective repeat count for Tab / arrow steps.

    When repeat_scale_campaign_day is set on an arrow step, scheduled runs pass
    CURRENT_AUTOMATION_RUN (1, 2, 3, …). Press count is
    min(200, max(1, tab_count + (run - 1) * tab_press_increment)).
    Falls back to CURRENT_CAMPAIGN_DAY as run index if CURRENT_AUTOMATION_RUN is absent.
    Manual runs use the saved tab_count only.
    """
    try:
        base = int(step.get("tab_count", 1))
    except (TypeError, ValueError):
        base = 1
    base = max(1, min(base, 200))
    rv = runtime_vars or {}
    if not step.get("repeat_scale_campaign_day"):
        return base
    try:
        inc = int(step.get("tab_press_increment", 1) or 1)
    except (TypeError, ValueError):
        inc = 1
    inc = max(1, min(200, inc))
    run_n: Optional[int] = None
    ar = str(rv.get("CURRENT_AUTOMATION_RUN") or "").strip()
    if ar.isdigit():
        v = int(ar)
        if v >= 1:
            run_n = v
    if run_n is None:
        day_raw = str(rv.get("CURRENT_CAMPAIGN_DAY") or "").strip()
        if day_raw.isdigit():
            v = int(day_raw)
            if v >= 1:
                run_n = v
    if run_n is None:
        return base
    count = base + (run_n - 1) * inc
    return max(1, min(200, count))


def _maybe_reexec_into_dot_venv() -> None:
    """
    If the repo has .venv/ but the user started `python3 dashboard.py` with system Python,
    re-exec using the venv interpreter so pip-installed deps (e.g. pyautogui) are visible.
    """
    if getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        vpy = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        vpy = BASE_DIR / ".venv" / "bin" / "python3"
        if not vpy.is_file():
            vpy = BASE_DIR / ".venv" / "bin" / "python"
    if not vpy.is_file():
        return
    try:
        if Path(sys.executable).resolve() == vpy.resolve():
            return
    except OSError:
        return
    main_py = Path(__file__).resolve()
    os.execv(str(vpy), [str(vpy), str(main_py), *sys.argv[1:]])


def _trainer_pip_install_requirements_hint() -> str:
    """Copy-paste install line; always uses `pip install` (never bare `pip -r`)."""
    req = BASE_DIR / "requirements.txt"
    return f"{shlex.quote(sys.executable)} -m pip install -r {shlex.quote(str(req))}"


def _trainer_warn_if_pyautogui_missing() -> None:
    try:
        import pyautogui  # noqa: F401
    except ImportError:
        cmd = _trainer_pip_install_requirements_hint()
        print(
            "\n  ✗ pyautogui is missing for this Python interpreter (Trainer replay needs it).\n"
            "  Install deps with:  python3 -m pip install -r requirements.txt\n"
            '  (pip needs the subcommand "install"; "pip -r file.txt" alone is invalid.)\n'
            f"  Full command for this machine:\n      {cmd}\n"
            "  Or from the repo root:  bash START.sh\n"
        )


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
    v = (os.environ.get("TRAINER_VISION_PROVIDER") or "openai").strip().lower()
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


def _release_modifier_keys(pyautogui_mod) -> None:
    """
    Defensive key release to avoid accidental Cmd+Tab / Alt+Tab app switchers.
    """
    for k in ("command", "ctrl", "alt", "option", "shift", "win", "fn"):
        try:
            pyautogui_mod.keyUp(k)
        except Exception:
            pass


def _release_nav_keys(pyautogui_mod) -> None:
    """Release navigation keys that can appear stuck after rapid automation."""
    for k in ("tab", "enter", "return"):
        try:
            pyautogui_mod.keyUp(k)
        except Exception:
            pass


def _trainer_use_live_vision_click(step: dict, x: int, y: int, *, allow_ai: bool = True) -> bool:
    """
    Use OpenAI/Anthropic on a fresh full-screen capture at run time instead of saved x,y.

    True when:
      - TRAINER_LIVE_VISION_CLICKS=1 — every click step uses live vision
      - step['live_vision'] is true — per-step checkbox in the trainer
      - saved coordinates are (0,0) and a vision API key is set — automatic fallback
    """
    if not allow_ai:
        return False
    if (os.environ.get("TRAINER_LIVE_VISION_CLICKS") or "").strip().lower() in ("1", "true", "yes"):
        return True
    if step.get("live_vision") or step.get("use_live_vision"):
        return True
    if (x or 0) == 0 and (y or 0) == 0 and _vision_keys_available():
        return True
    return False

# Serialize all workflow JSON read-modify-write (concurrent Add Step clicks used to drop steps).
_WORKFLOW_IO_LOCK = threading.Lock()
# Serialize all AR bundle JSON read-modify-write.
_BUNDLE_IO_LOCK = threading.Lock()
# Prevent overlapping /run executions that can stack keypress actions.
_RUN_WORKFLOW_LOCK = threading.Lock()
_MEDIA_IO_LOCK = threading.Lock()
_RUN_STOP_EVENT = threading.Event()
_AI_MEDIA_JOBS_LOCK = threading.Lock()
_AI_MEDIA_JOBS: dict[str, dict[str, Any]] = {}
_AI_MEDIA_ACTIVE_JOB_ID: str = ""
_AI_MEDIA_LAST_COMPLETED_JOB_ID: str = ""
_DESKTOP_EXPORT_JOBS_LOCK = threading.Lock()
_DESKTOP_EXPORT_JOBS: dict[str, dict[str, Any]] = {}
_BEST_AI_JOBS_LOCK = threading.Lock()
_BEST_AI_JOBS: dict[str, dict[str, Any]] = {}


def _app_mode() -> str:
    mode = (os.environ.get("AGENCY_USER_MODE") or "trainer").strip().lower()
    return "consumer" if mode == "consumer" else "trainer"


def _is_consumer_mode() -> bool:
    return _app_mode() == "consumer"


def _runtime_project_name(workflow_name: str, runtime_vars: dict) -> str:
    """
    Resolve project/repo name from explicit env first, then inferred runtime values.
    """
    explicit = (os.environ.get("TRAINER_PROJECT_NAME") or "").strip()
    if explicit:
        return explicit
    env_dir = (os.environ.get("TRAINER_PROJECT_DIR") or "").strip()
    if env_dir:
        try:
            return Path(env_dir).expanduser().resolve().name
        except Exception:
            return Path(env_dir).name
    inferred = (runtime_vars.get("PROJECT_FOLDER_NAME") or "").strip()
    if inferred:
        return inferred
    return (workflow_name or "").strip()


def _resolve_runtime_tokens(text: str, workflow_name: str, runtime_vars: dict) -> str:
    """
    Replace reusable workflow tokens with runtime values.

    Supported tokens:
      {{WORKFLOW_NAME}}
      {{PROJECT_FOLDER_NAME}}
      {{LAST_TYPED_TEXT}}
      {{WHATSAPP_COMPLETION_TEXT}}  (after completion_message / completion_link / type_completion_message
        until that step clears it; type_completion_message rebuilds from run results at paste time)
      {{CURRENT_TOPIC}}
      {{TOPIC_SLOT}}  (alias of CURRENT_TOPIC; useful for image prompts)
      {{CURRENT_CAPTION}}
      {{CURRENT_CAPTION_PATH}}
      {{CURRENT_MEDIA_PATH}}
      {{CURRENT_IMAGE_PATH}}
      {{CURRENT_VIDEO_PATH}}
      {{CURRENT_CAMPAIGN_DAY}}  (media-campaign day index when applicable)
      {{CURRENT_AUTOMATION_RUN}}  (1st, 2nd, … scheduled automation run for this workflow)
      {{CURRENT_CALENDAR_DAY}}  (1–N 30-day slot: maps from automation run # or campaign day)
      {{CALENDAR_CORE_IMAGE_PATH}} / {{CALENDAR_HYBRID_IMAGE_PATH}} / {{CALENDAR_AI_IMAGE_PATH}}
      (and _VIDEO_PATH / _TEXT_PATH / _STEM — see cusear/media_folders.apply_calendar_runtime_tokens)
    """
    if not isinstance(text, str) or not text:
        return ""
    out = text
    token_values = {
        "{{WORKFLOW_NAME}}": (workflow_name or "").strip(),
        "{{PROJECT_FOLDER_NAME}}": _runtime_project_name(workflow_name, runtime_vars),
        "{{LAST_TYPED_TEXT}}": (runtime_vars.get("LAST_TYPED_TEXT") or "").strip(),
        "{{CURRENT_TOPIC}}": (runtime_vars.get("CURRENT_TOPIC") or "").strip(),
        "{{TOPIC_SLOT}}": (runtime_vars.get("CURRENT_TOPIC") or "").strip(),
    }
    for key, val in runtime_vars.items():
        token = "{{" + str(key).strip() + "}}"
        if token not in token_values:
            token_values[token] = str(val)
    for token, val in token_values.items():
        out = out.replace(token, val)
    return out


def _activate_trainer_target_app_if_configured() -> None:
    """
    Bring TRAINER_ACTIVATE_APP to the foreground (macOS only).

    Used before Click (and similar) so coordinates land in the intended app, not the terminal.
    Not used before Type / AI Type / type_project_name / type_whatsapp_number / type_completion_message — those steps type into **whatever app
    already has keyboard focus**. To restore the old “activate before every type” behavior, set
    TRAINER_ACTIVATE_APP_BEFORE_TYPE=1 (same TRAINER_ACTIVATE_APP value applies).
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


def _trainer_run_shell_step(step: dict, stop_event: Optional[threading.Event] = None) -> None:
    """
    Run a single argv-only command (no shell=True). Stable alternative to clicking web UIs.

    Requires TRAINER_ALLOW_SHELL=1. First token must match TRAINER_SHELL_ALLOWLIST unless
    TRAINER_SHELL_UNRESTRICTED=1 (disables allowlist — localhost / expert use only).

    Default allowlist: git, gh, cursor, openai, npm, pnpm, yarn, node, vercel, firebase,
    gcloud, gsutil, mongosh, mongo, atlas.
    """
    import shlex
    import subprocess
    import time as _time

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

    proc = subprocess.Popen(
        argv,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    start_ts = _time.time()
    stopped_by_user = False
    while proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            stopped_by_user = True
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            break
        if (_time.time() - start_ts) >= timeout:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise RuntimeError(f"shell step timed out after {int(timeout)}s")
        _time.sleep(0.2)
    out, err = proc.communicate()
    if stopped_by_user:
        raise RuntimeError("Run stopped by user")
    if out:
        for line in out.strip().splitlines()[:20]:
            print(f"      | {line[:240]}")
    if err:
        for line in err.strip().splitlines()[:20]:
            print(f"      ! {line[:240]}")
    if proc.returncode != 0:
        tail = (err or out or "").strip()[:800]
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


def _copy_text_to_clipboard(text: str) -> None:
    """
    Copy text into system clipboard.
    Uses pbcopy on macOS, pyperclip elsewhere.
    """
    if platform.system() == "Darwin":
        _darwin_pbcopy(text)
        return
    import pyperclip  # type: ignore

    pyperclip.copy(text)


def _darwin_pbpaste() -> str:
    """Read UTF-8 text from the macOS pasteboard (pairs with ``pbcopy``)."""
    import subprocess

    proc = subprocess.run(["pbpaste"], capture_output=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


def _read_clipboard_text() -> str:
    """Best-effort read of the system clipboard (macOS ``pbpaste``, else pyperclip)."""
    if platform.system() == "Darwin":
        try:
            return _darwin_pbpaste()
        except Exception:
            return ""
    try:
        import pyperclip  # type: ignore

        return str(pyperclip.paste() or "")
    except Exception:
        return ""


def _trainer_avoid_pyautogui_failsafe(pyautogui_mod) -> None:
    """
    PyAutoGUI raises FailSafeException if the cursor is in a screen corner.
    That aborts pure keyboard steps (e.g. press_enter, press_space in WhatsApp) with a
    misleading error. Nudge the pointer toward the center when it sits in
    a corner safety zone.
    """
    import time

    try:
        w, h = pyautogui_mod.size()
        x, y = pyautogui_mod.position()
        try:
            margin = max(4, int((os.environ.get("TRAINER_FAILSAFE_MARGIN") or "14").strip() or "14"))
        except ValueError:
            margin = 14
        in_corner = (
            (x <= margin and y <= margin)
            or (x >= w - 1 - margin and y <= margin)
            or (x <= margin and y >= h - 1 - margin)
            or (x >= w - 1 - margin and y >= h - 1 - margin)
        )
        if not in_corner:
            return
        cx = max(min(w // 2, w - margin - 1), margin + 1)
        cy = max(min(h // 2, h - margin - 1), margin + 1)
        pyautogui_mod.moveTo(int(cx), int(cy), duration=0.12)
        time.sleep(0.06)
        if (os.environ.get("TRAINER_FAILSAFE_DEBUG") or "").strip().lower() in ("1", "true", "yes"):
            print(f"      (moved mouse away from fail-safe corner: was ({x},{y}))")
    except Exception:
        pass


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


def _darwin_chrome_focus_whatsapp_compose() -> str:
    """
    Activate Chrome and focus the WhatsApp Web message input (footer contenteditable).

    Without this, ``type_completion_message`` / Cmd+V often land on the chat list or
    nowhere because keyboard focus never reached the compose box. Controlled by
    ``TRAINER_WHATSAPP_FOCUS_COMPOSE`` (default on). Returns AppleScript result: ``ok``,
    ``not_whatsapp``, ``no_footer``, ``no_box``, ``skipped``, or an error string.
    """
    if (os.environ.get("TRAINER_WHATSAPP_FOCUS_COMPOSE") or "1").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return "skipped"
    js = (
        "(function(){try{"
        "var u=location.href||'';"
        "if(u.indexOf('web.whatsapp.com')<0)return'not_whatsapp';"
        "var f=document.querySelector('footer');"
        "if(!f)return'no_footer';"
        "var b=f.querySelector('[contenteditable=true]')||f.querySelector('div[role=textbox]');"
        "if(!b)return'no_box';"
        "b.focus();"
        "if(typeof b.click==='function')b.click();"
        "return'ok';"
        "}catch(e){return String(e)}})()"
    )
    try:
        _act_del = float(
            (os.environ.get("TRAINER_WHATSAPP_FOCUS_COMPOSE_ACTIVATE_DELAY") or "0.35").strip() or "0.35"
        )
    except (TypeError, ValueError):
        _act_del = 0.35
    _act_del = max(0.0, min(3.0, _act_del))
    body = (
        'tell application "Google Chrome"\n'
        "    activate\n"
        f"    delay {_act_del}\n"
        f"    set jsStr to {json.dumps(js)}\n"
        "    tell active tab of front window\n"
        "        set r to execute javascript jsStr\n"
        "    end tell\n"
        "end tell\n"
        "return r\n"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-"],
            input=body.encode("utf-8"),
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:
        return f"osascript_error:{exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return err or f"exit_{proc.returncode}"
    return out or "empty"


def _darwin_whatsapp_notify_keys_system_events(
    *, tabs: int, phone_digits: str, step_wait: float, send_only: bool = False
) -> None:
    """
    macOS fallback for WhatsApp notify when PyAutoGUI is unavailable.
    ``step_wait`` seconds of delay after each major step (same as ``TRAINER_WHATSAPP_STEP_WAIT_SEC``).

    ``send_only=True``: text was already prefilled in the /send URL, focus is in the compose box —
    just press Return once to send (no Tab, no retyping the phone, which would clear the draft).
    """
    import subprocess
    import time

    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    pd = re.sub(r"\D+", "", str(phone_digits or ""))
    if not send_only and not pd:
        raise ValueError("empty phone digits for WhatsApp notify")

    d = max(0.0, min(120.0, float(step_wait or 0)))
    n_tab = max(0, min(int(tabs), 40))
    try:
        act = float((os.environ.get("TRAINER_WHATSAPP_OSASCRIPT_CHROME_ACTIVATE_SEC") or "0.55").strip() or "0.55")
    except (TypeError, ValueError):
        act = 0.55
    act = max(0.0, min(3.0, act))
    # Bring Chrome to front *before* System Events keystrokes (WhatsApp Web refresh often steals focus from Chrome).
    script: list[str] = [
        'tell application "Google Chrome" to activate',
        f"delay {act:.3f}",
        'tell application "System Events"',
    ]
    if d > 0:
        script.append(f"  delay {d:.3f}")
    if send_only:
        script.append("  key code 36")  # Return — send prefilled message
    else:
        for _ in range(n_tab):
            script.append("  key code 48")  # Tab
            script.append("  delay 0.12")
        if d > 0:
            script.append(f"  delay {d:.3f}")
        script.append(f'  keystroke "{_esc(pd)}"')
        if d > 0:
            script.append(f"  delay {d:.3f}")
        script.append("  key code 36")  # Return
        if d > 0:
            script.append(f"  delay {d:.3f}")
        script.append("  key code 36")  # Return
    script.append("end tell")
    proc = subprocess.run(
        ["osascript", "-e", "\n".join(script)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or "osascript System Events WhatsApp notify keys failed")
    time.sleep(0.25)


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


def _darwin_paste_only_system_events() -> None:
    """Cmd+V only via System Events (no Select-All)."""
    import subprocess
    import time

    script = 'tell application "System Events" to keystroke "v" using command down\n'
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or "osascript System Events Cmd+V failed")
    time.sleep(0.35)


def _type_via_clipboard_paste_only_pyautogui(pyautogui_mod, text: str, darwin: bool) -> None:
    """
    Put ``text`` on the clipboard, then paste with Cmd/Ctrl+V **only** (no Select-All).

    WhatsApp Web's chat composer is a ``contenteditable``; Cmd+A often selects the wrong
    scope so the following paste never appears in the input. Use this for
    ``type_completion_message`` when focus is already in the message box.
    """
    import time

    if darwin:
        _darwin_pbcopy(text)
    else:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
    time.sleep(0.3)
    if darwin:
        pyautogui_mod.hotkey("command", "v")
    else:
        pyautogui_mod.hotkey("ctrl", "v")
    time.sleep(0.45)


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


def _generate_ai_type_text(
    prompt: str, workflow_name: str, runtime_vars: dict, preferred_model: str = ""
) -> str:
    """
    Generate field text from a natural-language instruction using OpenAI.
    """
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("AI Type needs OPENAI_API_KEY in the environment")

    resolved_prompt = _resolve_runtime_tokens(str(prompt or ""), workflow_name, runtime_vars).strip()
    if not resolved_prompt:
        raise ValueError("ai_type step has empty ai_prompt")
    prompt_l = resolved_prompt.lower()
    wf_l = (workflow_name or "").strip().lower()
    if "linkedin" in prompt_l or "linkedin" in wf_l:
        # Quality guard for social posting: strong structure, no cringe meta language.
        resolved_prompt = (
            f"{resolved_prompt}\n\n"
            "Writing rules for output:\n"
            "- Write a polished LinkedIn post in natural human voice.\n"
            "- Use: strong hook line, 3-5 short value lines, one practical takeaway, and 3-5 relevant hashtags.\n"
            "- Keep it concise and readable on mobile.\n"
            "- Do NOT mention words/phrases like: likes, comments, shares, engagement bait, algorithm, viral.\n"
            "- Output only the final post text."
        )

    model = (preferred_model or os.environ.get("TRAINER_AI_TYPE_MODEL") or "gpt-4o-mini").strip()
    max_chars = int((os.environ.get("TRAINER_AI_TYPE_MAX_CHARS") or "2000").strip() or "2000")
    max_chars = max(50, min(max_chars, 20000))
    max_tokens = int((os.environ.get("TRAINER_AI_TYPE_MAX_TOKENS") or "600").strip() or "600")
    max_tokens = max(32, min(max_tokens, 2000))

    client = OpenAI(api_key=key)
    msg = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "system",
                "content": (
                    "You write text for a single input field that will be auto-typed. "
                    "Return only the final field text. No markdown, no explanations."
                ),
            },
            {"role": "user", "content": resolved_prompt},
        ],
    )
    out = (msg.choices[0].message.content or "").strip()
    if not out:
        raise RuntimeError("OpenAI returned empty text for ai_type step")
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


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
        ct = next((l for l in headers.splitlines() if "Content-Type" in l), "")
        nm = re.search(r'name="([^"]+)"', cd)
        fn = re.search(r'filename="([^"]+)"', cd)
        if not nm:
            continue
        name = nm.group(1)
        if fn:
            filename = fn.group(1).strip()
            content_type = ""
            if ":" in ct:
                content_type = ct.split(":", 1)[1].strip().lower()
            result.setdefault(name, []).append(
                {"filename": filename, "data": data, "content_type": content_type}
            )
        else:
            result[name] = data.decode(errors="utf-8")
    return result


def save_workflow(name, steps):
    """Save workflow JSON from trained steps."""
    import datetime
    wf = {
        "workflow_name": name,
        "engine": "trainer_v1",
        "total_steps": len(steps),
        "taught_at": datetime.datetime.utcnow().isoformat(),
        "steps": steps,
    }
    path = WORKFLOWS_DIR / f"{name}.json"
    path.write_text(json.dumps(wf, indent=2))
    return path


def save_run_audit(workflow_name: str, dry_run: bool, steps: list[dict], error: Optional[str] = None) -> Path:
    """Persist a run audit entry and update latest.json."""
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", (workflow_name or "unknown")).strip("._-") or "unknown"
    ok_count = sum(1 for s in steps if s.get("status") in ("ok", "dry_run"))
    err_count = sum(1 for s in steps if s.get("status") == "error")
    payload = {
        "audited_at": datetime.datetime.utcnow().isoformat() + "Z",
        "workflow_name": workflow_name,
        "dry_run": bool(dry_run),
        "total_steps": len(steps),
        "ok_steps": ok_count,
        "error_steps": err_count,
        "error": error or "",
        "steps": steps,
    }
    out = RUN_AUDIT_DIR / f"{stamp}_{safe_name}.json"
    out.write_text(json.dumps(payload, indent=2))
    latest = RUN_AUDIT_DIR / "latest.json"
    latest.write_text(json.dumps(payload, indent=2))
    return out


def _now_utc_z() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _safe_slug(text: str, fallback: str = "asset") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "").strip()).strip("._-")
    return slug or fallback


def _normalize_clone_target_name(raw: str) -> str:
    """Return a safe new workflow filename stem, or empty if invalid."""
    name = str(raw or "").strip()
    if not name or name in (".", ".."):
        return ""
    if any(ch in name for ch in "/\\"):
        return ""
    for bad in '<>:"|?*':
        if bad in name:
            return ""
    if len(name) > 180:
        name = name[:180].rstrip()
    return name or ""


_WEBSITE_INDUSTRY_LABELS: dict[str, str] = {
    "business": "Business Website",
    "ecommerce": "E-Commerce Website",
    "portfolio": "Portfolio Website",
    "education": "Educational Website",
    "hospital": "Hospital / Clinic Website",
    "restaurant": "Restaurant Website",
    "realestate": "Real Estate Website",
    "service": "Service-Based Website",
    "blog": "Blog Website",
    "event": "Event Website",
    "saas": "SaaS Website",
}

_WEBSITE_PROMPT_TEMPLATES: dict[str, dict[str, str]] = {
    "business": {
        "basic": "Build a lead-generation business website with strong CTA, services, testimonials, and contact conversion.",
        "admin": "Build a business website plus admin dashboard for content blocks, leads, testimonials, and CTA settings.",
    },
    "ecommerce": {
        "basic": "Build a storefront website with catalog browsing, product detail pages, and conversion-first shopping flow.",
        "admin": "Build an e-commerce website plus admin dashboard for products, categories, orders, inventory, and coupons.",
    },
    "portfolio": {
        "basic": "Build a modern portfolio website with projects, skills, about, and contact conversion flow.",
        "admin": "Build a portfolio website plus admin dashboard for managing projects, case studies, and inbound inquiries.",
    },
    "education": {
        "basic": "Build an educational institute website with admissions CTA, course pages, and trust-focused content.",
        "admin": "Build an educational website plus admin dashboard for courses, admissions leads, notices, and faculty profiles.",
    },
    "hospital": {
        "basic": "Build a healthcare website with services, doctor highlights, emergency CTA, and appointment flow.",
        "admin": "Build a healthcare website plus admin dashboard for doctors, appointments, services, and notices.",
    },
    "restaurant": {
        "basic": "Build a restaurant website with menu-first UX, location/contact, and reservation/order CTA.",
        "admin": "Build a restaurant website plus admin dashboard for menu items, reservations, offers, and reviews.",
    },
    "realestate": {
        "basic": "Build a real-estate website with property listings, details, inquiry forms, and contact conversion.",
        "admin": "Build a real-estate website plus admin dashboard for properties, inquiries, agents, and content blocks.",
    },
    "service": {
        "basic": "Build a local services website with trust indicators, service pages, and fast lead capture.",
        "admin": "Build a service-business website plus admin dashboard for services, pricing, leads, and testimonials.",
    },
    "blog": {
        "basic": "Build a content-focused blog website with categories, article templates, and newsletter CTA.",
        "admin": "Build a blog website plus admin dashboard for posts, categories, authors, and publishing workflow.",
    },
    "event": {
        "basic": "Build an event website with schedule, speaker highlights, and registration conversion flow.",
        "admin": "Build an event website plus admin dashboard for sessions, speakers, registrations, and announcements.",
    },
    "saas": {
        "basic": "Build a SaaS marketing website with feature-led sections, pricing, and trial/demo CTA.",
        "admin": "Build a SaaS website plus admin dashboard for content, lead capture, user roles, and product announcements.",
    },
}

_WEBSITE_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("industry", "industry"),
    ("website_name", "website_name"),
    ("phone", "phone"),
    ("email", "email"),
    ("whatsapp", "whatsapp"),
    ("about", "about"),
    ("business_address", "business_address"),
    ("services_list", "services_list"),
    ("brand_colors", "brand_colors"),
    ("cta_goal", "cta_goal"),
    ("social_links", "social_links"),
    ("admin_users_roles", "admin_users_roles"),
)


def _website_payload_from_request(
    *,
    content_type: str,
    body: bytes,
) -> tuple[dict[str, str], dict[str, Any]]:
    payload: dict[str, str] = {}
    files: dict[str, Any] = {}
    ct = str(content_type or "")
    if "multipart/form-data" in ct:
        bnd = re.search(r'boundary=([^\s;]+)', ct)
        if not bnd:
            raise ValueError("no boundary for multipart/form-data")
        fields = parse_multipart(body, bnd.group(1))
        for key, value in fields.items():
            if isinstance(value, list):
                if value:
                    files[key] = value[0]
                continue
            payload[key] = str(value or "").strip()
    else:
        raw = json.loads(body.decode("utf-8") or "{}")
        if not isinstance(raw, dict):
            raw = {}
        for k, v in raw.items():
            payload[str(k)] = str(v or "").strip()
    return payload, files


def _website_validate_payload(payload: dict[str, str], files: dict[str, Any], website_type: str) -> dict[str, Any]:
    out: dict[str, Any] = {str(k): str(v or "").strip() for k, v in payload.items()}
    out["website_type"] = "admin" if website_type == "admin" else "basic"
    missing: list[str] = []
    for key, label in _WEBSITE_REQUIRED_FIELDS:
        if not str(out.get(key) or "").strip():
            missing.append(label)
    industry = str(out.get("industry") or "").strip().lower()
    if industry not in _WEBSITE_INDUSTRY_LABELS:
        missing.append("industry(valid)")
    create_logo = str(out.get("create_logo_with_ai") or "").strip().lower() in ("1", "true", "yes", "on")
    logo_file = files.get("logo")
    if not create_logo and not logo_file:
        missing.append("logo(upload or create_logo_with_ai=true)")
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    out["create_logo_with_ai"] = bool(create_logo)
    if logo_file:
        out["logo_filename"] = str(logo_file.get("filename") or "").strip()
    else:
        out["logo_filename"] = ""
    return out


def _website_build_internal_prompt(payload: dict[str, Any]) -> str:
    industry_key = str(payload.get("industry") or "").strip().lower()
    website_type = str(payload.get("website_type") or "basic").strip().lower()
    by_type = _WEBSITE_PROMPT_TEMPLATES.get(industry_key) or {}
    base_instruction = str(by_type.get(website_type) or "").strip()
    if not base_instruction:
        base_instruction = "Build a production-ready website with strong UX, clear conversion paths, and maintainable architecture."
    return (
        "You are a senior full-stack engineer using Cursor to implement production-ready websites.\n\n"
        "Mandatory stack and workflow:\n"
        "- Code in Cursor with clean modular structure.\n"
        "- Use git with meaningful commits.\n"
        "- Use MongoDB for persistence where dynamic data is needed.\n"
        "- Deploy frontend/backend to Vercel.\n"
        "- Use Firebase where realtime/auth/storage is appropriate.\n\n"
        f"Website type: {website_type}\n"
        f"Industry: {industry_key} ({_WEBSITE_INDUSTRY_LABELS.get(industry_key, industry_key)})\n"
        f"Business name: {payload.get('website_name', '')}\n"
        f"Phone: {payload.get('phone', '')}\n"
        f"Email: {payload.get('email', '')}\n"
        f"WhatsApp: {payload.get('whatsapp', '')}\n"
        f"Business address: {payload.get('business_address', '')}\n"
        f"Brand colors: {payload.get('brand_colors', '')}\n"
        f"Primary CTA goal: {payload.get('cta_goal', '')}\n"
        f"Services/products: {payload.get('services_list', '')}\n"
        f"About details: {payload.get('about', '')}\n"
        f"Social links: {payload.get('social_links', '')}\n"
        f"Admin users + roles: {payload.get('admin_users_roles', '')}\n"
        f"Logo mode: {'AI-generated logo required' if payload.get('create_logo_with_ai') else 'Use uploaded logo'}\n"
        f"Logo filename: {payload.get('logo_filename', '')}\n\n"
        "Project objective:\n"
        f"{base_instruction}\n\n"
        "If website_type is admin, include secure role-based admin dashboard with CRUD, audit-friendly logs, and operational metrics.\n"
        "Return production-ready code architecture and implementation plan in a way directly executable in Cursor."
    )


def _media_kind_from_upload(asset_type: str, mime_type: str, filename: str) -> str:
    kind = (asset_type or "").strip().lower()
    if kind in ("image", "video"):
        return kind
    mt = (mime_type or "").strip().lower()
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("video/"):
        return "video"
    guessed, _ = mimetypes.guess_type(filename or "")
    guessed = (guessed or "").lower()
    if guessed.startswith("image/"):
        return "image"
    if guessed.startswith("video/"):
        return "video"
    return ""


def _load_media_index() -> dict:
    if not MEDIA_INDEX_FILE.exists():
        return {"assets": []}
    try:
        data = json.loads(MEDIA_INDEX_FILE.read_text())
    except Exception:
        return {"assets": []}
    if not isinstance(data, dict):
        return {"assets": []}
    assets = data.get("assets")
    if not isinstance(assets, list):
        assets = []
    data["assets"] = assets
    return data


def _save_media_index(index: dict) -> None:
    MEDIA_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_INDEX_FILE.write_text(json.dumps(index, indent=2))


def _public_asset(asset: dict) -> dict:
    return {
        "id": str(asset.get("id") or ""),
        "asset_type": str(asset.get("asset_type") or ""),
        "source": str(asset.get("source") or ""),
        "original_filename": str(asset.get("original_filename") or ""),
        "stored_filename": str(asset.get("stored_filename") or ""),
        "relative_path": str(asset.get("relative_path") or ""),
        "mime_type": str(asset.get("mime_type") or ""),
        "size_bytes": int(asset.get("size_bytes") or 0),
        "topic": str(asset.get("topic") or ""),
        "industry": str(asset.get("industry") or ""),
        "created_at": str(asset.get("created_at") or ""),
    }


def _register_media_asset(
    *,
    kind: str,
    source: str,
    data: bytes,
    original_filename: str,
    mime_type: str = "",
    topic: str = "",
    industry: str = "",
) -> dict:
    if kind not in ("image", "video"):
        raise ValueError("kind must be image or video")
    ext = Path(original_filename or "").suffix.strip().lower()
    if not ext:
        ext = ".png" if kind == "image" else ".mp4"
    token = secrets.token_hex(6)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    stored_name = f"{stamp}_{_safe_slug(Path(original_filename or kind).stem, kind)}_{token}{ext}"
    base = MEDIA_IMAGES_DIR if kind == "image" else MEDIA_VIDEOS_DIR
    out_path = base / stored_name
    out_path.write_bytes(data)
    rel = str(out_path.relative_to(BASE_DIR))
    asset = {
        "id": f"{'upl' if source == 'uploaded' else 'ai'}_{stamp}_{token}",
        "asset_type": kind,
        "source": source,
        "original_filename": original_filename or stored_name,
        "stored_filename": stored_name,
        "relative_path": rel,
        "mime_type": mime_type or (mimetypes.guess_type(stored_name)[0] or ""),
        "size_bytes": len(data),
        "topic": str(topic or "").strip(),
        "industry": str(industry or "").strip(),
        "created_at": _now_utc_z(),
    }
    with _MEDIA_IO_LOCK:
        idx = _load_media_index()
        assets = idx.get("assets") if isinstance(idx.get("assets"), list) else []
        assets.append(asset)
        idx["assets"] = assets[-5000:]
        _save_media_index(idx)
    return asset


def _generate_ai_image_asset(topic: str, industry: str = "", *, main_topic: str = "") -> dict:
    theme = (main_topic or topic or "industry insight").strip()
    angle = (topic or theme).strip()
    prompt = (
        "Create ONE vertical **social infographic** image (not a generic stock photo, not a scenic wallpaper, "
        "not a single vague logo on empty space). It must read as an **information graphic** about the subject below.\n"
        f"- Campaign / umbrella theme: {theme}\n"
        f"- This slide’s specific angle or headline: {angle}\n"
        f"- Industry context: {industry or 'general'}\n"
        "Content: persuasive, expert-level on-image copy (short headline + 3–7 tight bullets or numbered steps, "
        "optional tiny stat callouts or simple icons). Write like a sharp digital marketer with internet-native clarity.\n"
        "Visuals: clear hierarchy, simple diagrams or iconography where helpful, high contrast, readable on a phone.\n"
        "Platforms: suitable for LinkedIn and Instagram feed in portrait."
    )
    return _generate_ai_image_asset_from_prompt(prompt=prompt, topic=topic, industry=industry)


def _fit_image_to_1080x1350_png(image_bytes: bytes) -> bytes:
    import io
    from PIL import Image, ImageOps  # type: ignore

    with Image.open(io.BytesIO(image_bytes)) as src:
        src = src.convert("RGB")
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        fitted = ImageOps.fit(src, (1080, 1350), method=resample, centering=(0.5, 0.5))
        out = io.BytesIO()
        fitted.save(out, format="PNG", optimize=True)
        return out.getvalue()


def _generate_ai_image_asset_from_prompt(prompt: str, topic: str = "", industry: str = "") -> dict:
    from openai import OpenAI
    import base64

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for AI image generation")
    model = (os.environ.get("TRAINER_MEDIA_IMAGE_MODEL") or "gpt-image-1").strip()
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("AI image prompt is empty")
    prompt = (
        f"{prompt}\n\n"
        "Output requirements (image — **infographic only**):\n"
        "- Must be a **single infographic composition** (headline + structured facts/steps), not a photo-only poster.\n"
        "- Final export target: 1080x1350 px portrait (4:5 aspect ratio).\n"
        "- Keep text readable with high contrast; hierarchy obvious in one glance.\n"
        "- Professional digital-marketer tone for all on-image copy.\n"
        "- Keep all text/content away from corners and edges.\n"
        "- Enforce safe margins: 20% left/right and 30% top/bottom.\n"
        "- Keep critical content inside the central safe area (60% width x 40% height).\n"
        "- No watermark or logo unless explicitly requested."
    )
    client = OpenAI(api_key=key)
    res = client.images.generate(model=model, prompt=prompt, size="1024x1536")
    b64 = ""
    if getattr(res, "data", None) and len(res.data) > 0:
        b64 = getattr(res.data[0], "b64_json", "") or ""
    if not b64:
        raise RuntimeError("AI image generation returned no image bytes")
    png = base64.b64decode(b64)
    png = _fit_image_to_1080x1350_png(png)
    return _register_media_asset(
        kind="image",
        source="ai",
        data=png,
        original_filename=f"ai_image_{_safe_slug(topic or 'day')}.png",
        mime_type="image/png",
        topic=topic,
        industry=industry,
    )


def _ai_media_common_prompt_requirements() -> str:
    return (
        "Output requirements:\n"
        "- Canvas must be 1080x1350 portrait (4:5).\n"
        "- Keep all important content inside center safe area (60% width x 40% height).\n"
        "- Maintain 20% empty margin on left/right edges.\n"
        "- Maintain 30% empty margin on top/bottom edges.\n"
        "- Never place key text near corners or outer edges.\n"
        "- Use professional, internet-savvy digital marketer tone.\n"
    )


def _ai_media_video_prompt_requirements() -> str:
    """
    Creative guardrails for short feed video — lively, hook-first, character-driven (not a static infographic clip).
    """
    return (
        "Video creative brief (must follow):\n"
        "- **Portrait 4:5** (1080x1350) framing for LinkedIn / Instagram feed.\n"
        "- The piece must feel **alive and watchable**: include **people** OR **stylized / cartoon characters** "
        "(pick one coherent look) who are **doing or explaining something concrete** tied to the topic — e.g. "
        "talking to camera, whiteboard sketch, screen demo, reaction, problem→fix vignette, mentor explaining to a peer.\n"
        "- **Hook in the first 1–2 seconds**: pattern interrupt, bold motion, or punchy on-screen line so a scroller "
        "**stops** — think curiosity gap + energy, not a slow fade-in on a title card.\n"
        "- Tone: **professional but lively**, LinkedIn-credible; avoid stiff corporate b-roll only.\n"
        "- Use **motion, expression, and staging**; not a slideshow of static infographic panels or Ken-burns on one poster.\n"
        "- Any on-screen copy: short, legible, centered safe zone; no tiny wall-of-text.\n"
        "- Do **not** describe the video as “an infographic video” or “animated chart only” — characters and story beat come first.\n"
    )


def _generate_ai_post_text_for_topic(
    *,
    topic: str,
    main_topic: str,
    platforms: list[str],
    model: str = "",
) -> str:
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for AI text generation")
    mdl = (model or os.environ.get("TRAINER_AI_TYPE_MODEL") or "gpt-4o-mini").strip()
    platform_list = ", ".join(platforms or ["instagram"])
    user_prompt = (
        "Write one social post caption.\n"
        f"Main topic: {main_topic}\n"
        f"Specific topic: {topic}\n"
        f"Platforms: {platform_list}\n\n"
        "Constraints:\n"
        "- Professional digital marketer voice.\n"
        "- Practical and high-knowledge internet-native insights.\n"
        "- 1 short hook + 3 to 5 concise lines + CTA.\n"
        "- Add relevant hashtags at the end (3 to 6).\n"
        "- Output plain text only."
    )
    client = OpenAI(api_key=key)
    res = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": "You are a high-performance social media strategist."},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )
    text = (
        (res.choices[0].message.content or "").strip()
        if getattr(res, "choices", None)
        else ""
    )
    if not text:
        raise RuntimeError("AI text generation returned empty caption")
    return text


def _generate_ai_caption_from_user_prompt(*, prompt: str, workflow_name: str, platform_hint: str = "") -> str:
    """
    Production consumer mode: generate a single caption from the user's prompt for a workflow.
    """
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for AI text generation")
    mdl = (os.environ.get("TRAINER_AI_TYPE_MODEL") or "gpt-4o-mini").strip()
    wf = str(workflow_name or "").strip() or "workflow"
    plat = str(platform_hint or "").strip().lower()
    if not plat:
        plat = "social"
    user_prompt = (
        "Write ONE ready-to-post social caption.\n"
        f"Workflow: {wf}\n"
        f"Platform: {plat}\n\n"
        "User instruction (follow exactly, but keep output as plain text only):\n"
        f"{str(prompt or '').strip()}\n\n"
        "Output rules:\n"
        "- Plain text only (no markdown code fences).\n"
        "- 1 hook line + 3–6 short lines + 1 CTA.\n"
        "- Add 3–8 relevant hashtags at the end.\n"
    )
    client = OpenAI(api_key=key)
    res = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": "You are a high-performance social media strategist."},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )
    text = (
        (res.choices[0].message.content or "").strip()
        if getattr(res, "choices", None)
        else ""
    )
    if not text:
        raise RuntimeError("AI text generation returned empty caption")
    return text


def _workflow_prompt_seed(workflow_name: str) -> str:
    fp = WORKFLOWS_DIR / f"{str(workflow_name or '').strip()}.json"
    if not fp.exists():
        return ""
    try:
        with _WORKFLOW_IO_LOCK:
            wf = json.loads(fp.read_text())
        auto = wf.get("automation") if isinstance(wf, dict) else {}
        if not isinstance(auto, dict):
            return ""
        return str(auto.get("topic_seed") or "").strip()
    except Exception:
        return ""


def _save_caption_text_to_downloads_simple(*, workflow_name: str, caption: str) -> str:
    dl = _downloads_dir()
    dl.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    base = _safe_slug(workflow_name or "caption", "caption")
    out = _next_available_download_path(dl, f"{stamp}_{base}_caption.txt")
    out.write_text((caption or "").strip() + "\n", encoding="utf-8")
    return str(out.resolve())


def _next_available_download_path(dl: Path, filename: str) -> Path:
    candidate = dl / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    ext = Path(filename).suffix
    i = 2
    while True:
        alt = dl / f"{stem}_{i}{ext}"
        if not alt.exists():
            return alt
        i += 1


def _save_media_asset_copy_to_downloads(
    asset: dict,
    *,
    stem_hint: str = "",
    run_index: int = 0,
    media_kind: str = "",
) -> str:
    rel_path = str(asset.get("relative_path") or "").strip()
    if not rel_path:
        return ""
    src = (BASE_DIR / rel_path).resolve()
    if not src.exists():
        return ""
    dl = _downloads_dir()
    dl.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or (".png" if str(asset.get("asset_type") or "") == "image" else ".mp4")
    if run_index > 0 and media_kind in ("image", "video"):
        out = _next_available_download_path(dl, f"run{run_index}{media_kind}{ext}")
    else:
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        base = _safe_slug(stem_hint or src.stem or "media", "media")
        out = _next_available_download_path(dl, f"{stamp}_{base}{ext}")
    shutil.copy2(src, out)
    return str(out.resolve())


def _ffmpeg_available() -> str:
    return shutil.which("ffmpeg") or ""


def _render_mp4_from_image_ffmpeg(image_path: Path, *, out_path: Path, duration_sec: int = 6) -> None:
    ffmpeg = _ffmpeg_available()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    cmd = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-vf",
        "scale=1080:1350,format=yuv420p",
        "-t",
        str(max(2, int(duration_sec))),
        "-r",
        "30",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not out_path.exists():
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or "ffmpeg video render failed")


def _generate_ai_video_asset_from_prompt(prompt: str, topic: str = "", industry: str = "") -> dict:
    """
    Preferred path: use OpenAI video API if available.
    Fallback path: generate AI image and render a short MP4 via ffmpeg.
    """
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for AI video generation")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("AI video prompt is empty")
    full_prompt = (
        f"{prompt}\n\n"
        f"{_ai_media_video_prompt_requirements()}\n"
        "Technical:\n"
        "- Keep important visuals and any text inside the center safe area.\n"
    )
    client = OpenAI(api_key=key)
    model = (os.environ.get("TRAINER_MEDIA_VIDEO_MODEL") or "sora-2").strip()

    # Attempt direct video generation where SDK/provider supports it.
    try:
        videos_api = getattr(client, "videos", None)
        gen = getattr(videos_api, "generate", None) if videos_api is not None else None
        if callable(gen):
            res = gen(
                model=model,
                prompt=full_prompt,
                size="1080x1350",
                duration="6s",
            )
            # Conservative extraction for common response shapes.
            blob = b""
            data = getattr(res, "data", None)
            if isinstance(data, list) and data:
                first = data[0]
                b64 = getattr(first, "b64_json", "") or getattr(first, "b64", "")
                if b64:
                    import base64

                    blob = base64.b64decode(b64)
            if blob:
                return _register_media_asset(
                    kind="video",
                    source="ai",
                    data=blob,
                    original_filename=f"ai_video_{_safe_slug(topic or 'day')}.mp4",
                    mime_type="video/mp4",
                    topic=topic,
                    industry=industry,
                )
    except Exception:
        # Fall through to ffmpeg fallback.
        pass

    # Fallback: make a topic **infographic** still and convert to short MP4 (not the full motion brief).
    if not _ffmpeg_available():
        raise RuntimeError(
            "AI video API unavailable and ffmpeg not installed. Install ffmpeg on macOS/Windows "
            "or configure a video-capable model in TRAINER_MEDIA_VIDEO_MODEL."
        )
    still_prompt = (
        f"Create one bold **infographic** key visual (single static frame) that teases this video topic — "
        f"headline + 3–5 bullets, high contrast, portrait 4:5. Topic: {topic or 'social insight'}. "
        f"Industry: {industry or 'general'}. This image will be used only as a short slideshow clip fallback, "
        "not as the primary creative direction for motion."
    )
    image_asset = _generate_ai_image_asset_from_prompt(
        prompt=still_prompt,
        topic=topic,
        industry=industry,
    )
    rel = str(image_asset.get("relative_path") or "").strip()
    src = (BASE_DIR / rel).resolve()
    if not src.exists():
        raise RuntimeError("Video fallback failed: source image file missing")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "clip.mp4"
        _render_mp4_from_image_ffmpeg(src, out_path=out, duration_sec=6)
        blob = out.read_bytes()
    return _register_media_asset(
        kind="video",
        source="ai",
        data=blob,
        original_filename=f"ai_video_{_safe_slug(topic or 'day')}.mp4",
        mime_type="video/mp4",
        topic=topic,
        industry=industry,
    )


def _build_ai_media_topics(main_topic: str, count: int) -> list[str]:
    seed = (main_topic or "").strip()
    if not seed:
        raise ValueError("main_topic is required")
    try:
        return _generate_topics_from_seed(seed, completed=[], count=count)
    except Exception:
        return [f"{seed} — angle {i}" for i in range(1, count + 1)]


def _openai_api_key_configured() -> bool:
    return bool((os.environ.get("OPENAI_API_KEY") or "").strip())


def _cusear_storage_norm_plan(x: str) -> str:
    t = (x or "").strip().lower().replace("-", "_").replace(" ", "_")
    if t in ("core",):
        return "core"
    if t in ("hybrid",):
        return "hybrid"
    if t in ("ai_budget", "aibudget", "budget", "ai"):
        return "ai_budget"
    if t in ("ai_pro", "aipro", "pro"):
        return "ai_pro"
    return ""


def _cusear_storage_norm_media(x: str) -> str:
    t = (x or "").strip().lower()
    if t in ("text", "texts", "caption", "captions"):
        return "text"
    if t in ("image", "images", "img"):
        return "image"
    if t in ("video", "videos", "vid"):
        return "video"
    return ""


def _rel_under_downloads(dl: Path, dest: Path) -> str:
    try:
        return str(dest.resolve().relative_to(dl.resolve()))
    except ValueError:
        return str(dest.resolve())


def _cusear_ensure_storage_plan(dl: Path, plan: str, platform_raw: str) -> None:
    """Create only this plan’s folders under Downloads/cusear (lazy vault)."""
    if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
        return
    if plan == "ai_pro":
        plat = (platform_raw or "").strip().lower()
        if plat not in PLATFORM_DIR:
            return
        ensure_plan_vault(dl, "ai_pro", platform=plat)  # type: ignore[arg-type]
        return
    ensure_plan_vault(dl, plan)  # type: ignore[arg-type]


def _cusear_storage_generate_one(
    dl: Path,
    *,
    plan: str,
    media: str,
    day: int,
    platform: str,
    industry: str,
    main_topic: str,
    topic: str,
) -> dict[str, Any]:
    """
    AI-fill one vault slot (text / image / video). Inner helpers enforce OPENAI_API_KEY.
    """
    from cusear.storage_vault import atomic_write_bytes as _awb, atomic_write_text as _awt

    _cusear_ensure_storage_plan(dl, plan, platform)
    dest = slot_path(dl, plan=plan, media=media, day=day, platform=(platform or None))
    base: dict[str, Any] = {
        "path": str(dest.resolve()),
        "relative_to_downloads": _rel_under_downloads(dl, dest),
        "plan": plan,
        "media": media,
        "day": day,
    }
    if media == "text":
        plats = [platform] if (plan == "ai_pro" and platform) else ["instagram"]
        txt = _generate_ai_post_text_for_topic(topic=topic, main_topic=main_topic, platforms=plats)
        _awt(dest, txt.strip() + "\n")
        base["size_bytes"] = int(dest.stat().st_size) if dest.exists() else 0
        return base
    if media == "image":
        asset = _generate_ai_image_asset(topic=topic, industry=industry, main_topic=main_topic)
        rel = str(asset.get("relative_path") or "").strip()
        src = (BASE_DIR / rel).resolve() if rel else None
        if not src or not src.exists():
            raise RuntimeError("generated image file missing")
        _awb(dest, src.read_bytes())
        base["size_bytes"] = int(dest.stat().st_size) if dest.exists() else 0
        return base
    if media == "video":
        theme = (main_topic or topic or "").strip()
        angle = (topic or theme).strip()
        ind = (industry or "general").strip()
        prompt = (
            f"Produce a short, **scroll-stopping** vertical social video for LinkedIn/Instagram.\n"
            f"- Umbrella theme: {theme}\n"
            f"- This episode’s focus: {angle}\n"
            f"- Industry: {ind}\n"
            "Narrative: open with a **strong hook** (visual + idea), then show characters **demonstrating or explaining** "
            "one clear takeaway tied to the focus — energetic, human (or cartoon) led, not a dry chart tour.\n"
            "Goal: someone mid-feed **stops scrolling** and watches."
        )
        asset = _generate_ai_video_asset_from_prompt(prompt=prompt, topic=topic, industry=industry)
        rel = str(asset.get("relative_path") or "").strip()
        src = (BASE_DIR / rel).resolve() if rel else None
        if not src or not src.exists():
            raise RuntimeError("generated video file missing")
        _awb(dest, src.read_bytes())
        base["size_bytes"] = int(dest.stat().st_size) if dest.exists() else 0
        return base
    raise RuntimeError("unsupported media")


def _ai_media_public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(job.get("job_id") or ""),
        "status": str(job.get("status") or "unknown"),
        "error": str(job.get("error") or ""),
        "manifest_download_path": str(job.get("manifest_download_path") or ""),
        "created_at": str(job.get("created_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
        "input": dict(job.get("input") or {}),
        "progress": dict(job.get("progress") or {}),
        "items": list(job.get("items") or []),
    }


def _ai_media_mark_updated(job: dict[str, Any]) -> None:
    job["updated_at"] = _now_utc_z()


def _save_caption_text_to_downloads(
    *,
    topic: str,
    caption: str,
    index: int,
    media_kind: str = "",
) -> str:
    dl = _downloads_dir()
    dl.mkdir(parents=True, exist_ok=True)
    if index > 0 and media_kind in ("image", "video"):
        out = _next_available_download_path(dl, f"run{index}{media_kind}caption.txt")
    else:
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        base = _safe_slug(topic or f"topic_{index}", f"topic_{index}")
        out = _next_available_download_path(dl, f"{stamp}_caption_{index:02d}_{base}.txt")
    out.write_text((caption or "").strip() + "\n", encoding="utf-8")
    return str(out.resolve())


def _save_ai_media_job_manifest_to_downloads(job: dict[str, Any]) -> str:
    dl = _downloads_dir()
    dl.mkdir(parents=True, exist_ok=True)
    job_id = str(job.get("job_id") or "aimedia")
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    out = dl / f"{stamp}_{_safe_slug(job_id, 'aimedia')}_manifest.json"
    payload = _ai_media_public_job(job)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(out.resolve())


def _cusear_platform_keys() -> list[str]:
    return list(PLATFORM_DIR.keys())


def _cusear_media_root(*, runtime_vars: dict[str, str], workflow_name: str, workflow_label: str = "") -> Path:
    _ = runtime_vars, workflow_name, workflow_label
    return vault_root(_downloads_dir())


def _write_text_in_dir(dest_dir: Path, filename: str, text: str) -> str:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = _next_available_download_path(dest_dir, filename)
    out.write_text((text or "").strip() + "\n", encoding="utf-8")
    return str(out.resolve())


def _copy_media_into_dir(src_path: str, dest_dir: Path, filename: str) -> str:
    src = Path(str(src_path or "").strip())
    if not src.exists() or not src.is_file():
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = _next_available_download_path(dest_dir, filename)
    shutil.copy2(src, out)
    return str(out.resolve())


def _copy_caption_into_dir(src_caption_path: str, caption_text: str, dest_dir: Path, filename: str) -> str:
    src = Path(str(src_caption_path or "").strip())
    if src.exists() and src.is_file():
        return _copy_media_into_dir(str(src), dest_dir, filename)
    if not str(caption_text or "").strip():
        return ""
    return _write_text_in_dir(dest_dir, filename, caption_text)


def _ai_media_export_item_to_cusear(
    *,
    item: dict[str, Any],
    runtime_vars: dict[str, str],
    workflow_name: str,
    workflow_label: str,
) -> dict[str, Any]:
    idx_raw = str(item.get("index") or runtime_vars.get("CURRENT_AUTOMATION_RUN") or "0").strip()
    idx = int(idx_raw) if idx_raw.isdigit() else 0
    idx = max(0, idx)
    image_src = str(item.get("image_download_path") or "").strip()
    video_src = str(item.get("video_download_path") or "").strip()
    caption = str(item.get("caption") or "").strip()
    cap_image_src = str(
        item.get("caption_image_download_path")
        or item.get("caption_download_path")
        or ""
    ).strip()
    cap_video_src = str(
        item.get("caption_video_download_path")
        or item.get("caption_download_path")
        or ""
    ).strip()
    cap_generic_src = str(item.get("caption_download_path") or "").strip()
    if not (image_src or video_src or caption or cap_image_src or cap_video_src or cap_generic_src):
        return item

    dl = _downloads_dir()
    base_dir = vault_root(dl)
    detected = [p for p in _detect_workflow_platforms(workflow_name) if p in PLATFORM_DIR]
    primary_key = detected[0] if detected else "instagram"
    if primary_key not in PLATFORM_DIR:
        primary_key = "instagram"
    ensure_plan_vault(dl, "ai_pro", platform=primary_key, create_stubs=False)  # type: ignore[arg-type]
    plat_label = PLATFORM_DIR[primary_key]  # type: ignore[index]
    ai_base = vault_root(dl) / PLAN_DIR["ai_pro"] / plat_label
    platform_tree: dict[str, dict[str, Path]] = {
        primary_key: {
            "text": ai_base / "Texts",
            "image": ai_base / "Images",
            "video": ai_base / "Videos",
        }
    }

    img_ext = (Path(image_src).suffix or ".png").lower() if image_src else ".png"
    vid_ext = (Path(video_src).suffix or ".mp4").lower() if video_src else ".mp4"
    index_tag = f"run{idx}" if idx > 0 else "run"
    image_name = f"{index_tag}image{img_ext}"
    video_name = f"{index_tag}video{vid_ext}"
    cap_name = f"{index_tag}caption.txt"
    cap_img_name = f"{index_tag}imagecaption.txt"
    cap_vid_name = f"{index_tag}videocaption.txt"

    exports: dict[str, dict[str, str]] = {}
    for platform_key, tree in platform_tree.items():
        img_out = _copy_media_into_dir(image_src, tree["image"], image_name) if image_src else ""
        vid_out = _copy_media_into_dir(video_src, tree["video"], video_name) if video_src else ""
        cap_img_out = _copy_caption_into_dir(cap_image_src, caption, tree["text"], cap_img_name)
        cap_vid_out = _copy_caption_into_dir(cap_video_src, caption, tree["text"], cap_vid_name)
        cap_out = _copy_caption_into_dir(cap_generic_src, caption, tree["text"], cap_name)
        exports[platform_key] = {
            "image_download_path": img_out,
            "video_download_path": vid_out,
            "caption_image_download_path": cap_img_out,
            "caption_video_download_path": cap_vid_out,
            "caption_download_path": cap_out or cap_img_out or cap_vid_out,
        }

    primary = exports.get(primary_key) or {}
    merged = dict(item)
    merged["image_download_path"] = str(primary.get("image_download_path") or image_src)
    merged["video_download_path"] = str(primary.get("video_download_path") or video_src)
    merged["caption_image_download_path"] = str(
        primary.get("caption_image_download_path") or cap_image_src or cap_generic_src
    )
    merged["caption_video_download_path"] = str(
        primary.get("caption_video_download_path") or cap_video_src or cap_generic_src
    )
    merged["caption_download_path"] = str(
        primary.get("caption_download_path")
        or merged.get("caption_image_download_path")
        or merged.get("caption_video_download_path")
        or cap_generic_src
    )
    merged["cusear_media_root"] = str(base_dir.resolve())
    merged["cusear_platform_exports"] = exports
    merged["cusear_primary_platform"] = primary_key
    return merged


def bootstrap_cusear_folders_on_desktop_launch() -> dict[str, Any]:
    """Pre-create cusear platform folders under Downloads (see ``cusear.media_folders``)."""
    return bootstrap_cusear_content_folders(
        workflows_dir=WORKFLOWS_DIR,
        bundles_dir=AR_BUNDLES_DIR,
        base_downloads=_downloads_dir(),
    )


def _trainer_resolve_calendar_flow_workflow(workflow_key: str, bundle_slug: str = "") -> tuple[str, str, str]:
    """
    Map ``workflow_key`` + optional ar™ ``bundle_slug`` to (flow_folder_label, workflow_display_label, bundle_slug_out).

    If ``bundle_slug`` is set, workflow must appear in that bundle's children.

    If not set, the first bundle containing this workflow wins; otherwise flow folder matches the standalone
    workflow label (same as folder bootstrap).
    """
    wk = str(workflow_key or "").strip()
    if not wk:
        raise ValueError("workflow_name required")
    if not (WORKFLOWS_DIR / f"{wk}.json").is_file():
        raise ValueError(f"workflow '{wk}' was not found")
    # Legacy: prior builds used a per-workflow folder label; the STORAGE vault is global now.
    # Keep a human-friendly label for UI messages.
    wf_disp = wk
    bs_in = str(bundle_slug or "").strip()
    if bs_in:
        fp = _bundle_path(bs_in)
        if not fp.is_file():
            raise ValueError("ar™ routine not found")
        with _BUNDLE_IO_LOCK:
            bundle = _normalize_bundle(json.loads(fp.read_text()))
        children = [str(x or "").strip() for x in (bundle.get("children") or []) if str(x or "").strip()]
        if wk not in children:
            raise ValueError("this workflow is not in that ar™")
        slug_out = str(bundle.get("slug") or bs_in).strip()
        flow = str(bundle.get("display_name") or bundle.get("slug") or bs_in).strip() or wf_disp
        return flow, wf_disp, slug_out
    with _BUNDLE_IO_LOCK:
        for bfp in sorted(AR_BUNDLES_DIR.glob("*.json")):
            try:
                raw = _normalize_bundle(json.loads(bfp.read_text()))
            except Exception:
                continue
            children = [str(x or "").strip() for x in (raw.get("children") or []) if str(x or "").strip()]
            if wk not in children:
                continue
            slug_out = str(raw.get("slug") or bfp.stem or "").strip()
            flow = str(raw.get("display_name") or slug_out).strip() or wf_disp
            return flow, wf_disp, slug_out
    return wf_disp, wf_disp, ""


def _preferred_media_kind_for_upload(step_desc: str, runtime_vars: dict[str, str]) -> str:
    explicit = str(
        runtime_vars.get("PREFER_MEDIA_KIND")
        or runtime_vars.get("AI_MEDIA_KIND")
        or runtime_vars.get("CURRENT_MEDIA_TYPE")
        or ""
    ).strip().lower()
    if explicit in ("image", "video"):
        return explicit
    low = (step_desc or "").strip().lower()
    if any(k in low for k in ("video", "reel", "clip", "short")):
        return "video"
    if any(k in low for k in ("image", "photo", "poster", "thumbnail", "graphic")):
        return "image"
    return ""


def _find_download_run_file(
    dl: Path,
    *,
    base_name: str,
    exts: list[str],
) -> str:
    for ext in exts:
        direct = dl / f"{base_name}{ext}"
        if direct.exists():
            return str(direct.resolve())
    candidates: list[Path] = []
    for ext in exts:
        candidates.extend(dl.glob(f"{base_name}_*{ext}"))
    if not candidates:
        return ""
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(candidates[0].resolve())


def _ai_media_item_from_downloads(*, run_index: int, preferred_kind: str = "") -> dict[str, Any]:
    if run_index <= 0:
        raise RuntimeError("Run index required for Downloads fallback mapping")
    dl = _downloads_dir()
    image_path = _find_download_run_file(
        dl, base_name=f"run{run_index}image", exts=[".png", ".jpg", ".jpeg", ".webp"]
    )
    video_path = _find_download_run_file(
        dl, base_name=f"run{run_index}video", exts=[".mp4", ".mov", ".mkv", ".webm"]
    )
    image_caption_path = _find_download_run_file(
        dl, base_name=f"run{run_index}imagecaption", exts=[".txt"]
    )
    video_caption_path = _find_download_run_file(
        dl, base_name=f"run{run_index}videocaption", exts=[".txt"]
    )
    if not image_path and not video_path:
        raise RuntimeError(
            f"No run{run_index}image/run{run_index}video file found in Downloads. "
            "Generate AI media first."
        )
    media_kind = preferred_kind if preferred_kind in ("image", "video") else ""
    if not media_kind:
        media_kind = "image" if image_path else ("video" if video_path else "")
    media_path = (
        video_path if media_kind == "video" and video_path else
        image_path if media_kind == "image" and image_path else
        (image_path or video_path)
    )
    caption_path = (
        video_caption_path if media_kind == "video" and video_caption_path else
        image_caption_path if media_kind == "image" and image_caption_path else
        (image_caption_path or video_caption_path)
    )
    caption_text = ""
    if caption_path:
        try:
            caption_text = Path(caption_path).read_text(encoding="utf-8").strip()
        except Exception:
            caption_text = ""
    return {
        "index": run_index,
        "topic": f"Run {run_index}",
        "status": "done",
        "caption": caption_text,
        "caption_download_path": caption_path,
        "caption_image_download_path": image_caption_path,
        "caption_video_download_path": video_caption_path,
        "image_download_path": image_path,
        "video_download_path": video_path,
        "media_kind": media_kind,
        "media_path": media_path,
    }


def _ai_media_select_next_item(runtime_vars: dict[str, str], preferred_kind: str = "") -> dict[str, Any]:
    """
    Select the next generated topic/caption/media item deterministically for workflow posting.
    """
    global _AI_MEDIA_LAST_COMPLETED_JOB_ID
    with _AI_MEDIA_JOBS_LOCK:
        job_id = str(runtime_vars.get("AI_MEDIA_JOB_ID") or "").strip()
        if not job_id:
            job_id = _AI_MEDIA_LAST_COMPLETED_JOB_ID
        if not job_id:
            run_idx_raw = str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip()
            if run_idx_raw.isdigit():
                return _ai_media_item_from_downloads(
                    run_index=int(run_idx_raw),
                    preferred_kind=preferred_kind,
                )
            raise RuntimeError(
                "No completed AI media job available in memory. "
                "Provide CURRENT_AUTOMATION_RUN or regenerate AI media."
            )
        job = _AI_MEDIA_JOBS.get(job_id)
        if not job:
            run_idx_raw = str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip()
            if run_idx_raw.isdigit():
                return _ai_media_item_from_downloads(
                    run_index=int(run_idx_raw),
                    preferred_kind=preferred_kind,
                )
            raise RuntimeError(f"AI media job not found: {job_id}")
        items = [x for x in (job.get("items") or []) if isinstance(x, dict)]
    valid = [
        x
        for x in items
        if str(x.get("status") or "") in ("done", "done_with_warning")
        and (
            str(x.get("image_download_path") or "").strip()
            or str(x.get("video_download_path") or "").strip()
        )
    ]
    if not valid:
        raise RuntimeError("No usable media items found in AI media job.")
    run_idx_raw = str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip()
    forced_ptr: Optional[int] = None
    if run_idx_raw.isdigit():
        forced_ptr = int(run_idx_raw) - 1
    if forced_ptr is not None:
        ptr = max(0, min(forced_ptr, len(valid) - 1))
        item = valid[ptr]
        runtime_vars["_AI_MEDIA_PTR"] = str(ptr)
    else:
        try:
            ptr = int(str(runtime_vars.get("_AI_MEDIA_PTR") or "0"))
        except Exception:
            ptr = 0
        ptr = max(0, min(ptr, len(valid) - 1))
        item = valid[ptr]
        runtime_vars["_AI_MEDIA_PTR"] = str(ptr + 1)
    runtime_vars["AI_MEDIA_JOB_ID"] = str(job_id)
    return item


def _run_ai_media_job(job_id: str) -> None:
    global _AI_MEDIA_ACTIVE_JOB_ID, _AI_MEDIA_LAST_COMPLETED_JOB_ID
    with _AI_MEDIA_JOBS_LOCK:
        job = _AI_MEDIA_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        _ai_media_mark_updated(job)
    try:
        inp = job["input"]
        main_topic = str(inp.get("main_topic") or "").strip()
        count = int(inp.get("count") or 1)
        media_type = str(inp.get("media_type") or "image").strip().lower()
        platforms = [str(x).strip().lower() for x in (inp.get("platforms") or []) if str(x).strip()]
        if not platforms:
            platforms = ["instagram"]
        base_prompt = str(inp.get("base_prompt") or "").strip()
        industry = str(inp.get("industry") or "").strip()
        topics = _build_ai_media_topics(main_topic, count)
        with _AI_MEDIA_JOBS_LOCK:
            if job.get("stop_requested"):
                job["status"] = "stopped"
                _ai_media_mark_updated(job)
                return
            job["progress"] = {
                "total_topics": len(topics),
                "current_topic_index": 0,
                "current_topic": "",
                "completed_items": 0,
            }
            job["items"] = []
            _ai_media_mark_updated(job)

        for idx, topic in enumerate(topics, start=1):
            with _AI_MEDIA_JOBS_LOCK:
                if job.get("stop_requested"):
                    job["status"] = "stopped"
                    _ai_media_mark_updated(job)
                    return
                job["progress"]["current_topic_index"] = idx
                job["progress"]["current_topic"] = topic
                _ai_media_mark_updated(job)
            item: dict[str, Any] = {
                "index": idx,
                "topic": topic,
                "status": "running",
                "caption": "",
                "caption_download_path": "",
                "caption_image_download_path": "",
                "caption_video_download_path": "",
                "image_download_path": "",
                "video_download_path": "",
                "image_asset_id": "",
                "video_asset_id": "",
                "error": "",
            }
            try:
                caption = _generate_ai_post_text_for_topic(
                    topic=topic,
                    main_topic=main_topic,
                    platforms=platforms,
                )
                item["caption"] = caption
                image_ok = False
                video_ok = False
                video_err = ""
                if media_type in ("image", "both"):
                    img_prompt = (
                        f"{base_prompt}\n\nTopic: {topic}\nCaption summary:\n{caption}\n\n"
                        "Deliver as one **infographic** layout (hierarchy + bullets/steps), not a generic stock photo.\n"
                        f"{_ai_media_common_prompt_requirements()}"
                    ).strip()
                    img_asset = _generate_ai_image_asset_from_prompt(
                        prompt=img_prompt,
                        topic=topic,
                        industry=industry,
                    )
                    item["image_asset_id"] = str(img_asset.get("id") or "")
                    item["image_download_path"] = _save_media_asset_copy_to_downloads(
                        img_asset,
                        stem_hint=f"image_{idx}_{topic}",
                        run_index=idx,
                        media_kind="image",
                    )
                    try:
                        item["caption_image_download_path"] = _save_caption_text_to_downloads(
                            topic=topic,
                            caption=caption,
                            index=idx,
                            media_kind="image",
                        )
                    except Exception:
                        item["caption_image_download_path"] = ""
                    image_ok = True
                if media_type in ("video", "both"):
                    try:
                        vid_prompt = (
                            f"{base_prompt}\n\nTopic: {topic}\nCaption summary:\n{caption}\n\n"
                            "Create a **short vertical social video** (not an infographic slideshow or static chart tour). "
                            "**People or stylized/cartoon characters** should clearly **do or explain** something tied to the topic; "
                            "**hook hard in the first 1–2 seconds** so a LinkedIn scroller stops; lively, professional energy."
                        ).strip()
                        vid_asset = _generate_ai_video_asset_from_prompt(
                            prompt=vid_prompt,
                            topic=topic,
                            industry=industry,
                        )
                        item["video_asset_id"] = str(vid_asset.get("id") or "")
                        item["video_download_path"] = _save_media_asset_copy_to_downloads(
                            vid_asset,
                            stem_hint=f"video_{idx}_{topic}",
                            run_index=idx,
                            media_kind="video",
                        )
                        try:
                            item["caption_video_download_path"] = _save_caption_text_to_downloads(
                                topic=topic,
                                caption=caption,
                                index=idx,
                                media_kind="video",
                            )
                        except Exception:
                            item["caption_video_download_path"] = ""
                        video_ok = True
                    except Exception as vexc:
                        video_err = str(vexc)
                        if media_type == "video":
                            raise
                if media_type == "image":
                    item["caption_download_path"] = str(item.get("caption_image_download_path") or "")
                elif media_type == "video":
                    item["caption_download_path"] = str(item.get("caption_video_download_path") or "")
                else:
                    item["caption_download_path"] = str(
                        item.get("caption_image_download_path")
                        or item.get("caption_video_download_path")
                        or ""
                    )
                if media_type == "both":
                    if image_ok and video_ok:
                        item["status"] = "done"
                    elif image_ok and not video_ok:
                        item["status"] = "done_with_warning"
                        item["error"] = (
                            "Image+caption generated. Video failed for this topic: "
                            + (video_err or "unknown video generation error")
                        )
                    else:
                        item["status"] = "error"
                        item["error"] = video_err or "both-mode generation failed"
                else:
                    item["status"] = "done"
            except Exception as exc:
                item["status"] = "error"
                item["error"] = str(exc)
            with _AI_MEDIA_JOBS_LOCK:
                items = job.get("items")
                if not isinstance(items, list):
                    items = []
                items.append(item)
                job["items"] = items
                job["progress"]["completed_items"] = len(
                    [
                        x
                        for x in items
                        if str(x.get("status")) in ("done", "done_with_warning")
                    ]
                )
                try:
                    job["manifest_download_path"] = _save_ai_media_job_manifest_to_downloads(job)
                except Exception:
                    pass
                _ai_media_mark_updated(job)

        with _AI_MEDIA_JOBS_LOCK:
            if str(job.get("status")) not in ("stopped", "error"):
                job["status"] = "completed"
                _AI_MEDIA_LAST_COMPLETED_JOB_ID = str(job.get("job_id") or "")
            _ai_media_mark_updated(job)
    except Exception as exc:
        with _AI_MEDIA_JOBS_LOCK:
            job = _AI_MEDIA_JOBS.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = str(exc)
                _ai_media_mark_updated(job)
    finally:
        with _AI_MEDIA_JOBS_LOCK:
            if _AI_MEDIA_ACTIVE_JOB_ID == job_id:
                _AI_MEDIA_ACTIVE_JOB_ID = ""


def _start_ai_media_job(payload: dict[str, Any]) -> dict[str, Any]:
    global _AI_MEDIA_ACTIVE_JOB_ID
    main_topic = str(payload.get("main_topic") or "").strip()
    if not main_topic:
        raise ValueError("main_topic is required")
    try:
        count = int(payload.get("count", 1))
    except Exception:
        count = 1
    count = max(1, min(30, count))
    media_type = str(payload.get("media_type") or "image").strip().lower()
    if media_type not in ("image", "video", "both"):
        raise ValueError("media_type must be image, video, or both")
    platforms_raw = payload.get("platforms")
    platforms: list[str] = []
    if isinstance(platforms_raw, list):
        for p in platforms_raw:
            v = str(p).strip().lower()
            if v in ("instagram", "facebook", "linkedin") and v not in platforms:
                platforms.append(v)
    if not platforms:
        platforms = ["instagram"]
    with _AI_MEDIA_JOBS_LOCK:
        if _AI_MEDIA_ACTIVE_JOB_ID:
            active = _AI_MEDIA_JOBS.get(_AI_MEDIA_ACTIVE_JOB_ID)
            if active and str(active.get("status")) in ("running", "queued"):
                raise RuntimeError("Another AI media generation job is already running")
        job_id = f"aimedia_{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
        job = {
            "job_id": job_id,
            "status": "queued",
            "error": "",
            "created_at": _now_utc_z(),
            "updated_at": _now_utc_z(),
            "stop_requested": False,
            "input": {
                "main_topic": main_topic,
                "count": count,
                "media_type": media_type,
                "platforms": platforms,
                "base_prompt": str(payload.get("base_prompt") or "").strip(),
                "industry": str(payload.get("industry") or "").strip(),
            },
            "progress": {
                "total_topics": count,
                "current_topic_index": 0,
                "current_topic": "",
                "completed_items": 0,
            },
            "items": [],
        }
        _AI_MEDIA_JOBS[job_id] = job
        _AI_MEDIA_ACTIVE_JOB_ID = job_id
    threading.Thread(target=_run_ai_media_job, args=(job_id,), daemon=True).start()
    return _ai_media_public_job(job)


def _campaign_day_template(day_index: int) -> dict:
    return {
        "day_index": int(day_index),
        "topic": "",
        "caption": "",
        "media_type": "image",
        "image_asset_id": "",
        "video_asset_id": "",
        "status": "draft",
        "source_meta": {"image": "", "video": ""},
        "review": {"status": "pending", "reviewed_at": "", "notes": ""},
    }


def _normalize_media_plan(raw: dict) -> dict:
    plan = raw if isinstance(raw, dict) else {}
    mode = str(plan.get("mode") or "uploaded_30").strip().lower()
    if mode not in ("uploaded_30", "ai_batch_7", "ai_daily_auto"):
        mode = "uploaded_30"
    image_source = str(plan.get("image_source") or "uploaded").strip().lower()
    if image_source not in ("uploaded", "ai"):
        image_source = "uploaded"
    # v1 scope: AI video generation is disabled. Keep uploaded-only video pipeline.
    video_source = "uploaded"
    raw_platforms = plan.get("platforms") or ["instagram"]
    platforms: list[str] = []
    if isinstance(raw_platforms, list):
        for p in raw_platforms:
            v = str(p).strip().lower()
            if v in ("instagram", "facebook", "linkedin") and v not in platforms:
                platforms.append(v)
    if not platforms:
        platforms = ["instagram"]
    try:
        campaign_days = int(plan.get("campaign_length_days", 30))
    except Exception:
        campaign_days = 30
    campaign_days = max(1, min(campaign_days, 365))
    try:
        batch_size = int(plan.get("batch_size", 7))
    except Exception:
        batch_size = 7
    batch_size = max(1, min(batch_size, 30))
    return {
        "enabled": bool(plan.get("enabled", False)),
        "campaign_length_days": campaign_days,
        "mode": mode,
        "review_required": bool(plan.get("review_required", True)),
        "image_source": image_source,
        "video_source": video_source,
        "batch_size": batch_size,
        "platforms": platforms,
        "topic_seed": str(plan.get("topic_seed") or "").strip(),
        "industry": str(plan.get("industry") or "").strip(),
    }


def _ensure_campaign_shape(auto: dict) -> tuple[dict, list[dict]]:
    plan = _normalize_media_plan(auto.get("media_plan") or {})
    days_raw = auto.get("campaign_days")
    days: list[dict] = []
    if isinstance(days_raw, list):
        for idx, d in enumerate(days_raw, start=1):
            if not isinstance(d, dict):
                continue
            day = _campaign_day_template(idx)
            day["day_index"] = int(d.get("day_index") or idx)
            day["topic"] = str(d.get("topic") or "").strip()
            day["caption"] = str(d.get("caption") or "").strip()
            media_type = str(d.get("media_type") or "image").strip().lower()
            if media_type not in ("none", "image", "video", "mixed"):
                media_type = "image"
            day["media_type"] = media_type
            day["image_asset_id"] = str(d.get("image_asset_id") or "").strip()
            day["video_asset_id"] = str(d.get("video_asset_id") or "").strip()
            src = d.get("source_meta") if isinstance(d.get("source_meta"), dict) else {}
            day["source_meta"] = {
                "image": str(src.get("image") or "").strip().lower(),
                "video": str(src.get("video") or "").strip().lower(),
            }
            review = d.get("review") if isinstance(d.get("review"), dict) else {}
            review_status = str(review.get("status") or "pending").strip().lower()
            if review_status not in ("pending", "approved", "rejected"):
                review_status = "pending"
            day["review"] = {
                "status": review_status,
                "reviewed_at": str(review.get("reviewed_at") or "").strip(),
                "notes": str(review.get("notes") or "").strip(),
            }
            status = str(d.get("status") or "draft").strip().lower()
            if status not in ("draft", "generated", "pending_review", "approved", "scheduled", "posted", "failed"):
                status = "draft"
            day["status"] = status
            days.append(day)
    target_count = int(plan.get("campaign_length_days") or 30)
    if len(days) < target_count:
        for i in range(len(days) + 1, target_count + 1):
            days.append(_campaign_day_template(i))
    elif len(days) > target_count:
        days = days[:target_count]
        for i, d in enumerate(days, start=1):
            d["day_index"] = i
    auto["media_plan"] = plan
    auto["campaign_days"] = days
    return plan, days


def _campaign_asset_maps() -> tuple[dict[str, dict], dict[str, dict]]:
    idx = _load_media_index()
    by_id: dict[str, dict] = {}
    for a in idx.get("assets") or []:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "").strip()
        if aid:
            by_id[aid] = a
    return idx, by_id


def _validate_campaign(auto: dict, *, skip_unfilled_draft_days: bool = False) -> list[str]:
    plan, days = _ensure_campaign_shape(auto)
    if not bool(plan.get("enabled", False)):
        return []
    errors: list[str] = []
    _idx, by_id = _campaign_asset_maps()
    ig_required = "instagram" in (plan.get("platforms") or [])
    review_required = bool(plan.get("review_required", True))
    image_source = str(plan.get("image_source") or "uploaded")
    video_source = str(plan.get("video_source") or "uploaded")
    for d in days:
        di = int(d.get("day_index") or 0)
        topic = str(d.get("topic") or "").strip()
        caption = str(d.get("caption") or "").strip()
        day_status = str(d.get("status") or "draft").strip().lower()
        if skip_unfilled_draft_days and day_status == "draft" and not topic and not caption:
            continue
        media_type = str(d.get("media_type") or "image")
        image_id = str(d.get("image_asset_id") or "").strip()
        video_id = str(d.get("video_asset_id") or "").strip()
        if ig_required and media_type == "none":
            errors.append(f"day {di}: instagram requires media_type image/video/mixed")
        needs_image = media_type in ("image", "mixed")
        needs_video = media_type in ("video", "mixed")
        if ig_required and media_type == "video":
            # Valid for reels workflows; no extra image requirement.
            needs_image = False
        if needs_image and not image_id:
            errors.append(f"day {di}: image asset is missing")
        if needs_video and not video_id:
            errors.append(f"day {di}: video asset is missing")
        src_meta = d.get("source_meta") if isinstance(d.get("source_meta"), dict) else {}
        image_meta = str(src_meta.get("image") or image_source).strip().lower()
        video_meta = str(src_meta.get("video") or video_source).strip().lower()
        if image_id:
            if image_meta == "ai":
                if image_id.startswith("ai_pending_"):
                    errors.append(f"day {di}: AI image not rendered yet")
            else:
                asset = by_id.get(image_id)
                if not asset:
                    errors.append(f"day {di}: image_asset_id '{image_id}' not found")
                elif str(asset.get("asset_type") or "") != "image":
                    errors.append(f"day {di}: image_asset_id '{image_id}' is not an image")
                elif image_meta == "uploaded" and str(asset.get("source") or "") != "uploaded":
                    errors.append(f"day {di}: image_source=uploaded but day uses non-uploaded asset")
        if video_id:
            asset = by_id.get(video_id)
            if not asset:
                errors.append(f"day {di}: video_asset_id '{video_id}' not found")
            elif str(asset.get("asset_type") or "") != "video":
                errors.append(f"day {di}: video_asset_id '{video_id}' is not a video")
            elif str(asset.get("source") or "") != "uploaded":
                errors.append(f"day {di}: video_source=uploaded but day uses non-uploaded asset")
        if review_required:
            review = d.get("review") if isinstance(d.get("review"), dict) else {}
            review_status = str(review.get("status") or "pending")
            if review_status != "approved":
                errors.append(f"day {di}: review_required=true but not approved")
    return errors


def _next_unfilled_days(days: list[dict], count: int) -> list[dict]:
    out: list[dict] = []
    for d in days:
        topic = str(d.get("topic") or "").strip()
        caption = str(d.get("caption") or "").strip()
        if topic and caption:
            continue
        out.append(d)
        if len(out) >= count:
            break
    return out


def _media_preflight_report(auto: dict) -> dict[str, Any]:
    _plan, days = _ensure_campaign_shape(auto)
    rows: list[dict[str, Any]] = []
    for d in days:
        day_index = int(d.get("day_index") or 0)
        topic = str(d.get("topic") or "").strip()
        caption = str(d.get("caption") or "").strip()
        image_id = str(d.get("image_asset_id") or "").strip()
        image_ok = False
        if image_id:
            image_ok = bool(_resolve_media_asset_path(image_id))
        errs: list[str] = []
        if not topic:
            errs.append("missing_topic")
        if not caption:
            errs.append("missing_caption")
        if day_index <= 7 and not image_ok:
            errs.append("missing_image")
        rows.append(
            {
                "day_index": day_index,
                "topic_ok": bool(topic),
                "caption_ok": bool(caption),
                "image_ok": image_ok,
                "errors": errs,
            }
        )
    return {"days": rows}


def _run_media_preflight(auto: dict, *, topic_seed: str = "", industry: str = "") -> dict[str, Any]:
    plan, days = _ensure_campaign_shape(auto)
    seed = str(topic_seed or plan.get("topic_seed") or auto.get("topic_seed") or "").strip()
    if not seed:
        raise ValueError("topic_seed required for media preflight")
    plan["enabled"] = True
    plan["image_source"] = "ai"
    plan["video_source"] = "uploaded"
    plan["campaign_length_days"] = max(30, int(plan.get("campaign_length_days") or 30))
    if industry:
        plan["industry"] = industry.strip()
    if seed:
        plan["topic_seed"] = seed
        auto["topic_seed"] = seed
    auto["media_plan"] = _normalize_media_plan(plan)
    plan, days = _ensure_campaign_shape(auto)
    existing_topics = [str(d.get("topic") or "").strip() for d in days if str(d.get("topic") or "").strip()]
    need_topics = max(0, 30 - len(existing_topics))
    if need_topics > 0:
        generated = _generate_topics_from_seed(seed, existing_topics, count=need_topics)
        gi = 0
        for d in days:
            if gi >= len(generated):
                break
            if str(d.get("topic") or "").strip():
                continue
            d["topic"] = generated[gi]
            gi += 1
    for d in days[:30]:
        if not str(d.get("caption") or "").strip():
            topic = str(d.get("topic") or "").strip() or f"Day {d.get('day_index')}"
            d["caption"] = f"{topic}\n\n#industry #dailyupdate"
    for d in days[:7]:
        d["media_type"] = "image"
        d["source_meta"]["video"] = "uploaded"
        if not str(d.get("image_asset_id") or "").strip():
            try:
                asset = _generate_ai_image_asset(
                    str(d.get("topic") or "").strip(),
                    industry=str(plan.get("industry") or ""),
                    main_topic=seed,
                )
                d["image_asset_id"] = str(asset.get("id") or "")
                d["source_meta"]["image"] = "ai"
            except Exception:
                d["source_meta"]["image"] = "uploaded"
        if bool(plan.get("review_required", True)):
            d["status"] = "pending_review"
            d["review"]["status"] = "pending"
            d["review"]["reviewed_at"] = ""
        else:
            d["status"] = "approved"
            d["review"]["status"] = "approved"
            d["review"]["reviewed_at"] = _now_utc_z()
    return _media_preflight_report(auto)


def _generate_campaign_day_content(auto: dict, day: dict, *, topic_seed: str = "") -> dict:
    """Fill one campaign day with topic/caption and media placeholders."""
    plan, days = _ensure_campaign_shape(auto)
    seed = str(topic_seed or plan.get("topic_seed") or auto.get("topic_seed") or "").strip()
    if not seed:
        raise ValueError("topic_seed required for AI campaign generation")
    existing = [str(d.get("topic") or "").strip() for d in days if str(d.get("topic") or "").strip()]
    if not str(day.get("topic") or "").strip():
        day["topic"] = _generate_topics_from_seed(seed, existing, count=1)[0]
    topic = str(day.get("topic") or "").strip()
    if not str(day.get("caption") or "").strip():
        day["caption"] = f"{topic}\n\n#industry #dailyupdate"
    media_type = str(day.get("media_type") or "image").strip().lower()
    if media_type in ("", "none"):
        media_type = "image"
    day["media_type"] = media_type
    industry = str(plan.get("industry") or "").strip()
    if media_type in ("image", "mixed") and plan.get("image_source") == "ai":
        if _campaign_ai_images_enabled():
            try:
                image_asset = _generate_ai_image_asset(topic, industry=industry, main_topic=seed)
                day["image_asset_id"] = str(image_asset.get("id") or "")
                day["source_meta"]["image"] = "ai"
            except Exception:
                day["image_asset_id"] = f"ai_pending_image_day{day['day_index']}"
                day["source_meta"]["image"] = "ai"
        else:
            day["image_asset_id"] = str(day.get("image_asset_id") or "").strip()
            day["source_meta"]["image"] = "uploaded"
    # v1 scope: AI video disabled; keep any existing uploaded video id.
    if media_type in ("video", "mixed"):
        day["source_meta"]["video"] = "uploaded"
    if bool(plan.get("review_required", True)):
        day["status"] = "pending_review"
        day["review"]["status"] = "pending"
        day["review"]["reviewed_at"] = ""
    else:
        day["status"] = "approved"
        day["review"]["status"] = "approved"
        day["review"]["reviewed_at"] = _now_utc_z()
    return day


def _resolve_media_asset_path(asset_id: str) -> str:
    aid = str(asset_id or "").strip()
    if not aid or aid.startswith("ai_pending_"):
        return ""
    _idx, by_id = _campaign_asset_maps()
    asset = by_id.get(aid)
    if not asset:
        return ""
    rel = str(asset.get("relative_path") or "").strip()
    if not rel:
        return ""
    abs_path = BASE_DIR / rel
    if not abs_path.exists():
        return ""
    return str(abs_path)


def _normalize_whatsapp_phone(raw: str) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    # If user enters a local 10-digit India number, prefix country code.
    if len(digits) == 10:
        return "91" + digits
    return digits


def _resolve_whatsapp_number(workflow_name: str, runtime_vars: Optional[dict] = None) -> str:
    """
    Resolve WhatsApp number in priority order:
    1) Runtime vars from the current run
    2) Saved per-workflow automation setting
    3) Environment fallback
    """
    runtime_vars = runtime_vars or {}
    for key in ("WHATSAPP_NOTIFY_PHONE", "WHATSAPP_NUMBER", "AR_WHATSAPP_NUMBER"):
        val = _normalize_whatsapp_phone(runtime_vars.get(key) or "")
        if val:
            return val
    try:
        wf_path = WORKFLOWS_DIR / f"{workflow_name}.json"
        if wf_path.exists():
            wf = json.loads(wf_path.read_text())
            auto = wf.get("automation") or {}
            if isinstance(auto, dict):
                saved = _normalize_whatsapp_phone(auto.get("whatsapp_number") or "")
                if saved:
                    return saved
    except Exception:
        pass
    return _normalize_whatsapp_phone((os.environ.get("TRAINER_WHATSAPP_NOTIFY_NUMBER") or "").strip())


def _detect_workflow_platforms(workflow_name: str) -> list[str]:
    """
    Best-effort platform detection from workflow name + saved step text/URLs.
    Returns ordered unique values from: facebook, instagram, linkedin, x, whatsapp.
    """
    haystacks: list[str] = [str(workflow_name or "")]
    try:
        wf_path = WORKFLOWS_DIR / f"{workflow_name}.json"
        if wf_path.exists():
            wf = json.loads(wf_path.read_text())
            for step in wf.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                haystacks.append(str(step.get("description") or ""))
                haystacks.append(str(step.get("url") or ""))
                haystacks.append(str(step.get("ai_prompt") or ""))
                haystacks.append(str(step.get("type_text") or ""))
                haystacks.append(str(step.get("shell_command") or ""))
    except Exception:
        pass
    blob = "\n".join(haystacks).lower()
    out: list[str] = []
    checks = (
        ("facebook", ("facebook", "fb.com", "facebook.com")),
        ("instagram", ("instagram", "insta", "instagram.com")),
        ("linkedin", ("linkedin", "linkedin.com", "linked in")),
        ("x", ("x.com", "twitter", "tweet")),
        ("whatsapp", ("whatsapp", "wa.me", "web.whatsapp.com")),
    )
    for name, needles in checks:
        if any(n in blob for n in needles):
            out.append(name)
    return out


def _completion_workflow_label(workflow_name: str) -> str:
    """
    Name shown in the WhatsApp completion line. Prefer the ``workflow_name`` field inside the
    JSON on disk (same as the project name in the UI); fall back to the run's workflow key.
    """
    key = (workflow_name or "").strip()
    if not key:
        return "Run"
    try:
        p = WORKFLOWS_DIR / f"{key}.json"
        if p.exists():
            data = json.loads(p.read_text())
            label = (data.get("workflow_name") or "").strip()
            if label:
                return label
    except Exception:
        pass
    return key


def _workflow_type_project_typed_text(wf: dict, run_file_key: str) -> str:
    """
    String used for ``type_project_name`` steps: the workflow's saved project name
    (``workflow_name`` in JSON), falling back to the filesystem key used to load the file.
    """
    return (str(wf.get("workflow_name") or "").strip() or (run_file_key or "").strip())


def _workflow_trainer_whatsapp_number_digits(wf: dict) -> str:
    """
    Digits for ``type_whatsapp_number``: number saved for this workflow in
    ``automation.whatsapp_number`` (Trainer "WhatsApp number (this workflow)"), normalized.
    """
    auto = wf.get("automation") if isinstance(wf.get("automation"), dict) else {}
    return _normalize_whatsapp_phone(auto.get("whatsapp_number") or "")


def _build_whatsapp_completion_link(
    workflow_name: str,
    *,
    source: str,
    dry_run: bool,
    mode: str,
    ok_steps: int,
    err_steps: int,
    step_results: Optional[list[dict[str, Any]]] = None,
    runtime_vars: Optional[dict] = None,
    error: str = "",
) -> tuple[str, str]:
    """
    Build prefilled WhatsApp text + ``/send`` URL.

    **Success (default, non-verbose):** the same one-line template for *every* workflow; only the
    workflow name and the clock readout change:
    ``✅ <WorkflowName> ar™ done ✅ at HH:MM for YYYY-MM-DD``

    **Failure (default, non-verbose):** the parallel template, then a short follow-up with either
    the first failing step (preferred) and/or a run-level exception (when present):
    ``❌ <WorkflowName> ar™ failed ❌ at HH:MM for YYYY-MM-DD`` then one or two detail lines.

    **Verbose** (``TRAINER_WHATSAPP_COMPLETION_VERBOSE=1``): longer success/failure for debugging.
    """
    wlabel = _completion_workflow_label(workflow_name)
    success = (not error) and err_steps == 0
    when_txt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    run_kind = "DRY" if dry_run else "LIVE"
    verbose = _env_truthy("TRAINER_WHATSAPP_COMPLETION_VERBOSE", "0")
    hm = datetime.datetime.now().strftime("%H:%M")
    ymd = datetime.datetime.now().strftime("%Y-%m-%d")

    if success and not verbose:
        # ✅ <WorkflowName> ar™ done ✅ at HH:MM for YYYY-MM-DD  (wlabel, %H:%M, %Y-%m-%d)
        msg = f"✅ {wlabel} ar™ done ✅ at {hm} for {ymd}"
    elif success and verbose:
        run_kind_l = "DRY RUN" if dry_run else "LIVE"
        mode_txt = (str(mode or "smart").strip().upper() or "SMART")
        platforms = _detect_workflow_platforms(workflow_name)
        plat_hint = f" · {', '.join(platforms)}" if platforms else ""
        msg = (
            f"✓ {wlabel} — all {ok_steps} step(s) OK · {run_kind_l} {mode_txt}{plat_hint} · "
            f"{when_txt} · {source}"
        )
    else:
        if not verbose:
            # Mirror the success one-liner, then the most useful error signal(s).
            er = (error or "").strip()
            parts2: list[str] = [f"❌ {wlabel} ar™ failed ❌ at {hm} for {ymd}"]
            first_fail = ""
            for s in step_results or []:
                if str(s.get("status") or "").strip().lower() != "error":
                    continue
                num = s.get("step")
                et = str(s.get("error") or "").strip()[:200]
                act = str(s.get("action") or "step").strip()
                first_fail = f"Step {num} · {act}: {et}" if num is not None else f"{act}: {et}"
                break
            if first_fail:
                parts2.append(first_fail)
            if er and er not in (first_fail or ""):
                # Run-level error (e.g. crash before/after a step) — not duplicating the step line.
                if len(parts2) == 1:
                    parts2.append(f"Run: {er[:220]}")
                else:
                    parts2.append(f"Run: {er[:180]}")
            if len(parts2) == 1:
                parts2.append(
                    f"{ok_steps} step(s) completed · {err_steps} error(s) · {run_kind}"
                    if err_steps
                    else f"Run stopped before completion · {run_kind}"
                )
            msg = "\n".join(parts2)
        else:
            run_kind_l = "DRY RUN" if dry_run else "LIVE"
            mode_txt = (str(mode or "smart").strip().upper() or "SMART")
            parts: list[str] = [
                f"❌ {wlabel} ar™ failed at {when_txt} · {run_kind_l} {mode_txt} · {source}",
                f"OK {ok_steps} · errors {err_steps}",
            ]
            er = (error or "").strip()
            if er:
                parts.append(f"Run: {er[:220]}")
            first_fail = ""
            for step in step_results or []:
                if str(step.get("status") or "").strip().lower() != "error":
                    continue
                num = step.get("step")
                action = str(step.get("action") or "step").strip()
                et = str(step.get("error") or "").strip()[:160]
                first_fail = (
                    f"First error — step {num} ({action}): {et}"
                    if num is not None
                    else f"First error — {action}: {et}"
                )
                break
            if first_fail:
                parts.append(first_fail)
            msg = "\n".join(parts)
    phone = _resolve_whatsapp_number(workflow_name, runtime_vars)
    try:
        max_url_chars = int((os.environ.get("TRAINER_WHATSAPP_URL_TEXT_MAX_CHARS") or "2000").strip() or "2000")
    except (TypeError, ValueError):
        max_url_chars = 2000
    max_url_chars = max(400, min(max_url_chars, 12000))
    url_msg = msg
    if len(msg) > max_url_chars:
        url_msg = msg[: max_url_chars - 80].rstrip() + "\n…(truncated for WhatsApp URL length)"
        print(
            f"      ⚠ WhatsApp completion URL text truncated "
            f"({len(msg)} → {len(url_msg)} chars); full text still in completion message for copy/send-workflow."
        )
    if phone:
        url = (
            "https://web.whatsapp.com/send"
            f"?phone={phone}&text={quote(url_msg)}&lang=en&locale=en_US"
        )
    else:
        url = f"https://web.whatsapp.com/send?text={quote(url_msg)}&lang=en&locale=en_US"
    return msg, url


def _trainer_whatsapp_completion_store_runtime(
    workflow_name: str,
    *,
    results: list,
    mode: str,
    runtime_vars: dict,
    dry_run: bool,
) -> tuple[str, str]:
    """
    Build WhatsApp completion text + URL and set ``WHATSAPP_COMPLETION_*`` scratch vars.

    ``results`` is the in-memory step list for **this invocation only** (so the message
    reflects this run, not prior runs). ``type_completion_message`` rebuilds from
    ``results`` at that step, then ``pbcopy`` + paste only there (no earlier clipboard
    staging). After typing it clears ``WHATSAPP_COMPLETION_TEXT`` so it is not reused as
    ``LAST_TYPED_TEXT``.
    """
    ok_steps = sum(1 for s in results if str(s.get("status") or "") in ("ok", "dry_run"))
    err_steps = sum(1 for s in results if str(s.get("status") or "") == "error")
    msg, url = _build_whatsapp_completion_link(
        workflow_name,
        source=(runtime_vars.get("RUN_SOURCE") or "manual_run"),
        dry_run=dry_run,
        mode=mode,
        ok_steps=ok_steps,
        err_steps=err_steps,
        step_results=results,
        runtime_vars=runtime_vars,
        error="",
    )
    runtime_vars["WHATSAPP_COMPLETION_TEXT"] = msg
    runtime_vars["WHATSAPP_COMPLETION_URL"] = url
    runtime_vars["LAST_COMPLETION_URL"] = url
    return msg, url


# Keys that must never carry across workflow runs via runtime_vars_seed — only steps in the
# current run may set them (completion_link / completion_message).
_COMPLETION_SCRATCH_KEYS = frozenset(
    {
        "WHATSAPP_COMPLETION_TEXT",
        "WHATSAPP_COMPLETION_URL",
        "LAST_COMPLETION_URL",
        "WHATSAPP_COMPLETION_URL_COPIED",
        "WHATSAPP_COMPLETION_MESSAGE_COPIED",
    }
)


def _trainer_scrub_seeded_completion_scratch(runtime_vars: dict) -> None:
    """Remove completion scratch keys so only this run's completion steps repopulate them."""
    for k in _COMPLETION_SCRATCH_KEYS:
        runtime_vars.pop(k, None)


def _trainer_note_whatsapp_web_send_open(url: str) -> None:
    global _TRAINER_LAST_WHATSAPP_WEB_SEND_URL
    low = (url or "").lower()
    if "web.whatsapp.com" in low and "/send" in low:
        _TRAINER_LAST_WHATSAPP_WEB_SEND_URL = url


def _trainer_clear_whatsapp_web_send_memory() -> None:
    global _TRAINER_LAST_WHATSAPP_WEB_SEND_URL
    _TRAINER_LAST_WHATSAPP_WEB_SEND_URL = None


def _whatsapp_send_url_identity(url: Optional[str]) -> Optional[tuple[str, str]]:
    """
    Canonical (phone_digits, decoded text) for web.whatsapp.com/send URLs; else None.
    Ignores lang/locale query noise.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    host = (p.netloc or "").lower().split(":")[0]
    if "web.whatsapp.com" not in host:
        return None
    path = (p.path or "").lower()
    if "/send" not in path:
        return None
    qs = parse_qs(p.query or "", keep_blank_values=True)

    def _first(key: str) -> str:
        for k in (key, key.upper()):
            if k in qs and qs[k]:
                return str(qs[k][0] or "").strip()
        return ""

    phone = _normalize_whatsapp_phone(_first("phone"))
    text_enc = _first("text")
    try:
        text_dec = unquote(text_enc) if text_enc else ""
    except Exception:
        text_dec = text_enc or ""
    return (phone, text_dec)


def _whatsapp_send_urls_equivalent(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    ia = _whatsapp_send_url_identity(a)
    ib = _whatsapp_send_url_identity(b)
    return ia is not None and ia == ib


def _trainer_chrome_front_tab_url_darwin() -> Optional[str]:
    if platform.system() != "Darwin":
        return None
    script = (
        'tell application "Google Chrome"\n'
        "\tif not (running) then return \"\"\n"
        "\ttry\n"
        "\t\treturn (URL of active tab of front window) as text\n"
        "\tend try\n"
        "\treturn \"\"\n"
        "end tell\n"
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        u = (r.stdout or "").strip()
        return u if u else None
    except Exception:
        return None


def _whatsapp_skip_redundant_chrome_open(notify_url: str) -> bool:
    """
    True when Chrome already shows the same web.whatsapp.com/send chat as ``notify_url``,
    so opening again would trigger a second navigation / reload after the compose field is ready.
    """
    if not _env_truthy("TRAINER_WHATSAPP_SKIP_REDUNDANT_OPEN", "1"):
        return False
    if _whatsapp_send_urls_equivalent(notify_url, _TRAINER_LAST_WHATSAPP_WEB_SEND_URL):
        return True
    cur = _trainer_chrome_front_tab_url_darwin()
    if cur and _whatsapp_send_urls_equivalent(notify_url, cur):
        return True
    return False


def _activate_chrome_for_whatsapp_notify() -> None:
    """Bring Google Chrome to the front so Tab/Enter go to WhatsApp Web."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            d = float((os.environ.get("TRAINER_WHATSAPP_CHROME_ACTIVATE_DELAY") or "0.7").strip() or "0.7")
        except (TypeError, ValueError):
            d = 0.7
        time.sleep(max(0.1, min(5.0, d)))
        return
    if sys_name == "Windows":
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(New-Object -ComObject WScript.Shell).AppActivate('Google Chrome')",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
            time.sleep(0.7)
        except Exception:
            time.sleep(0.5)


def _trainer_activate_chrome_before_whatsapp_keys(reason: str) -> None:
    """
    Bring Google Chrome to the foreground before WhatsApp Web typing or Tab/Space navigation.

    The Trainer is often started from Terminal/Cursor while the Run button lives in a browser
    tab; keyboard focus then stays on that browser or the IDE, so PyAutoGUI steps look \"ok\"
    but nothing happens in Chrome. Opt out with TRAINER_ACTIVATE_CHROME_BEFORE_WHATSAPP_STEPS=0.
    """
    if not _env_truthy("TRAINER_ACTIVATE_CHROME_BEFORE_WHATSAPP_STEPS", "1"):
        return
    if platform.system() not in ("Darwin", "Windows"):
        return
    _activate_chrome_for_whatsapp_notify()
    print(f"      (WhatsApp automation: Google Chrome activated — {reason})")


def _trainer_whatsapp_web_nav_maybe_activate_chrome(
    runtime_vars: dict, action_name: str
) -> None:
    """After typing web.whatsapp.com, activate Chrome once before the first Tab/Space toward search."""
    if not _env_truthy("TRAINER_ACTIVATE_CHROME_BEFORE_WHATSAPP_STEPS", "1"):
        return
    if runtime_vars.get("_WHATSAPP_WEB_NAV_NEED_CHROME") != "1":
        return
    if runtime_vars.get("_WHATSAPP_WEB_NAV_ACTIVATED") == "1":
        return
    if action_name not in ("press_tab", "press_space"):
        return
    sys_name = platform.system()
    if sys_name not in ("Darwin", "Windows"):
        return
    _trainer_activate_chrome_before_whatsapp_keys(
        f"before first {action_name} toward WhatsApp search/chat"
    )
    runtime_vars["_WHATSAPP_WEB_NAV_ACTIVATED"] = "1"


def _whatsapp_refocus_chrome_for_keys(phase: str) -> None:
    """
    Re-activate Chrome so Tab/Enter are delivered to WhatsApp after its own reload / in-page focus steal.
    ``phase`` is ``after_load`` (after page settle wait) or ``before_tab`` (after the first 5s gap, before Tab×N).
    """
    if not _env_truthy("TRAINER_WHATSAPP_ACTIVATE_CHROME", "1"):
        return
    if phase == "after_load" and not _env_truthy("TRAINER_WHATSAPP_REFOCUS_AFTER_PAGE_LOAD", "1"):
        return
    if phase == "before_tab" and not _env_truthy("TRAINER_WHATSAPP_REFOCUS_BEFORE_TAB", "1"):
        return
    _activate_chrome_for_whatsapp_notify()
    try:
        rs = float((os.environ.get("TRAINER_WHATSAPP_REFOCUS_SETTLE_SEC") or "0.5").strip() or "0.5")
    except (TypeError, ValueError):
        rs = 0.5
    if rs > 0:
        time.sleep(max(0.0, min(3.0, rs)))
    print(f"      (WhatsApp notify: refocused Chrome for keys — {phase})")


def _whatsapp_notify_maximize_chrome_before_keys() -> None:
    """
    Match the workflow ``maximize`` step so the window is big and stable before Tab/Enter:
    macOS ``Ctrl+Cmd+F`` (fullscreen), Windows / Linux best-effort ``Win+Up`` (same as ``run_workflow``).
    """
    if not _env_truthy("TRAINER_WHATSAPP_MAXIMIZE_BEFORE_KEYS", "1"):
        return
    try:
        settle = float((os.environ.get("TRAINER_WHATSAPP_MAXIMIZE_SETTLE_SEC") or "0.45").strip() or "0.45")
    except (TypeError, ValueError):
        settle = 0.45
    settle = max(0.0, min(2.0, settle))
    sys_name = platform.system()
    if sys_name == "Darwin":
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Google Chrome" to activate',
                    "-e",
                    "delay 0.2",
                    "-e",
                    'tell application "System Events" to keystroke "f" using {control down, command down}',
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            print("      (WhatsApp notify: maximized/fullscreen Chrome (Ctrl+Cmd+F) before Tab/Enter)")
        except Exception as e:
            print(f"      ⚠ WhatsApp notify: maximize skipped: {e}")
    else:
        try:
            import pyautogui as _pg

            _trainer_avoid_pyautogui_failsafe(_pg)
            _pg.hotkey("win", "up")
            print("      (WhatsApp notify: maximized window (Win+Up) before Tab/Enter)")
        except Exception as e:
            print(f"      ⚠ WhatsApp notify: maximize skipped: {e}")
    if settle > 0:
        time.sleep(settle)


def _whatsapp_notify_inter_step_pause() -> None:
    """
    Fixed pause between major WhatsApp notify actions.

    WhatsApp Web often reloads or steals focus; Chrome may still be launching when the
    /send URL is opened. Extra gaps reduce flaky missed sends. Override seconds with
    ``TRAINER_WHATSAPP_INTER_STEP_PAUSE_SEC`` (default 5).
    """
    try:
        sec = float((os.environ.get("TRAINER_WHATSAPP_INTER_STEP_PAUSE_SEC") or "5").strip() or "5")
    except (TypeError, ValueError):
        sec = 5.0
    sec = max(0.0, min(120.0, sec))
    if sec > 0:
        time.sleep(sec)


def _whatsapp_notify_open_chrome_then_url(url: str) -> None:
    """
    Notify step 1–2: launch Google Chrome, then open the WhatsApp Web ``/send`` URL (prefilled body).
    """
    sys_name = platform.system()
    try:
        gap = float((os.environ.get("TRAINER_WHATSAPP_LAUNCH_GAP_SEC") or "1.2").strip() or "1.2")
    except (TypeError, ValueError):
        gap = 1.2
    gap = max(0.35, min(5.0, gap))
    if sys_name == "Darwin":
        subprocess.Popen(["open", "-a", "Google Chrome"])
        time.sleep(gap)
        _whatsapp_notify_inter_step_pause()
        # IMPORTANT: `open --args <url>` only passes args when launching Chrome fresh.
        # If Chrome is already running, that form can fail to navigate to the URL, so
        # we open the URL directly via `open -a Google Chrome <url>`.
        subprocess.Popen(["open", "-a", "Google Chrome", url])
        _whatsapp_notify_inter_step_pause()
        return
    if sys_name == "Windows":
        subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
        time.sleep(gap)
        _whatsapp_notify_inter_step_pause()
        subprocess.Popen(["cmd", "/c", "start", "", "chrome", url])
        _whatsapp_notify_inter_step_pause()
        return
    subprocess.Popen(["google-chrome"])
    time.sleep(gap)
    _whatsapp_notify_inter_step_pause()
    subprocess.Popen(["google-chrome", url])
    _whatsapp_notify_inter_step_pause()


def _whatsapp_notify_post_open_sequence(
    *,
    phone_digits: str,
    notify_url: Optional[str] = None,
    notify_message: Optional[str] = None,
) -> None:
    """
    WhatsApp notify keyboard sequence after the prefilled ``/send`` URL is showing.

    **Default (URLs built by this app):** the open URL already includes ``phone=`` and ``text=`` with
    the completion message. Running Tab to search + retyping the phone + Enter was intended for
    older flows but on current WhatsApp Web it often **moves focus away** from the compose box and
    **wipes the prefilled text**.

    This function **skips** that sequence when we know a body was set: ``notify_message`` (the same
    string URL-encoded as ``text=``) is non-empty, and/or a re-parse of ``notify_url`` shows
    non-empty ``text=``. This avoids a rare bug where re-parsing the URL alone can miss the param and
    the key sequence would run anyway. Set ``TRAINER_WHATSAPP_POST_OPEN_KEY_SEQUENCE=1`` to force
    the legacy key sequence.

    After page settle (``TRAINER_WHATSAPP_PAGE_LOAD_WAIT_SEC``), optionally **maximize / fullscreen**
    Chrome (same as workflow ``maximize``) right before Tab/Enter when
    ``TRAINER_WHATSAPP_MAXIMIZE_BEFORE_KEYS=1``. Then each major action is followed by
    ``TRAINER_WHATSAPP_STEP_WAIT_SEC`` (default **5s**): first wait after the prefilled chat is up,
    then after Tab×N, after typing the mobile, after the first Enter, and after the second Enter
    (send). Tab keycodes still use a short 0.12s gap between them.

    Chrome is left open by default; set ``TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND=1`` to quit
    after send. Steps 1–2 (open Chrome + URL) run in ``_send_whatsapp_run_notification`` first.

    Disable: ``TRAINER_WHATSAPP_PRESS_ENTER_AFTER_OPEN=0`` or ``TRAINER_WHATSAPP_POST_OPEN_SEQUENCE=0``.
    """
    if (os.environ.get("TRAINER_WHATSAPP_POST_OPEN_SEQUENCE") or "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    if (os.environ.get("TRAINER_WHATSAPP_PRESS_ENTER_AFTER_OPEN") or "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    # When the /send URL has phone= and text= prefilled (the normal case), WhatsApp Web opens the
    # chat with focus already on the compose box. We only need to press Enter to send. The legacy
    # Tab + retype phone flow was for old-style URLs without text= — and it could wipe the draft.
    text_prefilled = bool((notify_message or "").strip())
    if not text_prefilled and (notify_url or "").strip():
        ident = _whatsapp_send_url_identity(notify_url.strip())
        if ident and (ident[1] or "").strip():
            text_prefilled = True
    force_legacy = _env_truthy("TRAINER_WHATSAPP_POST_OPEN_KEY_SEQUENCE", "0")
    send_only_mode = text_prefilled and not force_legacy
    pd = _normalize_whatsapp_phone(phone_digits or "")
    if not pd and not send_only_mode:
        print("      ⚠ WhatsApp notify: empty phone — cannot type digits; skipping keyboard sequence")
        return

    # Same step gap for PyAutoGUI + System Events (seconds between major sub-steps, default 5).
    try:
        sw = float((os.environ.get("TRAINER_WHATSAPP_STEP_WAIT_SEC") or "5").strip() or "5")
    except (TypeError, ValueError):
        sw = 5.0
    sw = max(0.0, min(120.0, sw))
    try:
        n_tabs = int((os.environ.get("TRAINER_WHATSAPP_TAB_COUNT") or "4").strip() or "4")
    except (TypeError, ValueError):
        n_tabs = 4
    n_tabs = max(0, min(40, n_tabs))

    try:
        if _env_truthy("TRAINER_WHATSAPP_ACTIVATE_CHROME", "1"):
            _activate_chrome_for_whatsapp_notify()
        try:
            t_load = float((os.environ.get("TRAINER_WHATSAPP_PAGE_LOAD_WAIT_SEC") or "2").strip() or "2")
        except (TypeError, ValueError):
            t_load = 2.0
        t_load = max(0.0, min(30.0, t_load))
        if t_load > 0:
            time.sleep(t_load)
        # WhatsApp Web may reload/steal focus; bring Chrome to front again before the keyboard sub-flow.
        _whatsapp_refocus_chrome_for_keys("after_load")
        _whatsapp_notify_inter_step_pause()
        if platform.system() == "Darwin" and _env_truthy("TRAINER_WHATSAPP_DARWIN_APPLESCRIPT_KEYS", "1"):
            # Keys via System Events + Chrome activate in-script (slower but reliable when PyAutoGUI misses Chrome).
            if sw > 0:
                time.sleep(sw)
                print(f"      (WhatsApp notify: waited {sw:g}s after prefilled URL / page)")
            _whatsapp_refocus_chrome_for_keys("before_tab")
            _whatsapp_notify_inter_step_pause()
            _whatsapp_notify_maximize_chrome_before_keys()
            _whatsapp_notify_inter_step_pause()
            _darwin_whatsapp_notify_keys_system_events(
                tabs=0 if send_only_mode else n_tabs,
                phone_digits="" if send_only_mode else pd,
                step_wait=sw,
                send_only=send_only_mode,
            )
            _whatsapp_notify_inter_step_pause()
            if send_only_mode:
                print(
                    "      (WhatsApp notify: text was prefilled in /send URL → pressed Enter to send "
                    "(skipped Tab + retype phone to avoid clearing the draft). "
                    "Set TRAINER_WHATSAPP_POST_OPEN_KEY_SEQUENCE=1 for the legacy Tab→type→Enter flow.)"
                )
            else:
                print(
                    "      (WhatsApp notify: keys via AppleScript; set TRAINER_WHATSAPP_DARWIN_APPLESCRIPT_KEYS=0 to use PyAutoGUI)"
                )
        else:
            # Import first so a failure falls through to System Events without an extra 5s already applied.
            import pyautogui as _pg

            _trainer_avoid_pyautogui_failsafe(_pg)
            _release_modifier_keys(_pg)
            _release_nav_keys(_pg)

            if sw > 0:
                time.sleep(sw)
                print(f"      (WhatsApp notify: waited {sw:g}s after prefilled URL / page)")

            _whatsapp_refocus_chrome_for_keys("before_tab")
            _whatsapp_notify_inter_step_pause()
            _whatsapp_notify_maximize_chrome_before_keys()
            _whatsapp_notify_inter_step_pause()

            if send_only_mode:
                # Compose box already has the prefilled text and the focus — just send.
                _pg.press("enter")
                print(
                    "      (WhatsApp notify: Enter — send prefilled message "
                    "(skipped Tab + retype phone because text= was in the URL))"
                )
                if sw > 0:
                    time.sleep(sw)
                    print(f"      (WhatsApp notify: waited {sw:g}s after send)")
                _whatsapp_notify_inter_step_pause()
            else:
                for _ in range(n_tabs):
                    _pg.press("tab")
                    time.sleep(0.12)
                if sw > 0:
                    time.sleep(sw)
                    print(f"      (WhatsApp notify: waited {sw:g}s after Tab ×{n_tabs})")

                _pg.write(pd, interval=0.03)
                _release_modifier_keys(_pg)
                _release_nav_keys(_pg)
                if sw > 0:
                    time.sleep(sw)
                    print(
                        f"      (WhatsApp notify: waited {sw:g}s after typing {len(pd)} mobile digit(s))"
                    )

                _pg.press("enter")
                print("      (WhatsApp notify: Enter — confirm / focus)")

                if sw > 0:
                    time.sleep(sw)
                    print(f"      (WhatsApp notify: waited {sw:g}s before send)")

                _pg.press("enter")
                print("      (WhatsApp notify: Enter — send prefilled message)")

                if sw > 0:
                    time.sleep(sw)
                    print(f"      (WhatsApp notify: waited {sw:g}s after send)")
                _whatsapp_notify_inter_step_pause()
    except Exception as e:
        # If PyAutoGUI is missing or blocked on macOS, fall back to System Events keystrokes.
        if platform.system() == "Darwin":
            try:
                _whatsapp_notify_maximize_chrome_before_keys()
                _whatsapp_notify_inter_step_pause()
                _darwin_whatsapp_notify_keys_system_events(
                    tabs=0 if send_only_mode else n_tabs,
                    phone_digits="" if send_only_mode else pd,
                    step_wait=sw,
                    send_only=send_only_mode,
                )
                _whatsapp_notify_inter_step_pause()
                print("      (WhatsApp notify: System Events fallback — same step timing)")
            except Exception as se:
                print(f"      ⚠ WhatsApp notify keyboard sequence skipped: {e} (fallback failed: {se})")
        else:
            print(f"      ⚠ WhatsApp notify keyboard sequence skipped: {e}")

    quit_chrome = _env_truthy("TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND", "0")
    if not quit_chrome:
        if _env_truthy("TRAINER_WHATSAPP_OPEN_BLANK_TAB_AFTER_SEND", "0"):
            try:
                _trainer_chrome_open_blank_tab_best_effort()
                print("      (WhatsApp notify: opened blank tab; Chrome left running)")
            except Exception as e:
                print(f"      ⚠ WhatsApp notify blank tab skipped: {e}")
        else:
            print("      (WhatsApp notify: left Chrome open (default) — set TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND=1 to quit after send")
        return
    try:
        w_close = float((os.environ.get("TRAINER_WHATSAPP_WAIT_BEFORE_CLOSE_SEC") or "1").strip() or "1")
    except (TypeError, ValueError):
        w_close = 1.0
    w_close = max(0.0, min(120.0, w_close))
    if w_close > 0:
        time.sleep(w_close)
    try:
        _trainer_clear_whatsapp_web_send_memory()
        _trainer_close_google_chrome()
        print("      (WhatsApp notify: closed Google Chrome — TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND=1)")
    except Exception as e:
        print(f"      ⚠ WhatsApp notify close Chrome skipped: {e}")


def _trainer_chrome_open_blank_tab_best_effort() -> None:
    """Open a new blank tab in Google Chrome; leave the app running (macOS / Windows best-effort)."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        script = (
            'tell application "Google Chrome"\n'
            "  if not (running) then return\n"
            "  activate\n"
            '  tell front window to make new tab with properties {URL:"about:blank"}\n'
            "end tell"
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=20,
            capture_output=True,
            text=True,
        )
        return
    if sys_name == "Windows":
        subprocess.Popen(
            ["cmd", "/c", "start", "", "chrome", "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.Popen(
        ["google-chrome", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_url_prefer_chrome(url: str, *, new_chrome_window: bool = True) -> None:
    """
    Open URL in Google Chrome when available, fallback to default browser.

    When ``new_chrome_window`` is False (e.g. WhatsApp notify), macOS opens the URL in Chrome
    without ``--new-window`` so the existing session is reused — fewer navigations than a
    blank window plus ``/send`` (WhatsApp's own reloads are outside our control).
    """
    try:
        sys_name = platform.system()
        if sys_name == "Darwin":
            if new_chrome_window:
                subprocess.Popen(
                    ["open", "-a", "Google Chrome", "--args", "--lang=en-US", "--new-window", url]
                )
            else:
                subprocess.Popen(["open", "-a", "Google Chrome", "--args", "--lang=en-US", url])
            return
        if sys_name == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", "chrome", "--lang=en-US", url])
            return
        # Linux best-effort
        subprocess.Popen(["google-chrome", "--lang=en-US", url])
        return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _send_whatsapp_run_notification(
    workflow_name: str,
    *,
    source: str,
    dry_run: bool,
    mode: str = "",
    steps: Optional[list[dict]] = None,
    error: str = "",
    runtime_vars: Optional[dict] = None,
) -> None:
    """
    Steps 1–2: launch Chrome and open the WhatsApp ``/send`` URL with a concise prefilled report.
    Then ``_whatsapp_notify_post_open_sequence`` may run Tab → type phone → Enter (see env below). Chrome
    stays open unless ``TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND=1``.

    When the ``/send`` URL already includes a non-empty ``text=`` (normal case), the post sequence is **skipped**
    by default — the old Tab + retype phone + Enter flow moved focus and often **cleared** the prefilled
    message on WhatsApp Web. Set ``TRAINER_WHATSAPP_POST_OPEN_KEY_SEQUENCE=1`` to restore that sequence.

    Optional ``TRAINER_WHATSAPP_SEND_WORKFLOW`` runs first; duplicate ``/send`` opens are skipped when possible.

    **Default:** notify is **off** (``TRAINER_WHATSAPP_NOTIFY`` unset or ``0``). Use workflow steps
    (``completion_message`` / ``completion_link`` + ``type_completion_message``) for WhatsApp instead.
    Set ``TRAINER_WHATSAPP_NOTIFY=1`` to restore this legacy end-of-run automation.
    """
    enabled = (os.environ.get("TRAINER_WHATSAPP_NOTIFY") or "0").strip().lower() in ("1", "true", "yes")
    if not enabled:
        return
    # Dry run does not move the real mouse/keyboard, but WhatsApp notify *does* open Chrome — that felt like
    # "WhatsApp started instead of the workflow". Off by default; set TRAINER_WHATSAPP_NOTIFY_ON_DRY_RUN=1 to test notify.
    if dry_run and not _env_truthy("TRAINER_WHATSAPP_NOTIFY_ON_DRY_RUN", "0"):
        print("      (WhatsApp notify: skipped for DRY RUN — uncheck Dry run for live Instagram steps, or set TRAINER_WHATSAPP_NOTIFY_ON_DRY_RUN=1)")
        return
    steps_list = steps or []
    ok_steps = sum(1 for s in steps_list if str(s.get("status") or "") in ("ok", "dry_run"))
    err_steps = sum(1 for s in steps_list if str(s.get("status") or "") == "error")
    status = "FAILED" if error or err_steps > 0 else "COMPLETED"
    msg, url = _build_whatsapp_completion_link(
        workflow_name,
        source=source,
        dry_run=dry_run,
        mode=mode,
        ok_steps=ok_steps,
        err_steps=err_steps,
        step_results=steps_list,
        error=error,
    )
    phone = _resolve_whatsapp_number(workflow_name, runtime_vars)
    if not phone:
        return
    send_wf = (os.environ.get("TRAINER_WHATSAPP_SEND_WORKFLOW") or "").strip()
    send_wf_path = WORKFLOWS_DIR / f"{send_wf}.json" if send_wf else None
    if send_wf and not (send_wf_path and send_wf_path.is_file()):
        print(
            f"      ⚠ TRAINER_WHATSAPP_SEND_WORKFLOW='{send_wf}' not found in workflows/ — either add "
            f"workflows/{send_wf}.json or remove that line from .env — using open-chat notify only"
        )
        send_wf = ""
    if send_wf:
        try:
            run_workflow(
                send_wf,
                dry_run=False,
                runtime_vars_seed={
                    "WHATSAPP_NOTIFY_URL": url,
                    "WHATSAPP_NOTIFY_TEXT": msg,
                    "WHATSAPP_NOTIFY_PHONE": phone,
                    "WHATSAPP_NOTIFY_WORKFLOW": workflow_name,
                    "WHATSAPP_NOTIFY_STATUS": status,
                },
                run_mode="smart",
            )
            _whatsapp_notify_inter_step_pause()
            skip_nav = _whatsapp_skip_redundant_chrome_open(url)
            if skip_nav:
                print(
                    "      (WhatsApp notify: skipping duplicate Chrome open — "
                    "send-workflow already on this web.whatsapp.com/send URL)"
                )
            else:
                _whatsapp_notify_open_chrome_then_url(url)
                _trainer_note_whatsapp_web_send_open(url)
            _whatsapp_notify_post_open_sequence(
                phone_digits=phone, notify_url=url, notify_message=msg
            )
            return
        except Exception as e:
            print(f"      ⚠ WhatsApp send-workflow '{send_wf}' failed: {e}; falling back to open-chat only.")
    skip_nav = _whatsapp_skip_redundant_chrome_open(url)
    if skip_nav:
        print(
            "      (WhatsApp notify: skipping duplicate Chrome open — "
            "workflow already opened this web.whatsapp.com/send URL; no second navigation)"
        )
    else:
        _whatsapp_notify_open_chrome_then_url(url)
        _trainer_note_whatsapp_web_send_open(url)
    _whatsapp_notify_post_open_sequence(phone_digits=phone, notify_url=url, notify_message=msg)


def _valid_hhmm(s: str) -> bool:
    if not isinstance(s, str) or len(s) != 5 or s[2] != ":":
        return False
    try:
        hh = int(s[:2])
        mm = int(s[3:])
    except Exception:
        return False
    return 0 <= hh <= 23 and 0 <= mm <= 59


def _env_truthy(key: str, default: str = "0") -> bool:
    return (os.environ.get(key) or default).strip().lower() in ("1", "true", "yes")


def _coerce_optional_bool(val: Any) -> Optional[bool]:
    """Parse JSON/env-style booleans; return None if missing or ambiguous."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return None


def _open_url_reuse_chrome_from_step(step: dict) -> bool:
    """
    When True, macOS opens the URL in Chrome without ``--new-window`` (same as WhatsApp notify).
    Per-step ``reuse_chrome_window`` overrides env TRAINER_OPEN_URL_REUSE_CHROME_WINDOW.
    """
    ob = _coerce_optional_bool(step.get("reuse_chrome_window"))
    if ob is not None:
        return ob
    return _env_truthy("TRAINER_OPEN_URL_REUSE_CHROME_WINDOW", "0")


# ``open_whatsapp`` step: WhatsApp Web home (not a prefilled ``/send`` URL).
_OPEN_WHATSAPP_WEB_URL = "https://web.whatsapp.com/"


def _scheduler_uses_media_campaign() -> bool:
    """
    When True, the scheduler may run the 30-day media campaign path (separate from
    plain workflow automation). Default off so schedule = run workflow + topic queue only.
    """
    return _env_truthy("TRAINER_AUTOMATION_USE_MEDIA_CAMPAIGN", "0")


def _scheduler_uses_topic_ai() -> bool:
    """When True, scheduler may call OpenAI to refill topics_pending. Default off."""
    return _env_truthy("TRAINER_AUTOMATION_TOPIC_AI", "0")


def _campaign_ai_images_enabled() -> bool:
    """When True, campaign content generation may call image APIs. Default off."""
    return _env_truthy("TRAINER_CAMPAIGN_AI_IMAGES", "0")


def _automation_next_run_is_due(next_raw: str, now_local: datetime.datetime) -> bool:
    """
    True if next_run_at is due *during this tool session*.

    Default behavior is **no catch-up**:
    - If the tool was closed and a scheduled time was missed, we do NOT run it automatically on reopen.
    - Catch-up is only enabled when TRAINER_SCHEDULER_CATCH_UP_MISSED_RUNS=1.

    Handles ISO strings with 'Z' or offsets so comparison matches the user's local clock.
    """
    s = str(next_raw or "").strip()
    if not s:
        return False
    try:
        if len(s) > 1 and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        nxt = datetime.datetime.fromisoformat(s)
        if nxt.tzinfo is not None:
            nxt = nxt.astimezone(None).replace(tzinfo=None)
        nl = now_local.replace(tzinfo=None) if now_local.tzinfo else now_local
        if nxt > nl:
            return False
        # If scheduler session start is known, skip catch-up runs by default.
        started = _TRAINER_SCHEDULER_SESSION_STARTED_AT
        if started and not _env_truthy("TRAINER_SCHEDULER_CATCH_UP_MISSED_RUNS", "0"):
            st = started.replace(tzinfo=None) if started.tzinfo else started
            if nxt < st:
                return False
        return True
    except Exception:
        return False


def _parse_next_run_local(next_raw: str, now_local: datetime.datetime) -> Optional[datetime.datetime]:
    """Parse next_run_at to a naive local datetime; return None if missing/unparsable."""
    s = str(next_raw or "").strip()
    if not s:
        return None
    try:
        if len(s) > 1 and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        nxt = datetime.datetime.fromisoformat(s)
        if nxt.tzinfo is not None:
            nxt = nxt.astimezone(None).replace(tzinfo=None)
        return nxt
    except Exception:
        return None


def _compute_next_run_local(automation: dict, now_local: Optional[datetime.datetime] = None) -> str:
    now = now_local or datetime.datetime.now()
    mode = str(automation.get("mode") or "daily").strip().lower()
    if mode == "minutes":
        try:
            interval_m = int(automation.get("interval_minutes", 5))
        except Exception:
            interval_m = 5
        interval_m = max(1, min(interval_m, 1440))
        nxt = now + datetime.timedelta(minutes=interval_m)
        return nxt.isoformat()
    if mode == "hourly":
        try:
            interval = int(automation.get("interval_hours", 24))
        except Exception:
            interval = 24
        interval = max(1, min(interval, 168))
        nxt = now + datetime.timedelta(hours=interval)
        return nxt.isoformat()
    if mode == "weekly":
        wt = automation.get("weekly_times") or {}
        if not isinstance(wt, dict):
            wt = {}
        day_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        today_idx = now.weekday()
        candidates: list[datetime.datetime] = []
        for i, d in enumerate(day_order):
            raw = str(wt.get(d) or "").strip()
            if not _valid_hhmm(raw):
                continue
            hh = int(raw[:2])
            mm = int(raw[3:])
            days_ahead = (i - today_idx) % 7
            cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + datetime.timedelta(days=days_ahead)
            if cand <= now:
                cand += datetime.timedelta(days=7)
            candidates.append(cand)
        if candidates:
            return min(candidates).isoformat()
        mode = "daily"
    raw_daily = str(automation.get("daily_time") or "21:00").strip()
    if not _valid_hhmm(raw_daily):
        raw_daily = "21:00"
    hh = int(raw_daily[:2])
    mm = int(raw_daily[3:])
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return nxt.isoformat()


def _topic_key(text: str) -> str:
    return re.sub(r"\W+", "", (text or "").strip().lower())


def _completed_topic_strings(auto: dict) -> list[str]:
    out: list[str] = []
    for x in (auto.get("topics_completed") or []):
        if isinstance(x, dict):
            t = str(x.get("topic") or "").strip()
        else:
            t = str(x).strip()
        if t:
            out.append(t)
    return out


def _dedupe_topics(candidates: list[str], banned: set[str], limit: int = 30) -> list[str]:
    uniq: list[str] = []
    seen = set(banned)
    for raw in candidates:
        t = str(raw).strip()
        if not t:
            continue
        k = _topic_key(t)
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(t)
        if len(uniq) >= limit:
            break
    return uniq


def _generate_topics_from_seed(topic_seed: str, completed: list[str], count: int = 30) -> list[str]:
    """
    Ask OpenAI for unique post-topic ideas for a single theme.
    """
    from openai import OpenAI

    seed = (topic_seed or "").strip()
    if not seed:
        raise ValueError("topic_seed is required to auto-generate topics")
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for topic auto-generation")

    banned = {_topic_key(x) for x in completed if _topic_key(x)}
    model = (os.environ.get("TRAINER_TOPIC_MODEL") or "gpt-4o-mini").strip()
    max_tokens = int((os.environ.get("TRAINER_TOPIC_MAX_TOKENS") or "900").strip() or "900")
    max_tokens = max(200, min(max_tokens, 2000))

    user_text = (
        f"Main topic: {seed}\n"
        f"Need: {count} unique LinkedIn post topic titles.\n"
        "Rules: short, actionable, non-duplicate ideas.\n"
        "Avoid these already-used topics exactly or semantically:\n"
        + ("\n".join(f"- {x}" for x in completed[-200:]) if completed else "- (none)")
        + "\n\nReturn JSON array only, e.g. [\"Topic 1\", \"Topic 2\"]"
    )
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.7,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate topic titles only. Return valid JSON array of strings and nothing else."
                ),
            },
            {"role": "user", "content": user_text},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    arr_raw = m.group(0) if m else raw
    try:
        parsed = json.loads(arr_raw)
    except Exception as exc:
        raise RuntimeError(f"topic generation returned invalid JSON: {raw[:240]}") from exc
    if not isinstance(parsed, list):
        raise RuntimeError("topic generation did not return a JSON array")
    out = _dedupe_topics([str(x) for x in parsed], banned, limit=count)
    if len(out) < count:
        # Deterministic fallback to ensure queue can proceed.
        i = 1
        while len(out) < count:
            cand = f"{seed} - daily angle {i}"
            if _topic_key(cand) not in banned and _topic_key(cand) not in {_topic_key(x) for x in out}:
                out.append(cand)
            i += 1
    return out[:count]


def _ensure_topics_queue(auto: dict, *, count: int = 30) -> tuple[bool, str]:
    """
    Ensure there are pending topics. Returns (changed, error_message).
    """
    pending = [str(t).strip() for t in (auto.get("topics_pending") or []) if str(t).strip()]
    auto["topics_pending"] = pending[:30]
    if auto["topics_pending"]:
        auto["auto_last_error"] = ""
        return False, ""
    if not bool(auto.get("auto_generate_topics", True)):
        return False, ""
    seed = str(auto.get("topic_seed") or "").strip()
    if not seed:
        return False, "topic_seed is required when auto-generate is enabled and queue is empty"
    completed = _completed_topic_strings(auto)
    gen = _generate_topics_from_seed(seed, completed, count=count)
    auto["topics_pending"] = gen[:30]
    auto["auto_last_error"] = ""
    return True, ""


def _ensure_workflow_automation_shape(wf: dict) -> dict:
    auto = wf.get("automation") or {}
    if not isinstance(auto, dict):
        auto = {}
    auto.setdefault("enabled", False)
    auto.setdefault("mode", "daily")
    auto.setdefault("interval_minutes", 5)
    auto.setdefault("interval_hours", 24)
    auto.setdefault("daily_time", "21:00")
    auto.setdefault(
        "weekly_times",
        {"mon": "21:00", "tue": "", "wed": "", "thu": "", "fri": "", "sat": "", "sun": ""},
    )
    auto.setdefault("topics_pending", [])
    auto.setdefault("topics_completed", [])
    auto.setdefault("topic_seed", "")
    auto.setdefault("whatsapp_number", "")
    auto.setdefault("auto_generate_topics", False)
    auto.setdefault("auto_last_error", "")
    auto.setdefault("next_run_at", "")
    auto.setdefault("last_run_at", "")
    auto.setdefault("in_progress_topic", "")
    auto.setdefault("in_progress_day", 0)
    auto.setdefault("in_progress_started_at", "")
    try:
        auto["scheduler_run_count"] = max(0, int(auto.get("scheduler_run_count") or 0))
    except (TypeError, ValueError):
        auto["scheduler_run_count"] = 0
    auto["topics_pending"] = [
        str(t).strip() for t in (auto.get("topics_pending") or []) if str(t).strip()
    ][:30]
    auto["whatsapp_number"] = _normalize_whatsapp_phone(auto.get("whatsapp_number") or "")
    auto["topics_completed"] = auto.get("topics_completed") or []
    _ensure_campaign_shape(auto)
    wf["automation"] = auto
    return auto


def _bundle_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(raw or "").strip()).strip("._-")
    return slug


def _bundle_path(slug: str) -> Path:
    return AR_BUNDLES_DIR / f"{_bundle_slug(slug)}.json"


def _normalize_bundle_schedule(raw: dict) -> dict:
    sch = raw if isinstance(raw, dict) else {}
    mode = str(sch.get("mode") or "daily").strip().lower()
    if mode not in ("minutes", "hourly", "daily", "weekly"):
        mode = "daily"
    daily_time = str(sch.get("daily_time") or "21:00").strip() or "21:00"
    if not _valid_hhmm(daily_time):
        daily_time = "21:00"
    weekly_raw = sch.get("weekly_times") if isinstance(sch.get("weekly_times"), dict) else {}
    weekly_times = {"mon": "", "tue": "", "wed": "", "thu": "", "fri": "", "sat": "", "sun": ""}
    for day in weekly_times:
        val = str(weekly_raw.get(day) or "").strip()
        weekly_times[day] = val if (val and _valid_hhmm(val)) else ("" if val else "")
    try:
        im = int(sch.get("interval_minutes", 5))
    except Exception:
        im = 5
    try:
        ih = int(sch.get("interval_hours", 24))
    except Exception:
        ih = 24
    return {
        "enabled": bool(sch.get("enabled", False)),
        "mode": mode,
        "interval_minutes": max(1, min(im, 1440)),
        "interval_hours": max(1, min(ih, 168)),
        "daily_time": daily_time,
        "weekly_times": weekly_times,
    }


def _normalize_bundle(raw: dict) -> dict:
    src = raw if isinstance(raw, dict) else {}
    slug = _bundle_slug(src.get("slug") or src.get("display_name") or "")
    children: list[str] = []
    for item in (src.get("children") or []):
        nm = str(item or "").strip()
        if nm and nm not in children:
            children.append(nm)
    same_for_all = bool(src.get("notify_same_for_all_flows", True))
    notify_number = _normalize_whatsapp_phone(str(src.get("notify_number") or ""))
    raw_by_flow = src.get("notify_numbers_by_flow") if isinstance(src.get("notify_numbers_by_flow"), dict) else {}
    by_flow: dict[str, str] = {}
    for wf in children:
        n = _normalize_whatsapp_phone(str(raw_by_flow.get(wf) or ""))
        if n:
            by_flow[wf] = n
    notify_mode = str(src.get("notify_mode") or "").strip().lower()
    if notify_mode not in ("one", "per_flow", "from_workflows"):
        notify_mode = "one" if same_for_all else "per_flow"
    if notify_mode == "one":
        same_for_all = True
    elif notify_mode in ("per_flow", "from_workflows"):
        same_for_all = False
    return {
        "slug": slug,
        "display_name": str(src.get("display_name") or slug).strip() or slug,
        "children": children,
        "notify_mode": notify_mode,
        "notify_same_for_all_flows": same_for_all,
        "notify_number": notify_number,
        "notify_numbers_by_flow": by_flow,
        "schedule": _normalize_bundle_schedule(src.get("schedule") or {}),
        "scheduler_run_count": int(src.get("scheduler_run_count") or 0) if str(src.get("scheduler_run_count") or "0").isdigit() else 0,
        "last_run_at": str(src.get("last_run_at") or ""),
        "next_run_at": str(src.get("next_run_at") or ""),
        "last_run_results": src.get("last_run_results") if isinstance(src.get("last_run_results"), list) else [],
    }


def _bundle_notify_number_for_child(bundle: dict, child_name: str) -> str:
    """
    WhatsApp digits to inject for an ar™ child run.

    - notify_mode one: bundle ``notify_number`` for every child.
    - per_flow: ``notify_numbers_by_flow[child]``.
    - from_workflows: empty — caller/runner uses each workflow's saved automation number.
    Legacy bundles without ``notify_mode`` fall back to ``notify_same_for_all_flows``.
    """
    b = bundle if isinstance(bundle, dict) else {}
    mode = str(b.get("notify_mode") or "").strip().lower()
    if mode == "from_workflows":
        return ""
    if mode == "per_flow":
        by_flow = b.get("notify_numbers_by_flow") if isinstance(b.get("notify_numbers_by_flow"), dict) else {}
        return _normalize_whatsapp_phone(str(by_flow.get(child_name) or ""))
    if mode == "one":
        return _normalize_whatsapp_phone(str(b.get("notify_number") or ""))
    same = bool(b.get("notify_same_for_all_flows", True))
    if same:
        return _normalize_whatsapp_phone(str(b.get("notify_number") or ""))
    by_flow = b.get("notify_numbers_by_flow") if isinstance(b.get("notify_numbers_by_flow"), dict) else {}
    return _normalize_whatsapp_phone(str(by_flow.get(child_name) or ""))


def _bundle_next_run_local(bundle: dict, now_local: Optional[datetime.datetime] = None) -> str:
    now = now_local or datetime.datetime.now()
    sch = _normalize_bundle_schedule(bundle.get("schedule") or {})
    auto_like = {
        "mode": sch.get("mode", "daily"),
        "interval_minutes": sch.get("interval_minutes", 5),
        "interval_hours": sch.get("interval_hours", 24),
        "daily_time": sch.get("daily_time", "21:00"),
        "weekly_times": sch.get("weekly_times", {}),
    }
    return _compute_next_run_local(auto_like, now)


def _workflow_names_managed_by_enabled_ar_bundles() -> set[str]:
    """
    Names of workflows listed as children of any ar™ bundle whose schedule is enabled.

    Those flows are executed only on the bundle's schedule (or via manual ar™ / Run tab),
    not by per-workflow Automation — otherwise the scheduler can fire the same workflow
    again immediately after an ar™ run because its own ``next_run_at`` is still due.
    Set TRAINER_AR_CHILD_ALLOW_PARALLEL_AUTOMATION=1 to restore the old overlap behavior.
    """
    if _env_truthy("TRAINER_AR_CHILD_ALLOW_PARALLEL_AUTOMATION", "0"):
        return set()
    out: set[str] = set()
    for bfp in sorted(AR_BUNDLES_DIR.glob("*.json")):
        try:
            bundle = _normalize_bundle(json.loads(bfp.read_text()))
        except Exception:
            continue
        sch = _normalize_bundle_schedule(bundle.get("schedule") or {})
        if not bool(sch.get("enabled", False)):
            continue
        for ch in bundle.get("children") or []:
            k = str(ch or "").strip()
            if k:
                out.add(k)
    return out


def _cusear_default_ar_slug() -> str:
    return (os.environ.get("CUSEAR_DEFAULT_AR_SLUG") or "").strip()


def _list_bundles() -> list[dict]:
    out: list[dict] = []
    for fp in sorted(AR_BUNDLES_DIR.glob("*.json")):
        try:
            d = _normalize_bundle(json.loads(fp.read_text()))
            if not d.get("slug"):
                continue
            out.append(d)
        except Exception:
            continue
    flt = _cusear_default_ar_slug()
    if flt and _is_consumer_mode():
        out = [b for b in out if str(b.get("slug") or "") == flt]
    return out


def _consumer_ar_child_workflow_names() -> Optional[set[str]]:
    """When desktop is locked to one AR, only these workflows are listed for the consumer."""
    slug = _cusear_default_ar_slug()
    if not slug or not _is_consumer_mode():
        return None
    fp = _bundle_path(slug)
    if not fp.is_file():
        return set()
    try:
        with _BUNDLE_IO_LOCK:
            b = _normalize_bundle(json.loads(fp.read_text()))
        return {str(x or "").strip() for x in (b.get("children") or []) if str(x or "").strip()}
    except Exception:
        return set()


def _trainer_automation_auto_run_summary() -> dict[str, Any]:
    """
    Lightweight snapshot for the Trainer UI: whether background automation can fire,
    and how many schedules are enabled (matches scheduler scope where possible).
    """
    scheduler_enabled = _env_truthy("TRAINER_SCHEDULER_ENABLED", "1")
    allowed = _consumer_ar_child_workflow_names()
    ar_managed = _workflow_names_managed_by_enabled_ar_bundles()
    standalone_automation = 0
    for fp in sorted(WORKFLOWS_DIR.glob("*.json")):
        stem = fp.stem
        if allowed is not None and stem not in allowed:
            continue
        if stem in ar_managed:
            continue
        try:
            wf = json.loads(fp.read_text())
            auto = _ensure_workflow_automation_shape(wf)
            if bool(auto.get("enabled", False)):
                standalone_automation += 1
        except Exception:
            continue
    ar_schedules = 0
    for b in _list_bundles():
        sch = _normalize_bundle_schedule(b.get("schedule") or {})
        if bool(sch.get("enabled", False)):
            ar_schedules += 1
    return {
        "scheduler_enabled": scheduler_enabled,
        "standalone_automation_workflows": standalone_automation,
        "ar_bundle_schedules_enabled": ar_schedules,
    }


def _trainer_exports_dir() -> Path:
    p = BASE_DIR / "trainer_exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _desktop_export_allowed() -> bool:
    v = (os.environ.get("TRAINER_ALLOW_DESKTOP_EXPORT") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _pyinstaller_available_for_current_interpreter() -> bool:
    """
    True if this Python can run PyInstaller (same check the export script uses).
    Do not rely on a `pyinstaller` entry on PATH — many installs only expose `python -m PyInstaller`.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _desktop_export_capabilities() -> dict[str, Any]:
    import shutil as _sh

    plat = sys.platform
    pyi = _pyinstaller_available_for_current_interpreter()
    return {
        "pyinstaller": pyi,
        "host_os": plat,
        "can_build_mac": pyi and plat == "darwin",
        "can_build_win": pyi and plat.startswith("win"),
        "create_dmg_available": bool(_sh.which("create-dmg")) if plat == "darwin" else False,
        "export_allowed": _desktop_export_allowed(),
    }


def _export_desktop_exe_base(bundle_slug: str) -> str:
    base = f"{_bundle_slug(bundle_slug)}_cusear"
    if len(base) > 52:
        base = base[:52].rstrip("._-")
    return base or "ar_cusear"


def _best_ai_run_worker(job_id: str, platform: str, query: str, run_id: str) -> None:
    """Run ``best_ai/run_platform.py`` in a background thread (Playwright; can take minutes)."""
    import sys as _sys

    started = _utc_now_z()
    script = Path(__file__).resolve().parent / "best_ai" / "run_platform.py"
    env = dict(os.environ)
    rid = (run_id or "").strip()
    if rid:
        env["BEST_AI_RUN_ID"] = rid
    session_dir = ""
    code = -1
    err_tail = ""
    out_tail = ""
    try:
        if not script.is_file():
            with _BEST_AI_JOBS_LOCK:
                _BEST_AI_JOBS[job_id] = {
                    "status": "error",
                    "platform": platform,
                    "error": "best_ai/run_platform.py not found",
                    "started_at": started,
                    "finished_at": _utc_now_z(),
                }
            return
        cp = subprocess.run(
            [_sys.executable, str(script), "--platform", platform, "--query", query],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
        code = int(cp.returncode or 0)
        out = cp.stdout or ""
        err_tail = (cp.stderr or "")[-12000:]
        out_tail = out[-12000:]
        for line in out.splitlines():
            if line.startswith("BEST_AI_SESSION_DIR="):
                session_dir = line.split("=", 1)[1].strip()
                break
    except subprocess.TimeoutExpired:
        code = -9
        err_tail = "subprocess.TimeoutExpired (900s)"
    except Exception as exc:
        code = -1
        err_tail = str(exc)
    with _BEST_AI_JOBS_LOCK:
        _BEST_AI_JOBS[job_id] = {
            "status": "ok" if code == 0 else "error",
            "platform": platform,
            "returncode": code,
            "session_dir": session_dir,
            "stdout_tail": out_tail,
            "stderr_tail": err_tail,
            "started_at": started,
            "finished_at": _utc_now_z(),
        }


def _best_ai_synthesize_openai(query: str, responses: dict[str, str]) -> dict[str, Any]:
    """
    Merge pasted platform answers with an OpenAI judge model (Best AI™ synthesizer).
    """
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY is not set — add it to .env.local for the synthesizer.")
    from openai import OpenAI

    model = (os.environ.get("BEST_AI_SYNTH_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    q = (query or "").strip()
    if not q:
        raise ValueError("query is required")
    if len(q) > 12000:
        q = q[:12000] + "\n…[truncated]"
    pieces: list[tuple[str, str]] = []
    for k, raw in (responses or {}).items():
        pk = str(k or "").strip().lower().replace(" ", "_")
        if not pk:
            continue
        t = str(raw or "").strip()
        if not t:
            continue
        if len(t) > 48000:
            t = t[:48000] + "\n…[truncated]"
        pieces.append((pk, t))
    if not pieces:
        raise ValueError("Paste at least one non-empty platform answer.")
    user_blocks = [f"### User's original question\n{q}\n"]
    for pk, tv in pieces:
        user_blocks.append(f"### Answer from {pk.upper().replace('_', ' ')}\n{tv}")
    user_c = "\n\n".join(user_blocks)
    if len(user_c) > 180000:
        user_c = user_c[:180000] + "\n…[truncated]"
    sys_c = (
        "You are Best AI™, a synthesizer judge. The user asked ONE question (given at the top of the user message). "
        "They pasted answers from different AI assistants (typically ChatGPT, Gemini, and Claude — labels match the ### headers).\n\n"
        "Your job:\n"
        "1) Read every pasted answer and compare them for accuracy, completeness, clarity, and fit to the user's goal.\n"
        "2) Either (A) choose the single best answer as the primary outcome, OR (B) explain that you merged ideas from two or more answers into one new synthesized answer because that serves the user better than picking one verbatim, OR (C) state that one or more answers are weak, off-topic, or unreliable and should NOT be selected — then base the final output on the remaining strong answer(s) only.\n"
        "3) Always explain your logic plainly: why the winner won, why a merge was needed, or why an answer was excluded.\n\n"
        "Reply in Markdown with exactly these sections:\n"
        "## Verdict\n"
        "One short paragraph: which platform(s) you relied on, or that you produced a merged answer, or that you excluded a weak entry.\n\n"
        "## Best answer\n"
        "The final reply for the user (polished winning text, or your merged synthesis — this is what they ship or use).\n\n"
        "## Reasoning\n"
        "Bullet list: how you compared the inputs and why this outcome is best for the question.\n\n"
        "## Per-input notes\n"
        "For each pasted platform answer: one or two sentences on strengths, weaknesses, or why it was excluded.\n"
    )
    client = OpenAI(api_key=key)
    msg = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        temperature=0.2,
        messages=[
            {"role": "system", "content": sys_c},
            {"role": "user", "content": user_c},
        ],
    )
    text = (msg.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("OpenAI returned empty synthesis.")
    return {"synthesis": text, "model": model, "platforms_used": [p[0] for p in pieces]}


_BEST_AI_BRIDGE_LOCK = threading.Lock()


def _best_ai_platform_wrap_instructions_text() -> str:
    """Same bundle suffix as TRAINER.html BEST_AI_PLATFORM_WRAP_INSTRUCTIONS (keep in sync)."""
    return (
        "\n\n---\nFormatting instructions for your reply (follow after answering the question above):\n"
        "- Put the complete answer the user should reuse inside ONE markdown fenced code block (triple backticks). "
        "They will copy that single block back into another tool.\n"
        "- Keep prose outside the fence minimal (a short intro is fine). Do not split the deliverable across multiple code fences.\n"
    )


def _best_ai_bridge_path() -> Path:
    d = agency_root() / "sessions" / "best_ai"
    d.mkdir(parents=True, exist_ok=True)
    return d / "ui_bridge.json"


def _best_ai_default_bridge() -> dict[str, Any]:
    return {
        "topic": "",
        "slots": {"chatgpt": "", "gemini": "", "claude": ""},
        "synthesis": {"text": "", "model": "", "platforms_used": [], "at": ""},
        "rev": 0,
    }


def _best_ai_bridge_read_unlocked() -> dict[str, Any]:
    path = _best_ai_bridge_path()
    if not path.exists():
        return _best_ai_default_bridge()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _best_ai_default_bridge()
    if not isinstance(raw, dict):
        return _best_ai_default_bridge()
    base = _best_ai_default_bridge()
    base["topic"] = str(raw.get("topic") or "").strip()
    src_slots = raw.get("slots")
    if isinstance(src_slots, dict):
        for k in ("chatgpt", "gemini", "claude"):
            v = src_slots.get(k)
            base["slots"][k] = str(v) if v is not None else ""
    syn = raw.get("synthesis")
    if isinstance(syn, dict):
        base["synthesis"]["text"] = str(syn.get("text") or "")
        base["synthesis"]["model"] = str(syn.get("model") or "")
        pu = syn.get("platforms_used")
        base["synthesis"]["platforms_used"] = pu if isinstance(pu, list) else []
        base["synthesis"]["at"] = str(syn.get("at") or "")
    try:
        base["rev"] = int(raw.get("rev") or 0)
    except (TypeError, ValueError):
        base["rev"] = 0
    return base


def _best_ai_bridge_write_unlocked(data: dict[str, Any]) -> None:
    _best_ai_bridge_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _best_ai_bridge_get() -> dict[str, Any]:
    with _BEST_AI_BRIDGE_LOCK:
        return _best_ai_bridge_read_unlocked()


def _best_ai_bridge_mutate(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _BEST_AI_BRIDGE_LOCK:
        data = _best_ai_bridge_read_unlocked()
        mutator(data)
        try:
            data["rev"] = int(data.get("rev") or 0) + 1
        except (TypeError, ValueError):
            data["rev"] = 1
        _best_ai_bridge_write_unlocked(data)
        return dict(data)


def _desktop_export_worker(
    job_id: str,
    agency: Path,
    bundle_slug: str,
    platform_target: str,
    embed_keys: bool,
) -> None:
    res: dict[str, Any] = {"ok": False, "log": "", "artifact": "", "error": ""}
    try:
        script = Path(__file__).resolve().parent / "scripts" / "export_ar_desktop.py"
        job_dir = _trainer_exports_dir() / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        work_dir = job_dir / "pyi_staging"
        shutil.rmtree(work_dir, ignore_errors=True)
        exe_base = _export_desktop_exe_base(bundle_slug)
        if platform_target == "mac":
            artifact_path = job_dir / f"{exe_base}.dmg"
        else:
            artifact_path = job_dir / f"{exe_base}_win.zip"
        cmd = [
            sys.executable,
            str(script),
            "--agency-home",
            str(agency),
            "--bundle-slug",
            bundle_slug,
            "--platform-target",
            platform_target,
            "--artifact-out",
            str(artifact_path),
            "--work-dir",
            str(work_dir),
        ]
        if embed_keys:
            cmd.append("--embed-keys")
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=960,
        )
        raw_out = (proc.stdout or "").strip()
        parsed: dict[str, Any] = {}
        for line in reversed(raw_out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                break
        if not parsed:
            res = {
                "ok": False,
                "log": raw_out + "\n" + (proc.stderr or ""),
                "artifact": "",
                "error": "export script produced no JSON result",
            }
        else:
            res = parsed
            if proc.returncode != 0 and not res.get("error"):
                res["ok"] = False
                res["error"] = f"exit {proc.returncode}"
    except Exception as exc:
        res = {"ok": False, "log": "", "artifact": "", "error": str(exc)}
    with _DESKTOP_EXPORT_JOBS_LOCK:
        j = _DESKTOP_EXPORT_JOBS.get(job_id)
        if not j:
            return
        j["status"] = "ok" if res.get("ok") else "error"
        j["log"] = str(res.get("log") or "")
        j["error"] = str(res.get("error") or "")
        j["artifact"] = str(res.get("artifact") or "")
        j["finished_at"] = _utc_now_z()


def _scheduler_loop() -> None:
    """
    Periodically runs due workflows based on per-workflow automation settings.
    """
    while True:
        try:
            now_local = datetime.datetime.now()
            ar_managed_children = _workflow_names_managed_by_enabled_ar_bundles()
            for fp in sorted(WORKFLOWS_DIR.glob("*.json")):
                wf_name = fp.stem
                if wf_name in ar_managed_children:
                    continue
                due_topic = ""
                due_day_idx = 0
                runtime_seed: dict[str, str] = {}
                run_mode = "smart"
                will_run = False
                with _WORKFLOW_IO_LOCK:
                    try:
                        wf = json.loads(fp.read_text())
                    except Exception:
                        continue
                    auto = _ensure_workflow_automation_shape(wf)
                    if not bool(auto.get("enabled", False)):
                        continue
                    plan, days = _ensure_campaign_shape(auto)
                    campaign_enabled = bool(plan.get("enabled", False)) and _scheduler_uses_media_campaign()
                    in_prog = bool(str(auto.get("in_progress_topic") or "").strip()) or int(
                        auto.get("in_progress_day") or 0
                    ) > 0
                    if in_prog:
                        raw_started = str(auto.get("in_progress_started_at") or "").strip()
                        stale = not raw_started
                        if raw_started:
                            try:
                                rs = raw_started
                                if len(rs) > 1 and rs.endswith("Z"):
                                    rs = rs[:-1] + "+00:00"
                                st = datetime.datetime.fromisoformat(rs)
                                if st.tzinfo is not None:
                                    st = st.astimezone(None).replace(tzinfo=None)
                                nl = (
                                    now_local.replace(tzinfo=None)
                                    if now_local.tzinfo
                                    else now_local
                                )
                                stale = (nl - st) > datetime.timedelta(minutes=45)
                            except Exception:
                                stale = True
                        if stale:
                            auto["in_progress_topic"] = ""
                            auto["in_progress_day"] = 0
                            auto["in_progress_started_at"] = ""
                            _, _days_r = _ensure_campaign_shape(auto)
                            for d in _days_r:
                                if str(d.get("status") or "") == "scheduled":
                                    d["status"] = "approved"
                            fp.write_text(json.dumps(wf, indent=2))
                        else:
                            continue
                    next_raw = str(auto.get("next_run_at") or "").strip()
                    # Reliability: never "catch up" missed runs by default.
                    # If next_run_at is missing/unparsable OR was missed while the tool was closed,
                    # we reschedule to the next future run time and do not execute now.
                    nxt = _parse_next_run_local(next_raw, now_local)
                    if nxt is None:
                        auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                        auto["auto_last_error"] = str(auto.get("auto_last_error") or "")
                        fp.write_text(json.dumps(wf, indent=2))
                        continue
                    started = _TRAINER_SCHEDULER_SESSION_STARTED_AT
                    if started and not _env_truthy("TRAINER_SCHEDULER_CATCH_UP_MISSED_RUNS", "0"):
                        st = started.replace(tzinfo=None) if started.tzinfo else started
                        if nxt < st:
                            auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                            fp.write_text(json.dumps(wf, indent=2))
                            continue
                    due = _automation_next_run_is_due(next_raw, now_local)
                    if not due:
                        continue
                    if campaign_enabled:
                        pending_days = [d for d in days if str(d.get("status") or "") not in ("posted", "scheduled")]
                        if not pending_days:
                            auto["enabled"] = False
                            auto["next_run_at"] = ""
                            auto["auto_last_error"] = ""
                            fp.write_text(json.dumps(wf, indent=2))
                            continue
                        target = pending_days[0]
                        if plan.get("mode") == "ai_daily_auto":
                            try:
                                _generate_campaign_day_content(auto, target)
                            except Exception as e:
                                auto["auto_last_error"] = str(e)
                                auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                                fp.write_text(json.dumps(wf, indent=2))
                                continue
                        if bool(plan.get("review_required", True)):
                            review = target.get("review") if isinstance(target.get("review"), dict) else {}
                            if str(review.get("status") or "pending") != "approved":
                                auto["auto_last_error"] = (
                                    f"Campaign blocked: day {target.get('day_index')} requires review approval"
                                )
                                auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                                fp.write_text(json.dumps(wf, indent=2))
                                continue
                        media_type = str(target.get("media_type") or "image").strip().lower()
                        image_path = _resolve_media_asset_path(str(target.get("image_asset_id") or ""))
                        video_path = _resolve_media_asset_path(str(target.get("video_asset_id") or ""))
                        if media_type in ("image", "mixed") and not image_path:
                            auto["auto_last_error"] = (
                                f"Campaign blocked: day {target.get('day_index')} image file is missing"
                            )
                            auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                            fp.write_text(json.dumps(wf, indent=2))
                            continue
                        if media_type in ("video", "mixed") and not video_path:
                            auto["auto_last_error"] = (
                                f"Campaign blocked: day {target.get('day_index')} video file is missing"
                            )
                            auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                            fp.write_text(json.dumps(wf, indent=2))
                            continue
                        due_day_idx = int(target.get("day_index") or 0)
                        auto["in_progress_day"] = due_day_idx
                        auto["in_progress_started_at"] = now_local.isoformat()
                        target["status"] = "scheduled"
                        auto["auto_last_error"] = ""
                        runtime_seed = {
                            "CURRENT_TOPIC": str(target.get("topic") or ""),
                            "CURRENT_CAPTION": str(target.get("caption") or ""),
                            "CURRENT_MEDIA_TYPE": media_type,
                            "CURRENT_IMAGE_PATH": image_path,
                            "CURRENT_VIDEO_PATH": video_path,
                            "CURRENT_MEDIA_PATH": image_path or video_path,
                            "CURRENT_CAMPAIGN_DAY": str(due_day_idx),
                        }
                        uses_ai = bool(plan.get("image_source") == "ai")
                        run_mode = "smart" if uses_ai else "fast"
                    else:
                        changed_topics = False
                        topic_error = ""
                        if bool(auto.get("auto_generate_topics", False)) and _scheduler_uses_topic_ai():
                            try:
                                changed_topics, topic_error = _ensure_topics_queue(auto, count=30)
                            except Exception as e:
                                topic_error = str(e)
                            if topic_error:
                                auto["auto_last_error"] = topic_error
                                auto["next_run_at"] = _compute_next_run_local(auto, now_local)
                                fp.write_text(json.dumps(wf, indent=2))
                                continue
                            if changed_topics:
                                fp.write_text(json.dumps(wf, indent=2))
                        seed_fallback = str(auto.get("topic_seed") or "").strip()
                        if not auto["topics_pending"]:
                            if seed_fallback:
                                due_topic = seed_fallback
                                auto["in_progress_topic"] = due_topic
                                auto["in_progress_started_at"] = now_local.isoformat()
                                runtime_seed = {"CURRENT_TOPIC": due_topic}
                                run_mode = "smart"
                                auto["auto_last_error"] = topic_error or ""
                            else:
                                due_topic = ""
                                auto["in_progress_topic"] = ""
                                auto["in_progress_started_at"] = now_local.isoformat()
                                runtime_seed = {}
                                run_mode = "smart"
                                auto["auto_last_error"] = ""
                        else:
                            due_topic = auto["topics_pending"].pop(0)
                            auto["in_progress_topic"] = due_topic
                            auto["in_progress_started_at"] = now_local.isoformat()
                            runtime_seed = {"CURRENT_TOPIC": due_topic}
                            run_mode = "smart"
                            auto["auto_last_error"] = ""
                    will_run = True
                    run_n = int(auto.get("scheduler_run_count") or 0) + 1
                    runtime_seed["CURRENT_AUTOMATION_RUN"] = str(run_n)
                    # Do not advance next_run_at / last_run_at until the run lock is taken and the
                    # workflow finishes — otherwise a busy lock skips the run but the schedule jumps ahead.
                    fp.write_text(json.dumps(wf, indent=2))
                if not will_run:
                    continue
                try:
                    lock_wait = float((os.environ.get("TRAINER_SCHEDULER_LOCK_WAIT_SECONDS") or "120").strip() or "120")
                except Exception:
                    lock_wait = 120.0
                lock_wait = max(5.0, min(lock_wait, 900.0))
                got_run_lock = _RUN_WORKFLOW_LOCK.acquire(blocking=True, timeout=lock_wait)
                if not got_run_lock:
                    with _WORKFLOW_IO_LOCK:
                        try:
                            wf = json.loads(fp.read_text())
                            auto = _ensure_workflow_automation_shape(wf)
                            lock_fail_dirty = False
                            if due_topic and auto.get("in_progress_topic") == due_topic:
                                auto["in_progress_topic"] = ""
                                auto["in_progress_started_at"] = ""
                                auto["topics_pending"] = [due_topic] + list(auto.get("topics_pending") or [])
                                lock_fail_dirty = True
                            if int(auto.get("in_progress_day") or 0) == due_day_idx and due_day_idx > 0:
                                auto["in_progress_day"] = 0
                                auto["in_progress_started_at"] = ""
                                _plan, _days = _ensure_campaign_shape(auto)
                                for d in _days:
                                    if int(d.get("day_index") or 0) == due_day_idx and str(d.get("status") or "") == "scheduled":
                                        d["status"] = "approved"
                                        break
                                lock_fail_dirty = True
                            if not due_topic and not due_day_idx and str(auto.get("in_progress_started_at") or "").strip():
                                auto["in_progress_started_at"] = ""
                                lock_fail_dirty = True
                            if lock_fail_dirty:
                                fp.write_text(json.dumps(wf, indent=2))
                        except Exception:
                            pass
                    continue
                run_results: list[dict] = []
                run_error = ""
                try:
                    _RUN_STOP_EVENT.clear()
                    run_results = run_workflow(
                        wf_name,
                        dry_run=False,
                        runtime_vars_seed=runtime_seed,
                        run_mode=run_mode,
                        run_source="automation",
                    )
                    save_run_audit(
                        wf_name,
                        False,
                        run_results,
                        error=None,
                    )
                except Exception as e:
                    run_error = str(e)
                    save_run_audit(wf_name, False, run_results, error=run_error)
                finally:
                    _RUN_WORKFLOW_LOCK.release()
                if not _stop_requested():
                    _send_whatsapp_run_notification(
                        wf_name,
                        source="automation",
                        dry_run=False,
                        mode=run_mode,
                        steps=run_results,
                        error=run_error,
                        runtime_vars=runtime_seed,
                    )
                with _WORKFLOW_IO_LOCK:
                    try:
                        wf = json.loads(fp.read_text())
                        auto = _ensure_workflow_automation_shape(wf)
                        if due_topic and auto.get("in_progress_topic") == due_topic:
                            auto["in_progress_topic"] = ""
                            auto["in_progress_started_at"] = ""
                        if int(auto.get("in_progress_day") or 0) == due_day_idx:
                            auto["in_progress_day"] = 0
                            auto["in_progress_started_at"] = ""
                            _plan, _days = _ensure_campaign_shape(auto)
                            for d in _days:
                                if int(d.get("day_index") or 0) == due_day_idx:
                                    d["status"] = "failed" if run_error else "posted"
                                    if run_error:
                                        d["review"]["notes"] = run_error
                                    break
                        completed = list(auto.get("topics_completed") or [])
                        completed.append({
                            "topic": due_topic or runtime_seed.get("CURRENT_TOPIC", ""),
                            "day_index": due_day_idx or None,
                            "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
                            "status": "error" if run_error else "ok",
                            "error": run_error,
                        })
                        auto["topics_completed"] = completed[-200:]
                        now_done = datetime.datetime.now()
                        auto["last_run_at"] = now_done.isoformat()
                        if due_day_idx:
                            _plan, _days = _ensure_campaign_shape(auto)
                            if not [d for d in _days if str(d.get("status") or "") not in ("posted", "scheduled")]:
                                auto["enabled"] = False
                                auto["next_run_at"] = ""
                            else:
                                auto["next_run_at"] = _compute_next_run_local(auto, now_done)
                        else:
                            # Schedule-only or topic-queue: keep automation on; next interval from run end.
                            auto["next_run_at"] = _compute_next_run_local(auto, now_done)
                        try:
                            auto["scheduler_run_count"] = int(auto.get("scheduler_run_count") or 0) + 1
                        except (TypeError, ValueError):
                            auto["scheduler_run_count"] = 1
                        fp.write_text(json.dumps(wf, indent=2))
                    except Exception:
                        pass
            for bfp in sorted(AR_BUNDLES_DIR.glob("*.json")):
                with _BUNDLE_IO_LOCK:
                    try:
                        bundle = _normalize_bundle(json.loads(bfp.read_text()))
                    except Exception:
                        continue
                    if not bundle.get("slug"):
                        continue
                    sch = _normalize_bundle_schedule(bundle.get("schedule") or {})
                    if not bool(sch.get("enabled", False)):
                        continue
                    now_local = datetime.datetime.now()
                    next_raw = str(bundle.get("next_run_at") or "").strip()
                    nxt = _parse_next_run_local(next_raw, now_local)
                    if nxt is None:
                        bundle["next_run_at"] = _bundle_next_run_local(bundle, now_local)
                        bfp.write_text(json.dumps(bundle, indent=2))
                        continue
                    if not _automation_next_run_is_due(next_raw, now_local):
                        continue
                    if not bundle.get("children"):
                        bundle["next_run_at"] = _bundle_next_run_local(bundle, now_local)
                        bfp.write_text(json.dumps(bundle, indent=2))
                        continue
                got_run_lock = _RUN_WORKFLOW_LOCK.acquire(blocking=False)
                if not got_run_lock:
                    continue
                try:
                    run_results: list[dict[str, Any]] = []
                    stop_now = False
                    bundle_run_n = int(bundle.get("scheduler_run_count") or 0) + 1
                    _RUN_STOP_EVENT.clear()
                    for child in bundle.get("children") or []:
                        if _stop_requested():
                            stop_now = True
                            run_results.append(
                                {
                                    "child": child,
                                    "status": "stopped",
                                    "started_at": _utc_now_z(),
                                    "finished_at": _utc_now_z(),
                                    "error": "Stopped by user",
                                }
                            )
                            break
                        st = _utc_now_z()
                        try:
                            child_steps = run_workflow(
                                child,
                                dry_run=False,
                                runtime_vars_seed={
                                    "CURRENT_AUTOMATION_RUN": str(bundle_run_n),
                                    "CURRENT_AR_BUNDLE_RUN": str(bundle_run_n),
                                    "CURRENT_AR_BUNDLE_SLUG": str(bundle.get("slug") or ""),
                                    "CURRENT_AR_BUNDLE_NAME": str(bundle.get("display_name") or bundle.get("slug") or ""),
                                    "CURRENT_AR_FLOW_NAME": str(bundle.get("display_name") or bundle.get("slug") or ""),
                                    "WHATSAPP_NOTIFY_PHONE": _bundle_notify_number_for_child(bundle, child),
                                },
                                run_mode="smart",
                                run_source="ar_bundle",
                            )
                            child_errs = [s for s in child_steps if s.get("status") == "error"]
                            run_results.append(
                                {
                                    "child": child,
                                    "status": "error" if child_errs else "ok",
                                    "started_at": st,
                                    "finished_at": _utc_now_z(),
                                    "error": child_errs[0].get("error", "") if child_errs else "",
                                }
                            )
                        except Exception as e:
                            run_results.append(
                                {
                                    "child": child,
                                    "status": "error",
                                    "started_at": st,
                                    "finished_at": _utc_now_z(),
                                    "error": str(e),
                                }
                            )
                    with _BUNDLE_IO_LOCK:
                        try:
                            latest_bundle = _normalize_bundle(json.loads(bfp.read_text()))
                        except Exception:
                            latest_bundle = bundle
                        latest_bundle["last_run_results"] = run_results[-200:]
                        latest_bundle["last_run_at"] = _utc_now_z()
                        try:
                            latest_bundle["scheduler_run_count"] = int(latest_bundle.get("scheduler_run_count") or 0) + 1
                        except (TypeError, ValueError):
                            latest_bundle["scheduler_run_count"] = 1
                        if stop_now:
                            sch2 = _normalize_bundle_schedule(latest_bundle.get("schedule") or {})
                            sch2["enabled"] = False
                            latest_bundle["schedule"] = sch2
                            latest_bundle["next_run_at"] = ""
                        else:
                            latest_bundle["next_run_at"] = _bundle_next_run_local(latest_bundle, datetime.datetime.now())
                        bfp.write_text(json.dumps(latest_bundle, indent=2))
                finally:
                    _RUN_WORKFLOW_LOCK.release()
        except Exception:
            pass
        try:
            poll = float((os.environ.get("TRAINER_SCHEDULER_POLL_SECONDS") or "5").strip() or "5")
        except Exception:
            poll = 5.0
        time.sleep(max(2.0, min(poll, 120.0)))


def _trainer_close_google_chrome() -> None:
    """Quit Google Chrome / Chromium (best-effort; no error if it is not running)."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        script = 'tell application "Google Chrome"\n  if running then quit\nend tell'
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=45,
            capture_output=True,
            text=True,
        )
    elif sys_name == "Windows":
        subprocess.run(
            ["taskkill", "/IM", "chrome.exe", "/F", "/T"],
            check=False,
            timeout=60,
            capture_output=True,
        )
    else:
        for argv in (
            ["pkill", "-x", "google-chrome"],
            ["pkill", "-x", "google-chrome-stable"],
            ["pkill", "-x", "chromium"],
        ):
            subprocess.run(argv, check=False, timeout=20, capture_output=True)
    time.sleep(0.75)


def _trainer_order_steps_for_run(raw_steps: list) -> list:
    """
    Run steps in numeric ``step`` order (same order as the Trainer list), not raw JSON array order.

    If the UI ever saves rows out of sequence, array order would disagree with on-screen step numbers;
    sorting fixes that. Warns when ``step`` fields are missing or not 1…N.

    Special case: ai_image steps are always executed first at run start so generated files
    are available before later manual upload steps.
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        return []
    keyed: list[tuple[int, int, int, dict]] = []
    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            continue
        try:
            n = int(s.get("step"))
        except (TypeError, ValueError):
            n = 1_000_000 + i
        action = str(s.get("action_type") or s.get("action") or "").strip().lower()
        pri = 0 if action == "ai_image" else 1
        keyed.append((pri, n, i, s))
    keyed.sort(key=lambda t: (t[0], t[1], t[2]))
    ordered = [t[3] for t in keyed]
    nums: list[int] = []
    for s in ordered:
        try:
            nums.append(int(s.get("step")))
        except (TypeError, ValueError):
            nums.append(-1)
    if -1 in nums:
        print("  ⚠ Workflow has step(s) without a numeric \"step\" field; those were sorted last.")
    elif ordered:
        want = list(range(1, len(ordered) + 1))
        if sorted(nums) != want:
            print(
                "  ⚠ Step numbers in JSON are not exactly 1…N (gap or duplicate). "
                "Execution uses sorted step order; re-save in Trainer to renumber if the list looks wrong."
            )
    return ordered


def _downloads_dir() -> Path:
    """
    Best-effort user Downloads folder across macOS/Windows/Linux.
    """
    home = Path.home()
    cand = home / "Downloads"
    if cand.exists():
        return cand
    up = (os.environ.get("USERPROFILE") or "").strip()
    if up:
        win = Path(up) / "Downloads"
        if win.exists():
            return win
    # Fall back safely if Downloads does not exist.
    return cand


def _latest_download_file_by_exts(exts: list[str]) -> Optional[Path]:
    """Pick the most recently modified Downloads file matching extensions."""
    dl = _downloads_dir()
    try:
        if not dl.exists():
            return None
    except Exception:
        return None
    norm_exts = {("." + str(e).lower().lstrip(".")) for e in (exts or []) if str(e).strip()}
    if not norm_exts:
        return None
    best: Optional[Path] = None
    best_mtime = -1.0
    try:
        for p in dl.iterdir():
            try:
                if not p.is_file():
                    continue
                if p.suffix.lower() not in norm_exts:
                    continue
                mt = float(p.stat().st_mtime)
                if mt > best_mtime:
                    best = p
                    best_mtime = mt
            except Exception:
                continue
    except Exception:
        return None
    return best


def _save_ai_image_copy_to_downloads(source_abs_path: Path, *, topic: str = "") -> Path:
    """
    Copy generated AI image into Downloads for file-picker based upload steps.
    """
    dl = _downloads_dir()
    dl.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    base = _safe_slug(topic or source_abs_path.stem or "ai_image", "ai_image")
    out = dl / f"{stamp}_{base}_1080x1350.png"
    shutil.copy2(source_abs_path, out)
    return out


def _utc_now_z() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _stop_requested() -> bool:
    return _RUN_STOP_EVENT.is_set()


def _make_step_result(
    *,
    step_num: Any,
    action: str,
    status: str,
    started_at_z: str,
    error: str = "",
    detail: str = "",
) -> dict[str, Any]:
    finished_at_z = _utc_now_z()
    try:
        st = started_at_z
        if st.endswith("Z"):
            st = st[:-1] + "+00:00"
        et = finished_at_z
        if et.endswith("Z"):
            et = et[:-1] + "+00:00"
        dur_ms = max(
            0,
            int(
                (
                    datetime.datetime.fromisoformat(et)
                    - datetime.datetime.fromisoformat(st)
                ).total_seconds()
                * 1000.0
            ),
        )
    except Exception:
        dur_ms = 0
    out: dict[str, Any] = {
        "step": step_num,
        "action": action,
        "status": status,
        "started_at": started_at_z,
        "finished_at": finished_at_z,
        "duration_ms": dur_ms,
    }
    if error:
        out["error"] = error
    if detail:
        out["detail"] = detail
    return out


def run_workflow(
    name,
    dry_run: bool = False,
    runtime_vars_seed: Optional[dict] = None,
    run_mode: str = "smart",
    run_source: str = "manual_run",
):
    """Load workflow and replay each step (pyautogui UI actions + optional shell argv steps)."""
    import time, subprocess
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Workflow '{name}' not found")
    wf = json.loads(path.read_text())
    try:
        from agency_api.entitlements import assert_run_allowed_local

        assert_run_allowed_local(workflow_name=name, run_mode=run_mode, wf=wf)
    except PermissionError as _pe:
        raise
    raw = wf.get("steps", [])
    if not isinstance(raw, list):
        raw = []
    steps = _trainer_order_steps_for_run(raw)
    print(
        f"  ▶ Run workflow  name={name!r}  file={path.resolve()}  "
        f"steps={len(steps)}  source={run_source!r}  dry_run={dry_run}  "
        "(order: JSON 'step' field ascending)"
    )
    type_project_text = _workflow_type_project_typed_text(wf, name)
    global _TRAINER_LAST_WHATSAPP_WEB_SEND_URL
    _inner_notify_seed = bool(str((runtime_vars_seed or {}).get("WHATSAPP_NOTIFY_URL") or "").strip())
    if not _inner_notify_seed:
        _TRAINER_LAST_WHATSAPP_WEB_SEND_URL = None
    results = []
    runtime_vars: dict[str, str] = {
        str(k): str(v) for k, v in (runtime_vars_seed or {}).items() if str(k).strip()
    }
    _trainer_scrub_seeded_completion_scratch(runtime_vars)
    runtime_vars.setdefault("CURRENT_WORKFLOW_KEY", str(name or "").strip())
    runtime_vars.setdefault("CURRENT_WORKFLOW_NAME", str(type_project_text or name or "").strip())
    runtime_vars.setdefault(
        "CURRENT_AR_FLOW_NAME",
        str(
            runtime_vars.get("CURRENT_AR_FLOW_NAME")
            or runtime_vars.get("CURRENT_AR_BUNDLE_NAME")
            or runtime_vars.get("CURRENT_AR_BUNDLE_SLUG")
            or type_project_text
            or name
            or "default_flow"
        ).strip(),
    )
    try:
        apply_calendar_runtime_tokens(
            runtime_vars,
            downloads_base=_downloads_dir(),
            workflow_stem=str(name or "").strip(),
            workflow_label=str(type_project_text or name or "").strip(),
        )
    except Exception as cal_err:
        print(f"  ⚠ Could not resolve 30-day calendar paths: {cal_err}")
    runtime_vars.setdefault("RUN_SOURCE", (run_source or "manual_run"))
    _ar_seed_phone = _normalize_whatsapp_phone(str(runtime_vars.get("WHATSAPP_NOTIFY_PHONE") or ""))
    whatsapp_number_digits = (
        _ar_seed_phone if _ar_seed_phone else _workflow_trainer_whatsapp_number_digits(wf)
    )
    mode = str(run_mode or "smart").strip().lower()
    allow_ai = mode != "fast"
    _guard_on = str(os.environ.get("TRAINER_GUARDED_RUN") or "").strip().lower() in ("1", "true", "yes")
    try:
        _guard_delay_s = float(os.environ.get("TRAINER_GUARD_STEP_DELAY_SECONDS") or "10")
    except (TypeError, ValueError):
        _guard_delay_s = 10.0
    _guard_delay_s = max(0.0, min(_guard_delay_s, 60.0))
    try:
        _guard_max_dist_px = float(os.environ.get("TRAINER_GUARD_MAX_DISTANCE_PX") or "160")
    except (TypeError, ValueError):
        _guard_max_dist_px = 160.0
    _guard_max_dist_px = max(10.0, min(_guard_max_dist_px, 1200.0))
    for step in steps:
        if _stop_requested():
            print("  ⚠ Stop requested — aborting workflow run.")
            break
        if _guard_on and not dry_run and _guard_delay_s > 0:
            # Guarded runs add a human-like pause and give the page time to settle
            # before validating/executing each step.
            print(f"  ⏱ Guard pause: waiting {_guard_delay_s:g}s before Step {step.get('step')}…")
            slept = 0.0
            while slept < _guard_delay_s:
                if _stop_requested():
                    print("  ⚠ Stop requested during guard pause — aborting workflow run.")
                    break
                chunk = min(0.5, _guard_delay_s - slept)
                time.sleep(chunk)
                slept += chunk
            if _stop_requested():
                break
        started_at_z = _utc_now_z()
        action = step.get("action_type") or step.get("action", "click")
        x      = step.get("x") or step.get("trained_x", 0)
        y      = step.get("y") or step.get("trained_y", 0)
        desc   = step.get("description", f"Step {step.get('step','')}")
        skip_rng, skip_why = _trainer_step_skipped_automation_run_range(step, runtime_vars)
        if skip_rng:
            tag = "[DRY RUN] " if dry_run else ""
            print(
                f"  {tag}Step {step.get('step')}: {action} skipped — automation run out of range ({skip_why}) — {desc}"
            )
            results.append(
                _make_step_result(
                    step_num=step.get("step"),
                    action=action,
                    status="dry_run" if dry_run else "skipped",
                    started_at_z=started_at_z,
                    detail=skip_why,
                )
            )
            if dry_run:
                time.sleep(0.1)
            continue
        if dry_run:
            if action == "wait":
                try:
                    wsv = float(step.get("wait_seconds", 2))
                except (TypeError, ValueError):
                    wsv = 2.0
                wsv = max(0.0, min(wsv, 120.0))
                print(f"  [DRY RUN] Step {step.get('step')}: wait {wsv:g}s — {desc}")
            elif action in _TRAINER_TAB_COUNT_ACTIONS:
                tab_n = _trainer_repeat_press_count(step, runtime_vars)
                scaled = bool(step.get("repeat_scale_campaign_day"))
                sfx = ""
                if scaled:
                    try:
                        b0 = int(step.get("tab_count", 1) or 1)
                    except (TypeError, ValueError):
                        b0 = 1
                    try:
                        i0 = int(step.get("tab_press_increment", 1) or 1)
                    except (TypeError, ValueError):
                        i0 = 1
                    if str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip().isdigit():
                        sfx = (
                            f" (automation run #{runtime_vars.get('CURRENT_AUTOMATION_RUN')} → {tab_n} "
                            f"from base {max(1, min(200, b0))} +{max(1, min(200, i0))}/run)"
                        )
                    elif str(runtime_vars.get("CURRENT_CAMPAIGN_DAY") or "").strip().isdigit():
                        sfx = (
                            f" (campaign day index → {tab_n} "
                            f"from base {max(1, min(200, b0))} +{max(1, min(200, i0))}/run)"
                        )
                if action == "press_tab":
                    print(f"  [DRY RUN] Step {step.get('step')}: press_tab x{tab_n}{sfx}")
                else:
                    print(
                        f"  [DRY RUN] Step {step.get('step')}: "
                        f"{_TRAINER_ARROW_LABELS.get(action, action)} x{tab_n}{sfx}"
                    )
            elif action == "press_automation_grid_nav":
                run_i = _trainer_automation_run_index_for_grid_nav(runtime_vars)
                gc, gr = _trainer_parse_grid_nav_cols_rows(step)
                plan = _trainer_grid_snake_nav_press_plan(run_i, gc, gr)
                parts = [f"{d}×{n}" for d, n in plan if n > 0]
                summary = ", ".join(parts) if parts else "(no arrow presses)"
                print(
                    f"  [DRY RUN] Step {step.get('step')}: press_automation_grid_nav "
                    f"run#{run_i} {gc}×{gr} grid → {summary} — {desc}"
                )
            elif action == "click" and _trainer_use_live_vision_click(step, int(x or 0), int(y or 0), allow_ai=allow_ai):
                print(f"  [DRY RUN] Step {step.get('step')}: click — LIVE VISION (fresh screen + API) — {desc}")
            elif action == "ai_type" and not allow_ai:
                print(f"  [DRY RUN] Step {step.get('step')}: ai_type skipped in FAST mode")
            elif action == "ai_image" and not allow_ai:
                print(f"  [DRY RUN] Step {step.get('step')}: ai_image skipped in FAST mode")
            elif action == "type_project_name":
                wn = type_project_text or "(empty workflow name)"
                print(
                    f"  [DRY RUN] Step {step.get('step')}: type_project_name — {wn} — {desc}"
                )
            elif action == "type_whatsapp_number":
                wn = whatsapp_number_digits or "(empty — save WhatsApp number for this workflow in the Trainer)"
                print(f"  [DRY RUN] Step {step.get('step')}: type_whatsapp_number — {wn} — {desc}")
            elif action == "type_image_text_caption":
                tc = _resolve_runtime_tokens("{{CURRENT_CAPTION}}", name, runtime_vars).strip()
                pv = tc.replace("\n", " ").strip()
                if len(pv) > 200:
                    pv = pv[:197] + "..."
                print(
                    f"  [DRY RUN] Step {step.get('step')}: type_image_text_caption — "
                    f"{pv or '(empty caption)'}"
                )
            elif action == "type_video_text_caption":
                tc = _resolve_runtime_tokens("{{CURRENT_CAPTION}}", name, runtime_vars).strip()
                pv = tc.replace("\n", " ").strip()
                if len(pv) > 200:
                    pv = pv[:197] + "..."
                print(
                    f"  [DRY RUN] Step {step.get('step')}: type_video_text_caption — "
                    f"{pv or '(empty caption)'}"
                )
            elif action == "shell":
                sc = (step.get("shell_command") or "").strip() or desc
                print(f"  [DRY RUN] Step {step.get('step')}: shell — {sc[:160]}{'…' if len(sc) > 160 else ''}")
            elif action == "open_tab":
                tab_url = _resolve_runtime_tokens(str(step.get("url") or ""), name, runtime_vars).strip()
                if tab_url:
                    print(f"  [DRY RUN] Step {step.get('step')}: open_tab — {tab_url}")
                else:
                    print(f"  [DRY RUN] Step {step.get('step')}: open_tab")
            elif action == "open_url":
                ou = _resolve_runtime_tokens(str(step.get("url") or ""), name, runtime_vars).strip()
                reuse = _open_url_reuse_chrome_from_step(step)
                tag = "reuse Chrome window" if reuse else "new Chrome window (macOS)"
                print(f"  [DRY RUN] Step {step.get('step')}: open_url — {tag} — {ou or desc}")
            elif action == "open_whatsapp":
                reuse = _open_url_reuse_chrome_from_step(step)
                tag = "reuse Chrome window" if reuse else "new Chrome window (macOS)"
                print(f"  [DRY RUN] Step {step.get('step')}: open_whatsapp — {tag} — {_OPEN_WHATSAPP_WEB_URL}")
            elif action == "close_chrome":
                print(f"  [DRY RUN] Step {step.get('step')}: close_chrome — {desc}")
            elif action == "completion_link":
                msg, url = _trainer_whatsapp_completion_store_runtime(
                    name, results=results, mode=mode, runtime_vars=runtime_vars, dry_run=True
                )
                try:
                    _copy_text_to_clipboard(url)
                    runtime_vars["WHATSAPP_COMPLETION_URL_COPIED"] = "1"
                except Exception as clip_err:
                    runtime_vars["WHATSAPP_COMPLETION_URL_COPIED"] = "0"
                    print(f"      ⚠ completion_link clipboard copy failed: {clip_err}")
                print(f"  [DRY RUN] Step {step.get('step')}: completion_link — {url}")
            elif action == "completion_message":
                msg, url = _trainer_whatsapp_completion_store_runtime(
                    name, results=results, mode=mode, runtime_vars=runtime_vars, dry_run=True
                )
                runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "0"
                clip_on = _env_truthy("TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP", "0")
                if clip_on:
                    try:
                        _copy_text_to_clipboard(msg)
                        runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "1"
                    except Exception as clip_err:
                        print(f"      ⚠ completion_message clipboard copy failed: {clip_err}")
                pv = msg.replace("\n", " ").strip()
                if len(pv) > 200:
                    pv = pv[:197] + "..."
                tag = "body to clipboard + memory" if clip_on else "memory only (no clipboard — set TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP=1 to copy)"
                print(
                    f"  [DRY RUN] Step {step.get('step')}: completion_message — "
                    f"{len(msg)} chars ({tag}; URL not opened) — {pv}"
                )
            elif action == "completion_clipboard_refresh":
                mem = (runtime_vars.get("WHATSAPP_COMPLETION_TEXT") or "").strip()
                pv = mem.replace("\n", " ").strip() if mem else "(empty — run completion_message first)"
                if len(pv) > 200:
                    pv = pv[:197] + "..."
                print(
                    f"  [DRY RUN] Step {step.get('step')}: completion_clipboard_refresh — "
                    f"re-copy {len(mem)} chars to clipboard — {pv}"
                )
            elif action == "type_completion_message":
                msg_tc, _url_tc = _trainer_whatsapp_completion_store_runtime(
                    name, results=results, mode=mode, runtime_vars=runtime_vars, dry_run=True
                )
                pv = (msg_tc or "").replace("\n", " ").strip()
                if len(pv) > 200:
                    pv = pv[:197] + "..."
                print(
                    f"  [DRY RUN] Step {step.get('step')}: type_completion_message — "
                    f"would build from run now + pbcopy+paste only at live run — {pv or '(empty)'} — {desc}"
                )
            elif action == "ai_type":
                ap = _resolve_runtime_tokens(str(step.get("ai_prompt") or ""), name, runtime_vars)
                pv = ap.replace("\n", " ").strip()
                if len(pv) > 160:
                    pv = pv[:157] + "..."
                print(f"  [DRY RUN] Step {step.get('step')}: ai_type — {pv or '(empty prompt)'}")
            elif action == "ai_image":
                ap = _resolve_runtime_tokens(str(step.get("ai_prompt") or ""), name, runtime_vars)
                pv = ap.replace("\n", " ").strip()
                if len(pv) > 160:
                    pv = pv[:157] + "..."
                print(
                    f"  [DRY RUN] Step {step.get('step')}: ai_image — "
                    f"{pv or '(empty prompt)'} (infographic 4:5, saved as 1080x1350 in Downloads)"
                )
            elif action == "hotkey":
                hk = _trainer_hotkey_keys_for_run(step)
                combo = "+".join(hk) if hk else "(no keys)"
                print(f"  [DRY RUN] Step {step.get('step')}: hotkey — {combo}")
            elif action == "best_ai_copy_query_bundle":
                print(
                    f"  [DRY RUN] Step {step.get('step')}: best_ai_copy_query_bundle — "
                    "clipboard ← CURRENT_TOPIC or bridge topic + platform instructions"
                )
            elif action == "best_ai_capture_slot_from_clipboard":
                _bs = str(step.get("best_ai_slot") or "?").strip().lower()
                print(
                    f"  [DRY RUN] Step {step.get('step')}: best_ai_capture_slot_from_clipboard — "
                    f"clipboard → bridge slot {_bs}"
                )
            elif action == "best_ai_run_synthesizer":
                print(
                    f"  [DRY RUN] Step {step.get('step')}: best_ai_run_synthesizer — "
                    "OpenAI on bridge slots → synthesis in ui_bridge.json"
                )
            elif action == "upload":
                cal_l = str(step.get("calendar_upload_layer") or "").strip().lower()
                cal_p = str(step.get("calendar_asset_pick") or "auto").strip().lower()
                day = str(runtime_vars.get("CURRENT_CALENDAR_DAY") or "").strip()
                if cal_l in ("core", "hybrid", "ai"):
                    mp, mk, cap_dr, _cp = select_calendar_asset_for_upload(
                        runtime_vars,
                        layer=cal_l,
                        pick=cal_p,
                    )
                    clip = mp or ((cap_dr or "").strip())
                    prev = (clip.replace("\n", " ").strip()[:120] + "…") if len(clip) > 120 else clip.replace("\n", " ").strip()
                    print(
                        f"  [DRY RUN] Step {step.get('step')}: upload — calendar {cal_l}/{cal_p} "
                        f"day={day} → {mk or '?'} path/preview: {prev or '(nothing resolved)'}"
                    )
                else:
                    print(
                        f"  [DRY RUN] Step {step.get('step')}: upload — AI-media queue bind — day={day} — {desc}"
                    )
            elif action == "press_space":
                print(f"  [DRY RUN] Step {step.get('step')}: press_space — {desc}")
            else:
                print(f"  [DRY RUN] Step {step.get('step')}: {action} — {desc}")
            results.append(
                _make_step_result(
                    step_num=step.get("step"),
                    action=action,
                    status="dry_run",
                    started_at_z=started_at_z,
                )
            )
            time.sleep(0.1)
            continue
        try:
            if action == "shell":
                time.sleep(0.6)
                _trainer_run_shell_step(step, stop_event=_RUN_STOP_EVENT)
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            if action == "best_ai_copy_query_bundle":
                st_b = _best_ai_bridge_get()
                base = (runtime_vars.get("CURRENT_TOPIC") or "").strip() or str(st_b.get("topic") or "").strip()
                if not base:
                    raise RuntimeError(
                        "best_ai_copy_query_bundle: no topic — save query in Best AI tab (syncs to server), "
                        "or set CURRENT_TOPIC for this run."
                    )
                bundle = base + _best_ai_platform_wrap_instructions_text()
                _copy_text_to_clipboard(bundle)
                print(
                    f"  ✓ Step {step.get('step')}: Best AI — copied query bundle to clipboard "
                    f"({len(bundle)} chars)"
                )
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            if action == "best_ai_capture_slot_from_clipboard":
                slot = str(step.get("best_ai_slot") or "").strip().lower()
                if slot not in ("chatgpt", "gemini", "claude"):
                    raise RuntimeError(
                        "best_ai_capture_slot_from_clipboard: invalid best_ai_slot "
                        "(must be chatgpt, gemini, or claude)"
                    )
                text = _read_clipboard_text().strip()
                if not text:
                    raise RuntimeError(
                        "best_ai_capture_slot_from_clipboard: clipboard is empty "
                        "(select model reply and Copy before this step)"
                    )

                def _cap_mut(d: dict[str, Any]) -> None:
                    d.setdefault("slots", {"chatgpt": "", "gemini": "", "claude": ""})
                    d["slots"][slot] = text

                _best_ai_bridge_mutate(_cap_mut)
                print(
                    f"  ✓ Step {step.get('step')}: Best AI — captured clipboard → bridge slot {slot} "
                    f"({len(text)} chars; Trainer polls ui_bridge.json)"
                )
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            if action == "best_ai_run_synthesizer":
                st_b = _best_ai_bridge_get()
                query = (runtime_vars.get("CURRENT_TOPIC") or "").strip() or str(st_b.get("topic") or "").strip()
                if not query:
                    raise RuntimeError(
                        "best_ai_run_synthesizer: missing query — use CURRENT_TOPIC or save topic in Best AI tab"
                    )
                slots_d = st_b.get("slots") or {}
                responses: dict[str, str] = {}
                if isinstance(slots_d, dict):
                    for k in ("chatgpt", "gemini", "claude"):
                        v = slots_d.get(k)
                        if isinstance(v, str) and v.strip():
                            responses[k] = v.strip()
                if not responses:
                    raise RuntimeError(
                        "best_ai_run_synthesizer: no slot text in bridge — run capture steps after each model"
                    )
                out = _best_ai_synthesize_openai(query, responses)
                syn_text = str(out.get("synthesis") or "").strip()

                def _syn_mut(d: dict[str, Any]) -> None:
                    d.setdefault("synthesis", {"text": "", "model": "", "platforms_used": [], "at": ""})
                    d["synthesis"]["text"] = syn_text
                    d["synthesis"]["model"] = str(out.get("model") or "")
                    pu = out.get("platforms_used")
                    d["synthesis"]["platforms_used"] = pu if isinstance(pu, list) else []
                    d["synthesis"]["at"] = _utc_now_z()

                _best_ai_bridge_mutate(_syn_mut)
                print(
                    f"  ✓ Step {step.get('step')}: Best AI synthesizer — model {out.get('model')} · "
                    f"{len(syn_text)} chars written to bridge"
                )
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            if action == "upload":
                # Upload marker: bind the next AI media pair so text/media don't cross-post.
                cal_layer_u = str(step.get("calendar_upload_layer") or "").strip().lower()
                cal_pick_u = str(step.get("calendar_asset_pick") or "auto").strip().lower()
                if cal_pick_u not in ("auto", "image", "video", "text"):
                    cal_pick_u = "auto"
                use_calendar_bind = cal_layer_u in ("core", "hybrid", "ai")

                bound_note = ""
                wf_key = str(name or "").strip()
                use_latest_downloads = False
                try:
                    # Presence consumer requirement: Instagram must post whatever latest image is in Downloads.
                    use_latest_downloads = (
                        wf_key == "Instagram_Post"
                        and bool(_cusear_default_ar_slug())
                        and _is_consumer_mode()
                        and bool(getattr(sys, "frozen", False))
                    )
                    # Production consumer build: always generate caption from user's per-workflow prompt.
                    consumer_prompt = ""
                    if (
                        bool(_cusear_default_ar_slug())
                        and _is_consumer_mode()
                        and bool(getattr(sys, "frozen", False))
                        and not use_calendar_bind
                    ):
                        consumer_prompt = _workflow_prompt_seed(wf_key)
                        if consumer_prompt:
                            try:
                                caption = _generate_ai_caption_from_user_prompt(
                                    prompt=consumer_prompt,
                                    workflow_name=wf_key,
                                    platform_hint=("instagram" if wf_key == "Instagram_Post" else "social"),
                                )
                                runtime_vars["CURRENT_CAPTION"] = caption
                                runtime_vars["CURRENT_CAPTION_PATH"] = _save_caption_text_to_downloads_simple(
                                    workflow_name=wf_key,
                                    caption=caption,
                                )
                            except Exception as cap_err:
                                raise RuntimeError(f"AI caption failed for {wf_key}: {cap_err}") from cap_err

                    if use_latest_downloads:
                        img = _latest_download_file_by_exts(["png", "jpg", "jpeg", "webp"])
                        if not img:
                            raise RuntimeError("No image found in Downloads (add a .png/.jpg/.jpeg/.webp file first)")
                        image_path = str(img.resolve())
                        video_path = ""
                        topic = runtime_vars.get("CURRENT_TOPIC", "").strip() or "Downloads image"
                        caption = runtime_vars.get("CURRENT_CAPTION", "").strip()
                        image_caption_path = runtime_vars.get("CURRENT_CAPTION_PATH", "").strip()
                        video_caption_path = ""
                        pref_kind = "image"
                        bound_note = f"Downloads→bound latest image: {image_path}"
                    elif use_calendar_bind:
                        media_path_c, media_kind_c, caption_c, caption_path_c = select_calendar_asset_for_upload(
                            runtime_vars,
                            layer=cal_layer_u,
                            pick=cal_pick_u,
                        )
                        stem_key_map = {
                            "core": "CALENDAR_CORE_STEM",
                            "hybrid": "CALENDAR_HYBRID_STEM",
                            "ai": "CALENDAR_AI_STEM",
                        }
                        stem_v = str(runtime_vars.get(stem_key_map.get(cal_layer_u, "")) or "").strip()

                        if cal_pick_u == "image" and media_kind_c != "image":
                            raise RuntimeError(
                                f"Calendar upload: expected image for {stem_v} ({cal_layer_u}); "
                                "add a .png/.jpg under Core|Hybrid|AI in your cusear Downloads folder."
                            )
                        if cal_pick_u == "video" and media_kind_c != "video":
                            raise RuntimeError(
                                f"Calendar upload: expected video for {stem_v} ({cal_layer_u}); "
                                "add a .mp4 (or .mov) under the mode folder."
                            )
                        if cal_pick_u == "text" and not (caption_c or "").strip():
                            raise RuntimeError(
                                f"Calendar upload: expected caption .txt for {stem_v} ({cal_layer_u}); "
                                "create the matching .txt stub with your copy."
                            )

                        if not media_path_c and media_kind_c != "text":
                            raise RuntimeError(
                                f"Calendar upload: no media for {cal_layer_u} "
                                f"day {runtime_vars.get('CURRENT_CALENDAR_DAY', '?')} stem {stem_v!r} "
                                f"(pick={cal_pick_u}). Fill Core/Hybrid/AI under Downloads/cusear/…"
                            )
                        if not media_path_c and media_kind_c == "text" and not (caption_c or "").strip():
                            raise RuntimeError(
                                f"Calendar upload: missing .txt for stem {stem_v!r} ({cal_layer_u})."
                            )

                        topic = (runtime_vars.get("CURRENT_TOPIC") or "").strip() or f"cusear {stem_v}"
                        caption = (caption_c or "").strip()

                        if media_kind_c == "text":
                            media_kind = "text"
                            media_path = ""
                            image_path = ""
                            video_path = ""
                            caption_path = caption_path_c or ""
                        elif media_kind_c == "video":
                            video_path = media_path_c
                            image_path = ""
                            media_path = video_path
                            media_kind = "video"
                            caption_path = caption_path_c or ""
                        else:
                            image_path = media_path_c
                            video_path = ""
                            media_path = image_path
                            media_kind = "image"
                            caption_path = caption_path_c or ""

                        runtime_vars["CURRENT_TOPIC"] = topic
                        runtime_vars["CURRENT_CAPTION"] = caption
                        runtime_vars["CURRENT_CAPTION_PATH"] = caption_path
                        runtime_vars["CURRENT_IMAGE_PATH"] = image_path
                        runtime_vars["CURRENT_VIDEO_PATH"] = video_path
                        runtime_vars["CURRENT_MEDIA_PATH"] = media_path
                        runtime_vars["CURRENT_MEDIA_KIND"] = media_kind
                        runtime_vars["LAST_AI_MEDIA_TOPIC"] = topic
                        runtime_vars["LAST_AI_MEDIA_CAPTION"] = caption

                        if media_path:
                            try:
                                _copy_text_to_clipboard(media_path)
                                runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "1"
                            except Exception:
                                runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "0"
                        elif caption.strip():
                            try:
                                _copy_text_to_clipboard(caption.strip())
                                runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "1"
                            except Exception:
                                runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "0"
                        else:
                            runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "0"

                        mode_dir = {"core": "Core", "hybrid": "Hybrid", "ai": "AI"}.get(cal_layer_u, cal_layer_u)
                        bound_note = (
                            f"30-day calendar [{mode_dir}] pick={cal_pick_u} day="
                            f"{runtime_vars.get('CURRENT_CALENDAR_DAY', '?')} stem={stem_v} "
                            f"media={media_path or '(caption only)'} caption_file={caption_path or '(n/a)'}"
                        )
                        print(
                            f"  ◆ Step {step.get('step')}: Upload — {desc} "
                            f"(marker; {bound_note}. Use {{CURRENT_CAPTION}} / {{CURRENT_MEDIA_PATH}}.)"
                        )
                        results.append(
                            _make_step_result(
                                step_num=step.get("step"),
                                action=action,
                                status="ok",
                                started_at_z=started_at_z,
                            )
                        )
                        continue
                    else:
                        pref_kind = _preferred_media_kind_for_upload(desc, runtime_vars)
                        item = _ai_media_select_next_item(runtime_vars, preferred_kind=pref_kind)
                        item = _ai_media_export_item_to_cusear(
                            item=item,
                            runtime_vars=runtime_vars,
                            workflow_name=wf_key,
                            workflow_label=str(type_project_text or name or "").strip(),
                        )
                        topic = str(item.get("topic") or "").strip()
                        caption = str(runtime_vars.get("CURRENT_CAPTION") or "").strip() or str(item.get("caption") or "").strip()
                        image_path = str(item.get("image_download_path") or "").strip()
                        video_path = str(item.get("video_download_path") or "").strip()
                        image_caption_path = str(
                            runtime_vars.get("CURRENT_CAPTION_PATH")
                            or item.get("caption_image_download_path")
                            or item.get("caption_download_path")
                            or ""
                        ).strip()
                        video_caption_path = str(
                            item.get("caption_video_download_path")
                            or item.get("caption_download_path")
                            or ""
                        ).strip()
                    if pref_kind == "video":
                        media_path = video_path or image_path
                        media_kind = "video" if video_path else ("image" if image_path else "")
                    elif pref_kind == "image":
                        media_path = image_path or video_path
                        media_kind = "image" if image_path else ("video" if video_path else "")
                    else:
                        media_path = image_path or video_path
                        media_kind = "image" if image_path else ("video" if video_path else "")
                    caption_path = (
                        video_caption_path
                        if media_kind == "video"
                        else image_caption_path
                        if media_kind == "image"
                        else (image_caption_path or video_caption_path)
                    )
                    runtime_vars["CURRENT_TOPIC"] = topic
                    runtime_vars.setdefault("CURRENT_CAPTION", caption)
                    runtime_vars.setdefault("CURRENT_CAPTION_PATH", caption_path)
                    runtime_vars["CURRENT_IMAGE_PATH"] = image_path
                    runtime_vars["CURRENT_VIDEO_PATH"] = video_path
                    runtime_vars["CURRENT_MEDIA_PATH"] = media_path
                    runtime_vars["CURRENT_MEDIA_KIND"] = media_kind
                    runtime_vars["LAST_AI_MEDIA_TOPIC"] = topic
                    runtime_vars["LAST_AI_MEDIA_CAPTION"] = runtime_vars.get("CURRENT_CAPTION") or caption
                    if media_path:
                        try:
                            _copy_text_to_clipboard(media_path)
                            runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "1"
                        except Exception:
                            runtime_vars["CURRENT_MEDIA_PATH_COPIED"] = "0"
                    if not bound_note:
                        bound_note = (
                            f"bound topic='{topic[:80]}' kind={media_kind or 'unknown'} "
                            f"path={media_path or '(missing)'} caption_file={caption_path or '(missing)'} "
                            f"folder={str(item.get('cusear_media_root') or '(n/a)')}"
                        )
                except Exception as bind_err:
                    if use_latest_downloads:
                        # Instagram must not proceed without a real Downloads image in locked consumer builds.
                        raise
                    bound_note = f"no AI media binding ({bind_err})"
                print(
                    f"  ◆ Step {step.get('step')}: Upload — {desc} "
                    f"(marker step; {bound_note}. Use {{CURRENT_CAPTION}} (or {{CURRENT_CAPTION_PATH}}) "
                    "and upload {{CURRENT_MEDIA_PATH}} manually)"
                )
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            if action == "ai_image":
                if not allow_ai:
                    raise RuntimeError("ai_image requires Smart mode; Fast mode disables AI image generation")
                ai_prompt_raw = step.get("ai_prompt")
                ai_prompt = _resolve_runtime_tokens(str(ai_prompt_raw or ""), name, runtime_vars).strip()
                if not ai_prompt:
                    raise ValueError("ai_image step has empty ai_prompt")
                cur_topic = (runtime_vars.get("CURRENT_TOPIC") or "").strip()
                raw_prompt_text = str(ai_prompt_raw or "")
                if cur_topic and "{{CURRENT_TOPIC}}" not in raw_prompt_text and "{{TOPIC_SLOT}}" not in raw_prompt_text:
                    ai_prompt = f"{ai_prompt}\n\nTopic slot: {cur_topic}"
                asset = _generate_ai_image_asset_from_prompt(
                    prompt=ai_prompt,
                    topic=cur_topic,
                    industry=str(runtime_vars.get("INDUSTRY") or runtime_vars.get("INDUSTRY_NAME") or ""),
                )
                rel_path = str(asset.get("relative_path") or "").strip()
                abs_path = str((BASE_DIR / rel_path).resolve()) if rel_path else ""
                runtime_vars["LAST_AI_IMAGE_ASSET_ID"] = str(asset.get("id") or "")
                runtime_vars["LAST_AI_IMAGE_RELATIVE_PATH"] = rel_path
                runtime_vars["LAST_AI_IMAGE_PATH"] = abs_path
                runtime_vars["LAST_AI_IMAGE_MIME_TYPE"] = str(asset.get("mime_type") or "image/png")
                if abs_path:
                    dl_path = ""
                    try:
                        dl_file = _save_ai_image_copy_to_downloads(
                            Path(abs_path),
                            topic=cur_topic,
                        )
                        dl_path = str(dl_file.resolve())
                        runtime_vars["LAST_AI_IMAGE_DOWNLOAD_PATH"] = dl_path
                        runtime_vars["LAST_AI_IMAGE_PATH"] = dl_path
                    except Exception as dl_err:
                        print(f"      ⚠ Could not copy AI image to Downloads: {dl_err}")
                    try:
                        _copy_text_to_clipboard(dl_path or abs_path)
                        runtime_vars["LAST_AI_IMAGE_PATH_COPIED"] = "1"
                    except Exception:
                        runtime_vars["LAST_AI_IMAGE_PATH_COPIED"] = "0"
                print(
                    f"  ✓ Step {step.get('step')}: AI image generated (infographic 1080x1350) "
                    f"and saved to Downloads/media library — "
                    f"{runtime_vars.get('LAST_AI_IMAGE_PATH') or rel_path or '(path unavailable)'}"
                )
                results.append(
                    _make_step_result(
                        step_num=step.get("step"),
                        action=action,
                        status="ok",
                        started_at_z=started_at_z,
                    )
                )
                continue

            try:
                import pyautogui
            except ImportError as ie:
                cmd = _trainer_pip_install_requirements_hint()
                raise RuntimeError(
                    "pyautogui is not installed for this Python. "
                    "Use:  " + cmd + "  (note: `pip install -r`, not `pip -r` alone). "
                    "Or run: bash START.sh"
                ) from ie
            pyautogui.FAILSAFE = True
            if _stop_requested():
                raise RuntimeError("Run stopped by user")
            time.sleep(0.6)
            # Defensive: before most actions, clear any stuck nav/modifier key state
            # left behind by prior automation.
            if action not in _TRAINER_TAB_COUNT_ACTIONS:
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
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
                _trainer_activate_chrome_before_whatsapp_keys("after open_chrome")
            elif action == "close_chrome":
                _trainer_clear_whatsapp_web_send_memory()
                _trainer_close_google_chrome()
                print(f"  ✓ Step {step.get('step')}: Closed Google Chrome")
            elif action == "open_cursor":
                import platform
                sys_name = platform.system()
                opened = False
                if sys_name == "Darwin":
                    try:
                        subprocess.Popen(["open", "-a", "Cursor"])
                        opened = True
                    except Exception:
                        opened = False
                elif sys_name == "Windows":
                    for cmd in (
                        ["cmd", "/c", "start", "", "Cursor"],
                        ["cmd", "/c", "start", "", "cursor"],
                    ):
                        try:
                            subprocess.Popen(cmd)
                            opened = True
                            break
                        except Exception:
                            continue
                else:  # Linux
                    try:
                        subprocess.Popen(["cursor"])
                        opened = True
                    except Exception:
                        opened = False
                if not opened:
                    raise RuntimeError(
                        "Could not open Cursor. Ensure Cursor is installed (and cursor CLI is available on Linux/Windows)."
                    )
                time.sleep(1.2)
                print(f"  ✓ Step {step.get('step')}: Opened Cursor")
            elif action == "press_enter":
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                key_mode = (os.environ.get("TRAINER_PRESS_ENTER_MODE") or "enter").strip().lower()
                if key_mode in ("cmd_enter", "cmd_return", "meta_enter") and platform.system() == "Darwin":
                    pyautogui.hotkey("command", "return")
                    key_label = "Cmd+Return"
                elif key_mode == "return":
                    pyautogui.press("return")
                    key_label = "Return"
                else:
                    pyautogui.press("enter")
                    key_label = "Enter"
                print(f"  ✓ Step {step.get('step')}: Pressed {key_label} — {desc}")
            elif action == "press_home":
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                pyautogui.press("home")
                print(f"  ✓ Step {step.get('step')}: Pressed Home — {desc}")
            elif action == "press_space":
                _trainer_whatsapp_web_nav_maybe_activate_chrome(runtime_vars, "press_space")
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                pyautogui.press("space")
                print(f"  ✓ Step {step.get('step')}: Pressed Space — {desc}")
            elif action == "press_tab":
                _trainer_whatsapp_web_nav_maybe_activate_chrome(runtime_vars, "press_tab")
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                tab_n = _trainer_repeat_press_count(step, runtime_vars)
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                if bool(step.get("direct_jump")):
                    dx = int(step.get("trained_x") or step.get("x") or 0)
                    dy = int(step.get("trained_y") or step.get("y") or 0)
                    if dx > 0 and dy > 0:
                        pyautogui.click(dx, dy)
                        print(f"  ✓ Step {step.get('step')}: Direct jump click ({dx},{dy})")
                    else:
                        raise RuntimeError(
                            "direct_jump is enabled but trained_x/trained_y are missing — edit and re-save this step"
                        )
                else:
                    old_pause = getattr(pyautogui, "PAUSE", 0.1)
                    try:
                        pyautogui.PAUSE = 0
                        pyautogui.press("tab", presses=max(1, tab_n), interval=0)
                    finally:
                        pyautogui.PAUSE = old_pause
                _release_nav_keys(pyautogui)
                print(f"  ✓ Step {step.get('step')}: Pressed Tab x{tab_n}")
            elif action in _TRAINER_ARROW_PY_KEYS:
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                tab_n = _trainer_repeat_press_count(step, runtime_vars)
                pg_key = _TRAINER_ARROW_PY_KEYS[action]
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                old_pause = getattr(pyautogui, "PAUSE", 0.1)
                try:
                    pyautogui.PAUSE = 0
                    pyautogui.press(pg_key, presses=max(1, tab_n), interval=0)
                finally:
                    pyautogui.PAUSE = old_pause
                _release_nav_keys(pyautogui)
                alab = _TRAINER_ARROW_LABELS[action]
                print(f"  ✓ Step {step.get('step')}: Pressed {alab} x{tab_n}")
            elif action == "press_automation_grid_nav":
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                run_i = _trainer_automation_run_index_for_grid_nav(runtime_vars)
                gc, gr = _trainer_parse_grid_nav_cols_rows(step)
                plan = _trainer_grid_snake_nav_press_plan(run_i, gc, gr)
                interval = float((os.environ.get("TRAINER_TAB_INTERVAL") or "0.09").strip() or "0.09")
                _release_modifier_keys(pyautogui)
                _release_nav_keys(pyautogui)
                fired: list[str] = []
                for d, n in plan:
                    if _stop_requested():
                        raise RuntimeError("Run stopped by user")
                    if n <= 0:
                        continue
                    pgk = _GRID_NAV_DIR_TO_PG.get(d)
                    if not pgk:
                        continue
                    for _ in range(n):
                        pyautogui.press(pgk)
                        time.sleep(max(0.01, interval))
                    fired.append(f"{d}×{n}")
                _release_nav_keys(pyautogui)
                summary = ", ".join(fired) if fired else "(none)"
                print(
                    f"  ✓ Step {step.get('step')}: Grid snake nav run#{run_i} ({gc}×{gr}) → {summary} — {desc}"
                )
            elif action == "wait":
                try:
                    ws = float(step.get("wait_seconds", 2))
                except (TypeError, ValueError):
                    ws = 2.0
                ws = max(0.0, min(ws, 120.0))
                print(f"  ✓ Step {step.get('step')}: Wait {ws:g}s — {desc}")
                remaining = ws
                while remaining > 0:
                    if _stop_requested():
                        raise RuntimeError("Run stopped by user")
                    chunk = min(0.25, remaining)
                    time.sleep(chunk)
                    remaining -= chunk
            elif action == "copy":
                import platform as _plat

                _trainer_avoid_pyautogui_failsafe(pyautogui)
                if _plat.system() == "Darwin":
                    pyautogui.hotkey("command", "c")
                else:
                    pyautogui.hotkey("ctrl", "c")
                time.sleep(0.28)
                print(f"  ✓ Step {step.get('step')}: Copy (selection → clipboard) — {desc}")
            elif action == "paste":
                import platform as _plat

                _trainer_avoid_pyautogui_failsafe(pyautogui)
                if _plat.system() == "Darwin" and _env_truthy(
                    "TRAINER_WHATSAPP_FOCUS_BEFORE_PASTE", "0"
                ):
                    _fd_p = _darwin_chrome_focus_whatsapp_compose()
                    if _fd_p == "ok":
                        print("      (WhatsApp Web: focused compose before Paste — TRAINER_WHATSAPP_FOCUS_BEFORE_PASTE=1)")
                        try:
                            _wpp = float(
                                (os.environ.get("TRAINER_WHATSAPP_COMPOSE_FOCUS_WAIT") or "0.45").strip() or "0.45"
                            )
                        except (TypeError, ValueError):
                            _wpp = 0.45
                        time.sleep(max(0.0, min(3.0, _wpp)))
                    elif _fd_p != "skipped":
                        print(f"      ⚠ Paste: WhatsApp compose focus returned {_fd_p!r}")
                if _plat.system() == "Darwin":
                    pyautogui.hotkey("command", "v")
                else:
                    pyautogui.hotkey("ctrl", "v")
                time.sleep(0.35)
                print(f"  ✓ Step {step.get('step')}: Paste (clipboard → focus) — {desc}")
            elif action == "open_url":
                raw = _resolve_runtime_tokens(str(step.get("url") or ""), name, runtime_vars).strip()
                if not raw:
                    raise ValueError("open_url step missing url")
                url = raw
                if not re.match(r"^[a-zA-Z][-a-zA-Z0-9+.]*:", url):
                    url = "https://" + url.lstrip("/")
                reuse = _open_url_reuse_chrome_from_step(step)
                _open_url_prefer_chrome(url, new_chrome_window=not reuse)
                _trainer_note_whatsapp_web_send_open(url)
                time.sleep(1.0)
                wtag = " (reuse Chrome window)" if reuse else ""
                print(f"  ✓ Step {step.get('step')}: Open URL{wtag} — {url}")
            elif action == "open_whatsapp":
                url = _OPEN_WHATSAPP_WEB_URL
                reuse = _open_url_reuse_chrome_from_step(step)
                _open_url_prefer_chrome(url, new_chrome_window=not reuse)
                _trainer_note_whatsapp_web_send_open(url)
                time.sleep(1)
                wtag = " (reuse Chrome window)" if reuse else ""
                print(f"  ✓ Step {step.get('step')}: Open WhatsApp Web{wtag} — {url}")
            elif action == "completion_link":
                msg, url = _trainer_whatsapp_completion_store_runtime(
                    name, results=results, mode=mode, runtime_vars=runtime_vars, dry_run=False
                )
                try:
                    _copy_text_to_clipboard(url)
                    runtime_vars["WHATSAPP_COMPLETION_URL_COPIED"] = "1"
                    print("      (copied completion URL to clipboard)")
                except Exception as clip_err:
                    runtime_vars["WHATSAPP_COMPLETION_URL_COPIED"] = "0"
                    print(f"      ⚠ completion_link clipboard copy failed: {clip_err}")
                print(f"  ✓ Step {step.get('step')}: Generated completion link")
                print(f"      {url}")
            elif action == "completion_message":
                msg, url = _trainer_whatsapp_completion_store_runtime(
                    name, results=results, mode=mode, runtime_vars=runtime_vars, dry_run=False
                )
                runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "0"
                if _env_truthy("TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP", "0"):
                    try:
                        _copy_text_to_clipboard(msg)
                        runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "1"
                        print(
                            "      (copied completion message body to clipboard — "
                            "TRAINER_COMPLETION_MESSAGE_CLIPBOARD_ON_STEP=1)"
                        )
                    except Exception as clip_err:
                        print(f"      ⚠ completion_message clipboard copy failed: {clip_err}")
                else:
                    print(
                        "      (completion_message: body in run memory + log only — clipboard unchanged; "
                        "type_completion_message builds+pbcopy+pastes at that step)"
                    )
                sep = "═" * 56
                print(f"  ✓ Step {step.get('step')}: Completion message (output only; no browser)\n{sep}\n{msg}\n{sep}")
                print(f"      Same text as completion_link body; URL not opened: {url[:96]}{'…' if len(url) > 96 else ''}")
            elif action == "completion_clipboard_refresh":
                mem = (runtime_vars.get("WHATSAPP_COMPLETION_TEXT") or "").strip()
                if not mem:
                    raise ValueError(
                        "completion_clipboard_refresh: no completion text in memory — "
                        "run completion_message or completion_link earlier in this workflow"
                    )
                try:
                    _copy_text_to_clipboard(mem)
                    runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "1"
                except Exception as clip_err:
                    runtime_vars["WHATSAPP_COMPLETION_MESSAGE_COPIED"] = "0"
                    raise RuntimeError(
                        f"completion_clipboard_refresh: clipboard copy failed: {clip_err}"
                    ) from clip_err
                print(
                    f"  ✓ Step {step.get('step')}: Re-copied completion body to clipboard "
                    f"({len(mem)} chars) — use before Paste / ⌘V if a Type step overwrote the clipboard"
                )
            elif action == "open_tab":
                import platform as _plat

                _activate_trainer_target_app_if_configured()
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                if _plat.system() == "Darwin":
                    pyautogui.hotkey("command", "t")
                else:
                    pyautogui.hotkey("ctrl", "t")
                time.sleep(0.25)
                raw = _resolve_runtime_tokens(str(step.get("url") or ""), name, runtime_vars).strip()
                if raw:
                    url = raw
                    if not re.match(r"^[a-zA-Z][-a-zA-Z0-9+.]*:", url):
                        url = "https://" + url.lstrip("/")
                    _type_via_clipboard_pyautogui(pyautogui, url, _plat.system() == "Darwin")
                    _release_modifier_keys(pyautogui)
                    _release_nav_keys(pyautogui)
                    _trainer_avoid_pyautogui_failsafe(pyautogui)
                    pyautogui.press("enter")
                    print(f"  ✓ Step {step.get('step')}: Opened new tab + URL — {url}")
                else:
                    print(f"  ✓ Step {step.get('step')}: Opened new browser tab")
            elif action in (
                "type",
                "ai_type",
                "type_project_name",
                "type_whatsapp_number",
                "type_completion_message",
                "type_image_text_caption",
                "type_video_text_caption",
            ):
                import platform as _plat

                text = ""
                focus_target = (step.get("focus_target") or "").strip()
                if action == "type_project_name":
                    text = _resolve_runtime_tokens(
                        type_project_text, name, runtime_vars
                    ).strip()
                    if not text:
                        raise ValueError("type_project_name: workflow name is empty")
                elif action == "type_whatsapp_number":
                    text = _resolve_runtime_tokens(
                        whatsapp_number_digits, name, runtime_vars
                    ).strip()
                    if not text:
                        raise ValueError(
                            "type_whatsapp_number: no WhatsApp number saved for this workflow — "
                            "set it under WhatsApp number (this workflow) in the Trainer, or in automation"
                        )
                    runtime_vars.pop("_WHATSAPP_WEB_NAV_NEED_CHROME", None)
                    runtime_vars.pop("_WHATSAPP_WEB_NAV_ACTIVATED", None)
                elif action == "type_completion_message":
                    runtime_vars.pop("_WHATSAPP_WEB_NAV_NEED_CHROME", None)
                    runtime_vars.pop("_WHATSAPP_WEB_NAV_ACTIVATED", None)
                    msg_tc, _url_tc = _trainer_whatsapp_completion_store_runtime(
                        name,
                        results=results,
                        mode=mode,
                        runtime_vars=runtime_vars,
                        dry_run=False,
                    )
                    text = (msg_tc or "").strip()
                    if not text:
                        raise ValueError("type_completion_message: generated completion body is empty")
                    runtime_vars["WHATSAPP_COMPLETION_TEXT"] = text
                    print(
                        f"      (type_completion_message: built from this run at this step — {len(text)} char(s); "
                        "pbcopy runs only with the paste below, not earlier)"
                    )
                elif action == "type_image_text_caption":
                    text = _resolve_runtime_tokens("{{CURRENT_CAPTION}}", name, runtime_vars).strip()
                    if not text:
                        raise ValueError(
                            "type_image_text_caption: CURRENT_CAPTION is empty — run Upload step first to bind media+caption"
                        )
                elif action == "type_video_text_caption":
                    text = _resolve_runtime_tokens("{{CURRENT_CAPTION}}", name, runtime_vars).strip()
                    if not text:
                        raise ValueError(
                            "type_video_text_caption: CURRENT_CAPTION is empty — run Upload step first to bind media+caption"
                        )
                elif action == "ai_type":
                    if not allow_ai:
                        raise RuntimeError("ai_type requires Smart mode; Fast mode disables AI text generation")
                    ai_prompt_raw = step.get("ai_prompt")
                    ai_prompt = _resolve_runtime_tokens(str(ai_prompt_raw or ""), name, runtime_vars)
                    if not ai_prompt:
                        raise ValueError("ai_type step has empty ai_prompt")
                    # Scheduler safety: if CURRENT_TOPIC exists but the step prompt forgot
                    # to include {{CURRENT_TOPIC}}, append it so each scheduled run varies.
                    cur_topic = (runtime_vars.get("CURRENT_TOPIC") or "").strip()
                    raw_prompt_text = str(ai_prompt_raw or "")
                    if cur_topic and "{{CURRENT_TOPIC}}" not in raw_prompt_text:
                        ai_prompt = f"{ai_prompt}\n\nPrimary topic to cover: {cur_topic}"
                    text = _generate_ai_type_text(
                        ai_prompt,
                        name,
                        runtime_vars,
                        str(step.get("ai_model") or "").strip(),
                    )
                    print(f"  ◆ Step {step.get('step')}: AI generated {len(text)} character(s)")
                else:
                    text_raw = step.get("type_text")
                    text = text_raw
                    if text is None:
                        text = step.get("description") or ""
                    text = _resolve_runtime_tokens(str(text), name, runtime_vars)
                    if not text:
                        raise ValueError("type step has empty type_text")
                sysn = _plat.system()
                darwin = sysn == "Darwin"

                if sysn in ("Darwin", "Windows") and _env_truthy(
                    "TRAINER_ACTIVATE_CHROME_BEFORE_WHATSAPP_STEPS", "1"
                ):
                    if action in ("type_whatsapp_number", "type_completion_message"):
                        _trainer_activate_chrome_before_whatsapp_keys(f"before {action}")
                    elif action == "type" and text and "web.whatsapp.com" in text.lower():
                        _trainer_activate_chrome_before_whatsapp_keys(
                            "before typing WhatsApp Web URL"
                        )

                _trainer_avoid_pyautogui_failsafe(pyautogui)

                _type_focus_delay = float(
                    (os.environ.get("TRAINER_TYPE_FOCUS_DELAY") or "0").strip() or "0"
                )
                if _type_focus_delay > 0:
                    time.sleep(_type_focus_delay)

                if darwin and os.environ.get("TRAINER_ACTIVATE_APP_BEFORE_TYPE", "").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                ):
                    _activate_trainer_target_app_if_configured()
                    time.sleep(0.45)

                if focus_target:
                    if not allow_ai:
                        print(
                            "      ⚠ Fast mode: skipping focus_target live-vision click; typing in current focus."
                        )
                    elif not _vision_keys_available():
                        print(
                            "      ⚠ Type focus_target skipped (no vision API key available); typing in current focus."
                        )
                    else:
                        try:
                            cap_path = SCREENSHOTS_DIR / "_runtime_vision_last.png"
                            time.sleep(float(os.environ.get("TRAINER_LIVE_VISION_DELAY", "0.35") or "0.35"))
                            _capture_screen_png(cap_path)
                            focus_coords = _analyse_click_with_retries(cap_path, focus_target)
                            fx = int(focus_coords.get("x") or 0)
                            fy = int(focus_coords.get("y") or 0)
                            if fx == 0 and fy == 0:
                                print(
                                    "      ⚠ Type focus_target returned (0,0); keeping current focus and continuing."
                                )
                            else:
                                pyautogui.click(fx, fy)
                                time.sleep(float((os.environ.get("TRAINER_TYPE_FOCUS_CLICK_DELAY") or "0.25").strip() or "0.25"))
                                print(
                                    f"  ◆ Step {step.get('step')}: Focused type field via live vision at ({fx},{fy}) "
                                    f"conf={focus_coords.get('confidence', '?')} — {focus_target}"
                                )
                        except Exception as focus_err:
                            print(
                                f"      ⚠ Type focus_target failed ({focus_err}); typing in current focus."
                            )

                if darwin and action == "type_completion_message":
                    _fd_res = _darwin_chrome_focus_whatsapp_compose()
                    if _fd_res == "ok":
                        print("      (WhatsApp Web: focused message compose box in Chrome before paste)")
                        try:
                            _w_comp = float(
                                (os.environ.get("TRAINER_WHATSAPP_COMPOSE_FOCUS_WAIT") or "0.45").strip()
                                or "0.45"
                            )
                        except (TypeError, ValueError):
                            _w_comp = 0.45
                        time.sleep(max(0.0, min(3.0, _w_comp)))
                    elif _fd_res != "skipped":
                        print(
                            f"      ⚠ WhatsApp compose focus returned {_fd_res!r} — "
                            "Cmd+V may not land in the message field; add a Click / focus_target on “Type a message”, "
                            "or set TRAINER_WHATSAPP_FOCUS_COMPOSE=0 if you focus manually before this step."
                        )
                        time.sleep(0.15)

                old_pause = getattr(pyautogui, "PAUSE", 0.1)
                try:
                    pyautogui.PAUSE = 0.05
                    _completion_paste_only = action == "type_completion_message" and (
                        (os.environ.get("TRAINER_COMPLETION_MESSAGE_PASTE_ONLY") or "1").strip().lower()
                        in ("1", "true", "yes")
                    )
                    if _completion_paste_only:
                        paste_err = None
                        try:
                            _type_via_clipboard_paste_only_pyautogui(pyautogui, text, darwin)
                        except Exception as e1:
                            paste_err = e1
                            if darwin and os.environ.get(
                                "TRAINER_NO_OSASCRIPT_TYPE", ""
                            ).strip().lower() not in ("1", "true", "yes"):
                                try:
                                    _darwin_pbcopy(text)
                                    _darwin_paste_only_system_events()
                                    paste_err = None
                                    print(
                                        "      (type_completion_message: System Events Cmd+V after "
                                        "PyAutoGUI paste-only failed)"
                                    )
                                except Exception as e2:
                                    paste_err = e2
                        if paste_err is not None:
                            raise RuntimeError(
                                f"type_completion_message: paste into compose failed ({paste_err!r}). "
                                "macOS: enable Accessibility (and often Input Monitoring) for the app that runs "
                                "Python. Or set TRAINER_COMPLETION_MESSAGE_PASTE_ONLY=0 to use Cmd+A+paste instead."
                            ) from paste_err
                        print(
                            "      (type_completion_message: Cmd+V only — no Cmd+A; "
                            "avoids WhatsApp Web contenteditable eating the paste)"
                        )
                    else:
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

                if action == "type_whatsapp_number":
                    print(f"  ✓ Step {step.get('step')}: Typed WhatsApp number — {text} ({len(text)} char(s))")
                elif action == "type_project_name":
                    print(f"  ✓ Step {step.get('step')}: Typed project/workflow name — {text!r} ({len(text)} char(s))")
                elif action == "type_completion_message":
                    preview = text.replace("\n", " ").strip()
                    if len(preview) > 80:
                        preview = preview[:77] + "..."
                    print(f"  ✓ Step {step.get('step')}: Typed completion message — {preview!r} ({len(text)} char(s))")
                elif action == "type_image_text_caption":
                    preview = text.replace("\n", " ").strip()
                    if len(preview) > 80:
                        preview = preview[:77] + "..."
                    print(f"  ✓ Step {step.get('step')}: Typed image caption — {preview!r} ({len(text)} char(s))")
                elif action == "type_video_text_caption":
                    preview = text.replace("\n", " ").strip()
                    if len(preview) > 80:
                        preview = preview[:77] + "..."
                    print(f"  ✓ Step {step.get('step')}: Typed video caption — {preview!r} ({len(text)} char(s))")
                else:
                    print(f"  ✓ Step {step.get('step')}: Typed {len(text)} character(s)")
                if action == "type_completion_message":
                    # One-shot: do not treat completion body as LAST_TYPED_TEXT / project name seed,
                    # and drop scratch so later steps cannot reuse this run's message by mistake.
                    runtime_vars.pop("WHATSAPP_COMPLETION_TEXT", None)
                    print(
                        "      (completion body was run-scratch only — cleared WHATSAPP_COMPLETION_TEXT; "
                        "not saved as LAST_TYPED_TEXT)"
                    )
                else:
                    runtime_vars["LAST_TYPED_TEXT"] = text
                    # First sane non-URL typed value becomes default project/folder name.
                    if not runtime_vars.get("PROJECT_FOLDER_NAME"):
                        if "://" not in text and "/" not in text and 1 <= len(text) <= 80:
                            runtime_vars["PROJECT_FOLDER_NAME"] = text
                    if action == "type" and "web.whatsapp.com" in text.lower():
                        runtime_vars["_WHATSAPP_WEB_NAV_NEED_CHROME"] = "1"
                        runtime_vars.pop("_WHATSAPP_WEB_NAV_ACTIVATED", None)
            elif action == "hotkey":
                _trainer_avoid_pyautogui_failsafe(pyautogui)
                _release_modifier_keys(pyautogui)
                key_list = _trainer_hotkey_keys_for_run(step)
                if not key_list:
                    raise RuntimeError(
                        "hotkey step has no keys — edit the step and set up to "
                        f"{_TRAINER_HOTKEY_MAX_KEYS} keys in the trainer"
                    )
                pyautogui.hotkey(*key_list)
                print(f"  ✓ Step {step.get('step')}: Hotkey {'+'.join(key_list)}")
            else:  # click
                _activate_trainer_target_app_if_configured()
                ix, iy = int(x or 0), int(y or 0)
                low_desc = (desc or "").strip().lower()
                use_mouse_cursor = (
                    "under mouse cursor" in low_desc
                    or "under the mouse cursor" in low_desc
                    or "current mouse position" in low_desc
                    or "cursor position" in low_desc
                )
                if use_mouse_cursor:
                    mouse_delay = float(
                        (os.environ.get("TRAINER_MOUSE_TARGET_DELAY") or "0.4").strip() or "0.4"
                    )
                    if mouse_delay > 0:
                        time.sleep(mouse_delay)
                    mx, my = pyautogui.position()
                    ix, iy = int(mx), int(my)
                    pyautogui.click(ix, iy)
                    print(
                        f"  ◆ Step {step.get('step')}: Mouse-assist click at ({ix},{iy}) — {desc}"
                    )
                    results.append(
                        _make_step_result(
                            step_num=step.get("step"),
                            action=action,
                            status="ok",
                            started_at_z=started_at_z,
                        )
                    )
                    continue
                saved_ix, saved_iy = ix, iy
                use_live = _trainer_use_live_vision_click(step, ix, iy, allow_ai=allow_ai)
                if _guard_on and not dry_run:
                    # Validate we can still locate the intended click target on the *current* screen.
                    # If the UI drifted (banner, modal, redesign), this aborts before performing the click.
                    if not (desc or "").strip():
                        raise RuntimeError("GUARD: click step has empty description — cannot validate target")
                    if not _vision_keys_available():
                        raise RuntimeError("GUARD: needs OPENAI_API_KEY or ANTHROPIC_API_KEY to validate clicks")
                    cap_path = SCREENSHOTS_DIR / "_runtime_guard_last.png"
                    _capture_screen_png(cap_path)
                    gcoords = _analyse_click_with_retries(cap_path, desc)
                    gx = int(gcoords.get("x") or 0)
                    gy = int(gcoords.get("y") or 0)
                    gconf = float(gcoords.get("confidence") or 0.0)
                    if gx == 0 and gy == 0:
                        raise RuntimeError("GUARD: could not locate target on current screen (0,0)")
                    if not use_live and (saved_ix != 0 or saved_iy != 0):
                        dx = float(gx - saved_ix)
                        dy = float(gy - saved_iy)
                        dist = (dx * dx + dy * dy) ** 0.5
                        if dist > _guard_max_dist_px:
                            raise RuntimeError(
                                f"GUARD: target moved too far vs saved coords "
                                f"(saved=({saved_ix},{saved_iy}) guard=({gx},{gy}) dist={dist:.0f}px > {_guard_max_dist_px:.0f}px)"
                            )
                    print(
                        f"      ✅ Guard verified: ({gx},{gy}) conf={gconf:.2f} — {desc}"
                    )
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
                    coords = _analyse_click_with_retries(cap_path, desc)
                    ix = int(coords.get("x") or 0)
                    iy = int(coords.get("y") or 0)
                    if ix == 0 and iy == 0:
                        scroll_retries = int(
                            (os.environ.get("TRAINER_LIVE_VISION_SCROLL_RETRIES") or "1").strip() or "1"
                        )
                        scroll_amount = int(
                            (os.environ.get("TRAINER_LIVE_VISION_SCROLL_AMOUNT") or "-650").strip() or "-650"
                        )
                        for _ in range(max(0, scroll_retries)):
                            pyautogui.scroll(scroll_amount)
                            time.sleep(0.35)
                            _capture_screen_png(cap_path)
                            coords = _analyse_click_with_retries(cap_path, desc)
                            ix = int(coords.get("x") or 0)
                            iy = int(coords.get("y") or 0)
                            if ix != 0 or iy != 0:
                                print(
                                    f"      ⚠ Live vision needed scroll retry; found target at ({ix},{iy})."
                                )
                                break
                    if ix == 0 and iy == 0:
                        if saved_ix != 0 or saved_iy != 0:
                            ix, iy = saved_ix, saved_iy
                            print(
                                f"      ⚠ Live vision returned (0,0); using saved coordinates ({ix},{iy}) instead."
                            )
                        else:
                            raise ValueError(
                                "Live vision returned (0,0) — improve the step description or check the screen"
                            )
                    print(
                        f"  ◆ Step {step.get('step')}: Live vision → ({ix},{iy}) "
                        f"conf={coords.get('confidence', '?')} — {desc}"
                    )
                elif ix == 0 and iy == 0:
                    if mode == "fast":
                        raise ValueError(
                            "Fast mode cannot resolve this click (no saved coordinates). Use Smart mode or re-train step."
                        )
                    raise ValueError(
                        "Click has no saved coordinates — enable “Live screen at run” for this step, "
                        "set TRAINER_LIVE_VISION_CLICKS=1, or re-save the step with a vision API key"
                    )
                pyautogui.click(ix, iy)
                if not use_live:
                    print(f"  ✓ Step {step.get('step')}: Clicked ({ix},{iy}) — {desc}")
            results.append(
                _make_step_result(
                    step_num=step.get("step"),
                    action=action,
                    status="ok",
                    started_at_z=started_at_z,
                )
            )
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
            results.append(
                _make_step_result(
                    step_num=step.get("step"),
                    action=action,
                    status="error",
                    started_at_z=started_at_z,
                    error=str(e),
                )
            )
            if str(e).startswith("GUARD:"):
                print("  ⚠ Guard abort — workflow halted due to UI mismatch.")
                break
            if _stop_requested() or "Run stopped by user" in str(e):
                print("  ⚠ Stop requested — workflow halted.")
                break
    try:
        import pyautogui as _pg_final
        _release_modifier_keys(_pg_final)
        _release_nav_keys(_pg_final)
    except Exception:
        pass
    if not dry_run:
        try:
            from agency_api.entitlements import infer_requires_ai
            from agency_api import entitlements_local_usage as _elu

            if infer_requires_ai(wf, run_mode):
                _elu.increment_local_ai_run()
        except Exception:
            pass
    return results


def run_wra_workflow(name: str, *, dry_run: bool, run_source: str) -> list[dict[str, Any]]:
    """
    WRA Engine v2 runner (Lucky → Agami → AHA™) wired into Trainer.

    - dry_run=True  : run Lucky only (validation)
    - dry_run=False : run full orchestrator (Lucky then Agami/AHA)
    """
    path = WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Workflow '{name}' not found")
    wf = json.loads(path.read_text())
    try:
        from agency_api.entitlements import assert_run_allowed_local

        assert_run_allowed_local(workflow_name=name, run_mode="wra", wf=wf)
    except PermissionError:
        raise

    started_at = _utc_now_z()
    _wra_monitor_update(
        running=True,
        workflow_name=name,
        lucky="NOT RUN",
        agami="IDLE",
        aha="IDLE",
        signals=[{"t": started_at, "msg": "MOVE", "detail": "run started"}],
        last_error="",
        last_result=None,
    )
    try:
        if dry_run:
            from cusear.engine.lucky import Lucky as _Lucky
            from cusear.engine.paths import WraPaths as _WraPaths

            _wra_monitor_update(lucky="NOT RUN", agami="VALIDATING")
            p = _WraPaths(root=str(BASE_DIR))
            lucky = _Lucky(logs_dir=str(p.lucky_logs_dir))
            rep = lucky.run(wf).to_dict()
            status = "ok" if rep.get("signal") == "GREEN" else "error"
            finished_at = _utc_now_z()
            _wra_monitor_update(
                running=False,
                lucky="PASSED" if rep.get("signal") == "GREEN" else "FAILED",
                agami="IDLE",
                aha="IDLE",
                last_result=rep,
                signals=[
                    {"t": finished_at, "msg": "DONE", "detail": f"Lucky signal={rep.get('signal')}"}
                ],
            )
            return [
                _audit_step(
                    1,
                    "wra_lucky",
                    status,
                    started_at,
                    finished_at,
                    error=rep.get("abort_reason", "") if status == "error" else "",
                    detail=f"signal={rep.get('signal')} drift_entries={len(rep.get('drift_map') or [])}",
                )
            ]

        # Live run: full orchestrator
        from cusear.engine.wra_engine import run_wra as _run_wra

        _wra_monitor_update(lucky="PASSED", agami="HEALING", aha="WAITING")
        out = _run_wra(
            repo_root=str(BASE_DIR),
            workflow_path=str(path),
            content_map={},
            company_endpoint=(os.environ.get("COMPANY_ENDPOINT") or "").strip() or None,
            enable_mouse_guard=True,
            enable_focus_mode=True,
        )
        finished_at = _utc_now_z()
        ok = bool(out.get("ok"))
        session_path = str(out.get("session_path") or "")
        reason = str(out.get("reason") or "")
        _wra_monitor_update(
            running=False,
            lucky="PASSED" if (out.get("lucky_report") or {}).get("signal") == "GREEN" else "FAILED",
            agami="IDLE" if ok else "ABORT",
            aha="DONE" if ok else "TIMEOUT",
            last_result=out,
            last_error=reason if not ok else "",
            signals=[{"t": finished_at, "msg": "DONE", "detail": "LANDED" if ok else reason}],
        )
        return [
            _audit_step(
                1,
                "wra_lucky",
                "ok" if (out.get("lucky_report") or {}).get("signal") == "GREEN" else "error",
                started_at,
                finished_at,
                error=str((out.get("lucky_report") or {}).get("abort_reason") or ""),
                detail="Lucky dry-run completed",
            ),
            _audit_step(
                2,
                "wra_runtime",
                "ok" if ok else "error",
                started_at,
                finished_at,
                error=reason,
                detail=f"session_clone={session_path}" if session_path else "",
            ),
        ]
    except Exception as exc:
        finished_at = _utc_now_z()
        _wra_monitor_update(
            running=False,
            agami="ABORT",
            aha="TIMEOUT",
            last_error=str(exc),
            signals=[{"t": finished_at, "msg": "DONE", "detail": str(exc)}],
        )
        raise


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
    Find click coordinates from a step screenshot using a configured vision provider.

    TRAINER_VISION_PROVIDER:
      - openai (default): require OPENAI_API_KEY
      - auto: OpenAI first; fallback to Anthropic only if OPENAI fails and ANTHROPIC key exists
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
    raise ValueError("Set OPENAI_API_KEY for click training (or ANTHROPIC_API_KEY if provider=anthropic)")


def _analyse_click_with_retries(image_path: Path, description: str) -> dict:
    """
    Retry live vision with a stricter prompt before giving up.
    """
    quoted = re.findall(r'"([^"]{2,80})"', description or "")
    target_txt = quoted[0].strip() if quoted else ""
    desc_l = (description or "").lower()
    prompts = [description]
    if target_txt:
        prompts.append(
            (
                f'{description}\nTarget exact visible text: "{target_txt}". '
                "Find that text on screen, then return the center of its clickable ancestor element."
            )
        )
    if any(k in desc_l for k in ("bottom", "end of page", "below", "footer")):
        prompts.append(
            (
                f"{description}. Prioritize elements in the lower half of the page and choose the primary CTA."
            )
        )
    prompts.append(
        (
            f'{description}. Use exact visible text and return the center pixel of the clickable element. '
            "If multiple matches exist, choose the most prominent enabled action control."
        )
    )
    last = {"x": 0, "y": 0, "action": "click", "confidence": 0}
    for idx, prompt in enumerate(prompts, start=1):
        out = analyse_screenshot_for_click(image_path, prompt)
        x = int(out.get("x") or 0)
        y = int(out.get("y") or 0)
        if x != 0 or y != 0:
            return out
        last = out
        if idx < len(prompts):
            time.sleep(0.2)
    return last


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")

    def _json(self, data, status=200, no_cache: bool = False):
        # Client can disconnect before the response is fully written (e.g. health poll); avoid noisy tracebacks.
        _conn = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
        try:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            if no_cache:
                self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
        except _conn:
            return
        try:
            self.wfile.write(body)
        except _conn:
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        # Trainer UI (file:// or another origin) uses fetch + X-API-Key; preflight must allow it.
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        qs = parse_qs(parsed.query or "")
        if p in ("/trainer", "/trainer/") or p == "/trainer.html":
            tp = _trainer_html_path()
            if not tp.is_file():
                self._json({"error": "TRAINER.html not found"}, 404, no_cache=True)
                return
            _send_local_file(self, tp)
            return
        if p.startswith("/downloads/"):
            rel = unquote(p[len("/downloads/") :].strip("/"))
            if not rel or "/" in rel or "\\" in rel:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            dl = _safe_leaf_file_under(DOWNLOADS_ROOT, rel)
            if dl is None:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            _send_local_file(self, dl)
            return
        ms = _marketing_site_root()
        portal_leaf = unquote(p.lstrip("/"))
        if ms is not None:
            home = _marketing_home_file(ms)
            content_root = _marketing_content_root(ms)
            if p in ("/", "/index.html"):
                if home is not None:
                    _send_local_file(self, home)
                    return
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            if portal_leaf:
                mf = _safe_site_file_under(content_root, portal_leaf)
                if mf is not None:
                    _send_local_file(self, mf)
                    return
            # Marketing UX is active: never fall back to ``portal/`` or TRAINER for unknown paths.
        pf: Optional[Path] = None
        if ms is None and portal_leaf and "/" not in portal_leaf:
            pf = _safe_leaf_file_under(PORTAL_ROOT, portal_leaf)
        if ms is None and p in ("/", "/index.html"):
            pix = PORTAL_ROOT / "index.html"
            if pix.is_file():
                _send_local_file(self, pix)
                return
            tp = _trainer_html_path()
            if tp.is_file():
                _send_local_file(self, tp)
                return
            self._json({"error": "no portal index and no trainer"}, 404, no_cache=True)
            return
        if pf is not None:
            _send_local_file(self, pf)
            return
        elif p == "/app/version":
            self._json(
                {"version": _read_app_version(), "brand": "cusear™", "product": "Control Center"},
                no_cache=True,
            )
        elif p == "/health":
            try:
                from agency_api.entitlements import enforcement_enabled

                _enf = enforcement_enabled()
            except Exception:
                _enf = False
            _mods = []
            try:
                import os as _os

                rawm = (_os.environ.get("ENTITLED_MODULES") or "").strip()
                if rawm:
                    _mods = [x.strip().lower() for x in rawm.split(",") if x.strip()]
            except Exception:
                _mods = []
            self._json(
                {
                    "status": "ok",
                    "mode": _app_mode(),
                    "entitlements": {"enforcement": _enf, "entitled_modules": _mods},
                }
            )
        elif p == "/mode":
            locked_slug = _cusear_default_ar_slug() if _is_consumer_mode() else ""
            self._json(
                {
                    "mode": _app_mode(),
                    "default_ar_slug": locked_slug,
                    "locked_consumer_ui": bool(locked_slug),
                },
                no_cache=True,
            )
        elif p == "/run/status":
            locked = bool(_RUN_WORKFLOW_LOCK.locked())
            self._json(
                {
                    "running": locked,
                    "stop_requested": _stop_requested(),
                },
                no_cache=True,
            )
        elif p == "/wra/rekky/status":
            with _WRA_REKKY_LOCK:
                self._json(dict(_WRA_REKKY_STATUS), no_cache=True)
        elif p == "/control/status":
            ok_chrome = _control_chrome_ok()
            ok_net, tot_net = _control_platform_reachability()
            free_gb = _control_disk_free_gb()
            perm = "Granted" if ok_chrome else "Required"
            self._json(
                {
                    "chrome_connected": ok_chrome,
                    "permissions": perm,
                    "permissions_sub": _control_permissions_summary(),
                    "network_online": ok_net > 0,
                    "platform_reachability": f"{ok_net}/{tot_net} platforms",
                    "disk_free_gb": free_gb,
                    "engine_ready": not _CONTROL_ENGINE_STOP_REQUESTED,
                },
                no_cache=True,
            )
        elif p == "/control/engine/log":
            try:
                n = int((qs.get("lines") or ["20"])[0] or 20)
            except ValueError:
                n = 20
            self._json({"lines": _control_engine_log_tail(n)}, no_cache=True)
        elif p == "/control/activity/recent":
            self._json({"items": _control_recent_runs(10)}, no_cache=True)
        elif p == "/control/schedule/upcoming":
            self._json({"items": _control_upcoming_scheduled()}, no_cache=True)
        elif p == "/control/settings":
            self._json({"settings": _load_control_settings()}, no_cache=True)
        elif p == "/control/open-logs-folder":
            # Desktop clients: returns path only (reveal in shell is OS-specific).
            self._json({"path": str((BASE_DIR / "logs").resolve())}, no_cache=True)
        elif p == "/wra/monitor":
            with _WRA_MONITOR_LOCK:
                mon = dict(_WRA_MONITOR)
            mon["run_lock_busy"] = bool(_RUN_WORKFLOW_LOCK.locked())
            self._json(mon, no_cache=True)
        elif p == "/wra/last-lucky-report":
            self._json({"report": dict(_LAST_LUCKY_REPORT) if _LAST_LUCKY_REPORT else None}, no_cache=True)
        elif p == "/wra/lucky/job":
            with _LUCKY_JOB_LOCK:
                self._json(dict(_LUCKY_JOB_STATUS), no_cache=True)
        elif p == "/trainer/automation-summary":
            self._json(_trainer_automation_auto_run_summary(), no_cache=True)
        elif p == "/best-ai/job":
            jid = str((qs.get("id") or [""])[0]).strip()
            if not jid:
                self._json({"error": "id query param required"}, 400, no_cache=True)
                return
            with _BEST_AI_JOBS_LOCK:
                job = dict(_BEST_AI_JOBS.get(jid) or {})
            if not job:
                self._json({"error": "job not found"}, 404, no_cache=True)
                return
            self._json({"job": job}, no_cache=True)
        elif p == "/best-ai/ui-bridge":
            self._json(_best_ai_bridge_get(), no_cache=True)
        elif p == "/bundles":
            bundles = _list_bundles()
            locked_slug = _cusear_default_ar_slug() if _is_consumer_mode() else ""
            if locked_slug:
                bundles = [b for b in bundles if str(b.get("slug") or "").strip() == locked_slug]
            self._json({"bundles": bundles}, no_cache=True)
        elif p.startswith("/bundle/"):
            slug = unquote(p[len("/bundle/") :]).strip("/")
            if not slug:
                self._json({"error": "bundle slug required"}, 400, no_cache=True)
                return
            if _cusear_default_ar_slug() and _is_consumer_mode() and slug != _cusear_default_ar_slug():
                self._json({"error": "bundle not found"}, 404, no_cache=True)
                return
            fp = _bundle_path(slug)
            if not fp.exists():
                self._json({"error": "bundle not found"}, 404, no_cache=True)
                return
            with _BUNDLE_IO_LOCK:
                data = _normalize_bundle(json.loads(fp.read_text()))
            self._json({"bundle": data}, no_cache=True)
        elif p == "/export-desktop-ar/capabilities":
            cap = _desktop_export_capabilities()
            self._json(cap, no_cache=True)
        elif p == "/export-desktop-ar/job":
            jid = str((qs.get("id") or [""])[0]).strip()
            if not jid:
                self._json({"error": "id query param required"}, 400, no_cache=True)
                return
            with _DESKTOP_EXPORT_JOBS_LOCK:
                job = dict(_DESKTOP_EXPORT_JOBS.get(jid) or {})
            if not job:
                self._json({"error": "job not found"}, 404, no_cache=True)
                return
            log = str(job.get("log") or "")
            if len(log) > 12000:
                log = log[-12000:]
            job["log"] = log
            self._json({"job": job}, no_cache=True)
        elif p == "/export-desktop-ar/download":
            jid = str((qs.get("id") or [""])[0]).strip()
            if not jid:
                self._json({"error": "id query param required"}, 400, no_cache=True)
                return
            with _DESKTOP_EXPORT_JOBS_LOCK:
                job = dict(_DESKTOP_EXPORT_JOBS.get(jid) or {})
            if not job:
                self._json({"error": "job not found"}, 404, no_cache=True)
                return
            if job.get("status") != "ok":
                self._json({"error": "export not ready", "status": job.get("status")}, 400, no_cache=True)
                return
            ap = Path(str(job.get("artifact") or "")).resolve()
            root = (_trainer_exports_dir()).resolve()
            try:
                ap.relative_to(root)
            except ValueError:
                self._json({"error": "artifact not available"}, 404, no_cache=True)
                return
            if not ap.is_file():
                self._json({"error": "artifact not available"}, 404, no_cache=True)
                return
            try:
                blob = ap.read_bytes()
            except Exception as exc:
                self._json({"error": str(exc)}, 500, no_cache=True)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Access-Control-Allow-Origin", "*")
            disp = f'attachment; filename="{ap.name}"'
            self.send_header("Content-Disposition", disp)
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(blob)
        elif p == "/workflows":
            wfs = []
            allowed = _consumer_ar_child_workflow_names()
            for f in sorted(WORKFLOWS_DIR.glob("*.json")):
                if allowed is not None and f.stem not in allowed:
                    continue
                try:
                    d = json.loads(f.read_text())
                    steps = d.get("steps") or []
                    n = len(steps) if isinstance(steps, list) else int(d.get("total_steps") or 0)
                    eng = _infer_workflow_engine(d)
                    plat = str(d.get("platform") or "").strip()
                    meta = _workflow_last_run_meta(f.stem)
                    wfs.append(
                        {
                            "name": f.stem,
                            "total_steps": n,
                            "platform": plat,
                            "engine": eng,
                            "last_run": meta.get("last_run", ""),
                            "last_result": meta.get("last_result", "never"),
                            "last_ok": bool(meta.get("last_ok", False)),
                            "rekky_enriched_at": str(d.get("rekky_enriched_at") or ""),
                        }
                    )
                except Exception:
                    pass
            self._json({"workflows": wfs}, no_cache=True)
        elif p == "/cusear/calendar-upload-info":
            max_d = calendar_total_days_env()
            plan_raw = str((qs.get("plan") or [""])[0] or "").strip().lower()
            media_raw = str((qs.get("media") or [""])[0] or "").strip().lower()
            platform_raw = str((qs.get("platform") or [""])[0] or "").strip().lower()
            dl = _downloads_dir()
            root = vault_root(dl).resolve()

            def norm_plan(x: str) -> str:
                t = (x or "").strip().lower().replace("-", "_").replace(" ", "_")
                if t in ("core",):
                    return "core"
                if t in ("hybrid",):
                    return "hybrid"
                if t in ("ai_budget", "aibudget", "budget", "ai"):
                    return "ai_budget"
                if t in ("ai_pro", "aipro", "pro"):
                    return "ai_pro"
                return ""

            def norm_media(x: str) -> str:
                t = (x or "").strip().lower()
                if t in ("text", "texts", "caption", "captions"):
                    return "text"
                if t in ("image", "images", "img"):
                    return "image"
                if t in ("video", "videos", "vid"):
                    return "video"
                return ""

            plan_key = norm_plan(plan_raw)
            media_key = norm_media(media_raw)
            rel = ""
            folder = root
            if plan_key and media_key:
                try:
                    dest = slot_path(dl, plan=plan_key, media=media_key, day=1, platform=platform_raw or None)
                    folder = dest.parent.resolve()
                except Exception:
                    folder = root
            try:
                rel = str(folder.relative_to(dl.resolve()))
            except ValueError:
                rel = str(folder)
            self._json(
                {
                    "ok": True,
                    "calendar_max_days": max_d,
                    "downloads_root": str(dl),
                    "vault_root": str(root),
                    "relative_to_downloads": rel,
                },
                no_cache=True,
            )
        elif p == "/cusear/storage/slot-info":
            plan_raw = str((qs.get("plan") or [""])[0] or "").strip().lower()
            media_raw = str((qs.get("media") or [""])[0] or "").strip().lower()
            platform_raw = str((qs.get("platform") or [""])[0] or "").strip().lower()
            day_raw = str((qs.get("day") or [""])[0] or "1").strip()
            try:
                day = int(day_raw)
            except ValueError:
                day = 1

            def norm_plan(x: str) -> str:
                t = (x or "").strip().lower().replace("-", "_").replace(" ", "_")
                if t in ("core",):
                    return "core"
                if t in ("hybrid",):
                    return "hybrid"
                if t in ("ai_budget", "aibudget", "budget", "ai"):
                    return "ai_budget"
                if t in ("ai_pro", "aipro", "pro"):
                    return "ai_pro"
                return ""

            def norm_media(x: str) -> str:
                t = (x or "").strip().lower()
                if t in ("text", "texts"):
                    return "text"
                if t in ("image", "images"):
                    return "image"
                if t in ("video", "videos"):
                    return "video"
                return ""

            plan = norm_plan(plan_raw)
            media = norm_media(media_raw)
            if plan not in ("core", "hybrid", "ai_budget", "ai_pro") or media not in ("text", "image", "video"):
                self._json({"error": "plan + media required"}, 400, no_cache=True)
                return
            if day < 1 or day > calendar_total_days_env():
                self._json({"error": f"day must be 1..{calendar_total_days_env()}"}, 400, no_cache=True)
                return
            if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                self._json({"error": "platform required for AI Pro"}, 400, no_cache=True)
                return
            dl = _downloads_dir()
            _cusear_ensure_storage_plan(dl, plan, platform_raw)
            dest = slot_path(dl, plan=plan, media=media, day=day, platform=(platform_raw or None))
            exists = dest.exists() and dest.is_file()
            size = dest.stat().st_size if exists else 0
            preview = ""
            if exists and media == "text":
                try:
                    preview = dest.read_text(encoding="utf-8", errors="replace")[:4000]
                except Exception:
                    preview = ""
            try:
                rel = str(dest.resolve().relative_to(dl.resolve()))
            except ValueError:
                rel = str(dest.resolve())
            self._json(
                {
                    "ok": True,
                    "plan": plan,
                    "media": media,
                    "platform": platform_raw,
                    "day": day,
                    "path": str(dest.resolve()),
                    "relative_to_downloads": rel,
                    "exists": bool(exists),
                    "size_bytes": int(size),
                    "preview": preview,
                },
                no_cache=True,
            )
        elif p == "/cusear/storage/ai-ready":
            self._json(
                {
                    "ok": True,
                    "openai_configured": _openai_api_key_configured(),
                },
                no_cache=True,
            )
        elif p == "/cusear/storage/legacy-list":
            dl = _downloads_dir()
            self._json(list_cusear_legacy_top_level(dl), no_cache=True)
        elif p == "/consumer/prompts":
            slug = _cusear_default_ar_slug() if _is_consumer_mode() else ""
            if not slug:
                self._json({"error": "not available"}, 404, no_cache=True)
                return
            fpb = _bundle_path(slug)
            if not fpb.exists():
                self._json({"error": "bundle not found"}, 404, no_cache=True)
                return
            with _BUNDLE_IO_LOCK:
                bundle = _normalize_bundle(json.loads(fpb.read_text()))
            children = [str(x or "").strip() for x in (bundle.get("children") or []) if str(x or "").strip()]
            out = []
            for ch in children:
                out.append({"workflow_name": ch, "prompt": _workflow_prompt_seed(ch)})
            self._json({"bundle_slug": slug, "workflows": out}, no_cache=True)
        elif p == "/runs/latest":
            latest = RUN_AUDIT_DIR / "latest.json"
            if latest.exists():
                self._json(json.loads(latest.read_text()), no_cache=True)
            else:
                self._json({"error": "no run audits yet"}, 404, no_cache=True)
        elif p == "/runs":
            runs = []
            for f in sorted(RUN_AUDIT_DIR.glob("*.json"), reverse=True):
                if f.name == "latest.json":
                    continue
                try:
                    d = json.loads(f.read_text())
                    runs.append(
                        {
                            "file": f.name,
                            "workflow_name": d.get("workflow_name", ""),
                            "audited_at": d.get("audited_at", ""),
                            "dry_run": bool(d.get("dry_run", False)),
                            "total_steps": int(d.get("total_steps", 0)),
                            "error_steps": int(d.get("error_steps", 0)),
                        }
                    )
                except Exception:
                    continue
                if len(runs) >= 25:
                    break
            self._json({"runs": runs}, no_cache=True)
        elif p == "/media/list":
            requested = str((qs.get("type") or [""])[0]).strip().lower()
            if requested and requested not in ("image", "video"):
                self._json({"error": "type must be image or video"}, 400, no_cache=True)
                return
            with _MEDIA_IO_LOCK:
                idx = _load_media_index()
                assets = [a for a in idx.get("assets") or [] if isinstance(a, dict)]
            if requested:
                assets = [a for a in assets if str(a.get("asset_type") or "") == requested]
            assets = sorted(assets, key=lambda a: str(a.get("created_at") or ""), reverse=True)
            self._json({"assets": [_public_asset(a) for a in assets]}, no_cache=True)
        elif p == "/ai-media/status":
            job_id = str((qs.get("job_id") or [""])[0]).strip()
            with _AI_MEDIA_JOBS_LOCK:
                if not job_id and _AI_MEDIA_ACTIVE_JOB_ID:
                    job_id = _AI_MEDIA_ACTIVE_JOB_ID
                job = _AI_MEDIA_JOBS.get(job_id) if job_id else None
            if not job:
                self._json({"error": "job not found"}, 404, no_cache=True)
                return
            self._json({"job": _ai_media_public_job(job)}, no_cache=True)
        elif p == "/ai-media/active":
            with _AI_MEDIA_JOBS_LOCK:
                job = _AI_MEDIA_JOBS.get(_AI_MEDIA_ACTIVE_JOB_ID) if _AI_MEDIA_ACTIVE_JOB_ID else None
            if not job:
                self._json({"job": None}, no_cache=True)
                return
            self._json({"job": _ai_media_public_job(job)}, no_cache=True)
        elif p == "/media/preflight_report":
            name = str((qs.get("workflow") or [""])[0]).strip()
            if not name:
                self._json({"error": "workflow query param required"}, 400, no_cache=True)
                return
            fp = WORKFLOWS_DIR / f"{name}.json"
            if not fp.exists():
                self._json({"error": "workflow not found"}, 404, no_cache=True)
                return
            with _WORKFLOW_IO_LOCK:
                wf = json.loads(fp.read_text())
                auto = _ensure_workflow_automation_shape(wf)
                plan, _days = _ensure_campaign_shape(auto)
                report = _media_preflight_report(auto)
            self._json(
                {
                    "workflow_name": name,
                    "media_enabled": bool(plan.get("enabled", False)),
                    "report": report,
                },
                no_cache=True,
            )
        elif p.startswith("/workflow/") and p.endswith("/campaign"):
            name = unquote(p[len("/workflow/") : -len("/campaign")]).strip("/")
            fp = WORKFLOWS_DIR / f"{name}.json"
            if not fp.exists():
                self._json({"error": "workflow not found"}, 404, no_cache=True)
                return
            with _WORKFLOW_IO_LOCK:
                wf = json.loads(fp.read_text())
                auto = _ensure_workflow_automation_shape(wf)
                plan, days = _ensure_campaign_shape(auto)
                fp.write_text(json.dumps(wf, indent=2))
            self._json(
                {
                    "workflow_name": name,
                    "media_plan": plan,
                    "campaign_days": days,
                },
                no_cache=True,
            )
        elif p.startswith("/workflow/") and p.endswith("/automation"):
            name = unquote(p[len("/workflow/") : -len("/automation")]).strip("/")
            fp = WORKFLOWS_DIR / f"{name}.json"
            if not fp.exists():
                self._json({"error": "workflow not found"}, 404, no_cache=True)
                return
            with _WORKFLOW_IO_LOCK:
                wf = json.loads(fp.read_text())
                auto = _ensure_workflow_automation_shape(wf)
                plan, _ = _ensure_campaign_shape(auto)
                if not (bool(plan.get("enabled")) and _scheduler_uses_media_campaign()):
                    err = str(auto.get("auto_last_error") or "")
                    if "Campaign blocked" in err or "requires review approval" in err.lower():
                        auto["auto_last_error"] = ""
                topic_error = ""
                if bool(auto.get("enabled", False)) and _scheduler_uses_topic_ai():
                    try:
                        _changed, topic_error = _ensure_topics_queue(auto, count=30)
                    except Exception as e:
                        topic_error = str(e)
                    auto["auto_last_error"] = topic_error or ""
                # Do not overwrite next_run_at on read.
                fp.write_text(json.dumps(wf, indent=2))
            self._json({"workflow_name": name, "automation": auto}, no_cache=True)
        elif p.startswith("/workflow/"):
            # Locked consumer builds must not expose workflow steps JSON.
            if _cusear_default_ar_slug() and _is_consumer_mode():
                self._json({"error": "not found"}, 404, no_cache=True)
                return
            name = unquote(p[len("/workflow/"):])
            fp = WORKFLOWS_DIR / f"{name}.json"
            self._json(
                json.loads(fp.read_text()) if fp.exists() else {"error": "not found"},
                200 if fp.exists() else 404,
                no_cache=True,
            )
        elif p in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            # Browsers request these; avoid noisy 404 in DevTools.
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        p = self.path.split("?")[0]
        if p.startswith("/bundle/"):
            slug = unquote(p[len("/bundle/") :]).strip("/")
            fp = _bundle_path(slug)
            if fp.exists():
                fp.unlink()
                self._json({"deleted": True, "slug": slug})
            else:
                self._json({"error": "bundle not found"}, 404)
        elif "/step/" in p and p.startswith("/workflow/"):
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

        global _CONTROL_ENGINE_STOP_REQUESTED
        p       = self.path.split("?")[0]
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        ct      = self.headers.get("Content-Type", "")

        locked_slug = _cusear_default_ar_slug() if _is_consumer_mode() else ""
        locked_consumer_ui = bool(locked_slug)
        if locked_consumer_ui:
            # Single-AR consumer exports: allow schedule time + STOP + prompt editing (no steps exposed).
            allowed = {
                "/bundle",
                "/run/stop",
                "/permissions/trial",
                "/consumer/prompts",
                "/consumer/topics/regenerate",
                "/cusear/save-calendar-media",
                "/cusear/storage/init",
                "/cusear/storage/ensure-plan",
                "/cusear/storage/legacy-list",
                "/cusear/storage/cleanup-legacy",
                "/cusear/storage/upload-slot",
                "/cusear/storage/set-text",
                "/cusear/storage/ai-ready",
                "/cusear/storage/topics",
                "/cusear/storage/generate-seq",
                "/cusear/storage/generate-slot",
                "/cusear/storage/hybrid/generate-texts",
            }
            if p not in allowed:
                self._json({"error": "locked consumer build: editing is disabled"}, 403, no_cache=True)
                return

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
                ai_prompt_f = fields.get("ai_prompt", "")
                ai_model_f  = fields.get("ai_model", "")
                focus_field = fields.get("focus_target", "")
                url_field   = fields.get("url", "")
                tab_count_raw = fields.get("tab_count", "1")
                insert_after_raw = fields.get("insert_after", "")
                if isinstance(type_text, str):
                    type_text = type_text.replace("\r\n", "\n")
                else:
                    type_text = ""
                if isinstance(ai_prompt_f, str):
                    ai_prompt = ai_prompt_f.replace("\r\n", "\n").strip()
                else:
                    ai_prompt = ""
                ai_model = ai_model_f.strip() if isinstance(ai_model_f, str) else ""
                if isinstance(focus_field, str):
                    focus_target = focus_field.strip()
                else:
                    focus_target = ""
                if isinstance(url_field, str):
                    open_url = url_field.strip()
                else:
                    open_url = ""
                if isinstance(tab_count_raw, str):
                    tab_count_text = tab_count_raw.strip()
                else:
                    tab_count_text = "1"
                if isinstance(insert_after_raw, str):
                    insert_after_text = insert_after_raw.strip()
                else:
                    insert_after_text = ""
                if not wf_name:
                    self._json({"error": "workflow_name required"}, 400); return
                if action_type == "type" and not type_text:
                    self._json({"error": "type_text required for type action"}, 400); return
                if action_type in ("ai_type", "ai_image") and not ai_prompt:
                    self._json({"error": f"ai_prompt required for {action_type} action"}, 400); return
                if action_type == "open_url" and not open_url:
                    self._json({"error": "url required for open_url action"}, 400); return
                tab_count_val = 1
                if action_type in _TRAINER_TAB_COUNT_ACTIONS:
                    try:
                        tab_count_val = int(tab_count_text or "1")
                    except ValueError:
                        self._json({"error": "tab_count must be an integer"}, 400); return
                    if not (1 <= tab_count_val <= 200):
                        self._json({"error": "tab_count must be between 1 and 200"}, 400); return
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

                cal_layer_raw = fields.get("calendar_upload_layer", "")
                calendar_upload_layer = (
                    str(cal_layer_raw or "").strip().lower() if isinstance(cal_layer_raw, str) else ""
                )
                cal_pick_raw = fields.get("calendar_asset_pick", "auto")
                calendar_asset_pick = (
                    str(cal_pick_raw or "").strip().lower() if isinstance(cal_pick_raw, str) else "auto"
                )
                if calendar_asset_pick not in ("auto", "image", "video", "text"):
                    calendar_asset_pick = "auto"

                hk_list_for_step: Optional[list[str]] = None
                if action_type == "hotkey":
                    raw_hk = fields.get("hotkey_keys_json", "")
                    hk_list_for_step, hk_err = _trainer_parse_hotkey_keys_json(
                        raw_hk if isinstance(raw_hk, str) else ""
                    )
                    if hk_err:
                        self._json({"error": hk_err}, 400); return

                best_ai_slot_f = fields.get("best_ai_slot", "")
                best_ai_slot_v = (
                    str(best_ai_slot_f).strip().lower() if isinstance(best_ai_slot_f, str) else ""
                )
                if action_type == "best_ai_capture_slot_from_clipboard":
                    if best_ai_slot_v not in ("chatgpt", "gemini", "claude"):
                        self._json({"error": "best_ai_slot must be chatgpt, gemini, or claude"}, 400); return

                live_vis = str(fields.get("live_vision", "")).strip().lower() in ("1", "true", "yes")
                screenshots_early = fields.get("screenshot", [])
                add_wait_after = str(fields.get("add_wait_after", "1")).strip().lower() not in ("0", "false", "no")
                direct_jump = str(fields.get("direct_jump", "0")).strip().lower() in ("1", "true", "yes")
                direct_jump_shots = fields.get("direct_jump_screenshot", [])
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
                if action_type != "press_tab" and direct_jump:
                    direct_jump = False
                if direct_jump and not direct_jump_shots:
                    self._json(
                        {"error": "Direct jump for Tab needs a target screenshot upload"},
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
                    insert_at_idx = None
                    if insert_after_text:
                        try:
                            insert_after_num = int(insert_after_text)
                        except ValueError:
                            self._json({"error": "insert_after must be an integer step number"}, 400); return
                        if not (0 <= insert_after_num <= len(wf["steps"])):
                            self._json({"error": f"insert_after must be between 0 and {len(wf['steps'])}"}, 400); return
                        insert_at_idx = insert_after_num
                    step_num = (insert_at_idx + 1) if insert_at_idx is not None else (len(wf["steps"]) + 1)

                    step = {
                        "step": step_num,
                        "action_type": action_type,
                        "description": description,
                        "x": 0, "y": 0,
                        "status": "saved"
                    }
                    if action_type == "type":
                        step["type_text"] = type_text
                        if focus_target:
                            step["focus_target"] = focus_target
                        preview = type_text.replace("\n", " ").strip()
                        if len(preview) > 100:
                            preview = preview[:97] + "..."
                        step["description"] = preview or "Type text"
                    elif action_type == "upload":
                        note = description.strip()
                        step["description"] = (note or "Upload (manual)")[:220]
                        if calendar_upload_layer in ("core", "hybrid", "ai"):
                            step["calendar_upload_layer"] = calendar_upload_layer
                            step["calendar_asset_pick"] = calendar_asset_pick
                        else:
                            step.pop("calendar_upload_layer", None)
                            step.pop("calendar_asset_pick", None)
                    elif action_type in ("ai_type", "ai_image"):
                        step["ai_prompt"] = ai_prompt
                        if ai_model:
                            step["ai_model"] = ai_model
                        if focus_target:
                            step["focus_target"] = focus_target
                        preview = ai_prompt.replace("\n", " ").strip()
                        if len(preview) > 100:
                            preview = preview[:97] + "..."
                        if action_type == "ai_image":
                            step["description"] = ("AI image: " + preview) if preview else "AI image"
                        else:
                            step["description"] = ("AI: " + preview) if preview else "AI type"
                    elif action_type == "open_url":
                        step["url"] = open_url
                        step["description"] = open_url if len(open_url) <= 120 else open_url[:117] + "..."
                    elif action_type == "open_tab":
                        if open_url:
                            step["url"] = open_url
                            step["description"] = f"Open new tab: {open_url if len(open_url) <= 90 else (open_url[:87] + '...')}"
                        else:
                            step["description"] = "Open new browser tab"
                    elif action_type == "open_whatsapp":
                        step["url"] = _OPEN_WHATSAPP_WEB_URL
                        step["description"] = "Open WhatsApp Web — https://web.whatsapp.com/"
                    elif action_type == "completion_link":
                        step["description"] = "Generate WhatsApp completion link from this run"
                    elif action_type == "completion_message":
                        step["description"] = "Output completion text (same as link body); copy message; no browser"
                    elif action_type == "completion_clipboard_refresh":
                        step["description"] = (
                            "Re-copy completion body from memory to clipboard (after Type URL wiped it)"
                        )
                    elif action_type == "type_project_name":
                        step["description"] = "Typing project name"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_whatsapp_number":
                        step["description"] = "Type WhatsApp number saved for this workflow"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_completion_message":
                        step["description"] = "Type WhatsApp completion body from prior completion step"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_image_text_caption":
                        step["description"] = "Type caption for current uploaded image item"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_video_text_caption":
                        step["description"] = "Type caption for current uploaded video item"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "wait":
                        ws_save = float(str(fields.get("wait_seconds", "2") or "2").strip())
                        step["wait_seconds"] = ws_save
                        note = description.strip()
                        step["description"] = (
                            (f"Wait {ws_save:g}s — {note}" if note else f"Wait {ws_save:g}s")[:220]
                        )
                    elif action_type in _TRAINER_TAB_COUNT_ACTIONS:
                        step["tab_count"] = tab_count_val
                        if action_type == "press_tab":
                            step["direct_jump"] = bool(direct_jump)
                        if action_type == "press_tab":
                            step["description"] = f"Press Tab x{tab_count_val}"
                        else:
                            step["description"] = (
                                f"Press {_TRAINER_ARROW_LABELS[action_type]} x{tab_count_val}"
                            )
                        if action_type in _TRAINER_ARROW_PY_KEYS:
                            step["tab_press_increment"] = _trainer_parse_tab_press_increment(fields, 1)
                            if str(fields.get("repeat_scale_campaign_day", "")).strip().lower() in (
                                "1",
                                "true",
                                "yes",
                            ):
                                step["repeat_scale_campaign_day"] = True
                            else:
                                step.pop("repeat_scale_campaign_day", None)
                    elif action_type == "shell":
                        step["shell_command"] = shell_cmd
                        step["description"] = (shell_cmd[:117] + "...") if len(shell_cmd) > 120 else shell_cmd
                    elif action_type == "press_automation_grid_nav":
                        try:
                            gcols = int(str(fields.get("grid_nav_cols", "6") or "6").strip())
                        except ValueError:
                            gcols = 6
                        try:
                            grows = int(str(fields.get("grid_nav_rows", "5") or "5").strip())
                        except ValueError:
                            grows = 5
                        gcols = max(2, min(50, gcols))
                        grows = max(1, min(20, grows))
                        step["grid_nav_cols"] = gcols
                        step["grid_nav_rows"] = grows
                        note = description.strip()
                        step["description"] = (
                            (note + f" — {gcols}×{grows} snake")[:220]
                            if note
                            else f"Snake grid nav {gcols}×{grows} (automation run)"
                        )
                    elif action_type == "hotkey":
                        assert hk_list_for_step is not None
                        step["hotkey_keys"] = hk_list_for_step
                        note = description.strip()
                        combo = "+".join(hk_list_for_step)
                        step["description"] = (
                            (note[:120] + f" — {combo}")[:220] if note else f"Hotkey {combo}"
                        )
                    elif action_type == "best_ai_copy_query_bundle":
                        step["description"] = "Best AI: copy saved topic + platform instructions → clipboard"
                    elif action_type == "best_ai_capture_slot_from_clipboard":
                        step["best_ai_slot"] = best_ai_slot_v
                        step["description"] = f"Best AI: clipboard → Trainer slot ({best_ai_slot_v})"
                    elif action_type == "best_ai_run_synthesizer":
                        step["description"] = "Best AI: run OpenAI synthesizer (bridge slots → result)"

                    if action_type == "click" or (action_type == "press_tab" and direct_jump):
                        step["live_vision"] = live_vis
                        screenshots = direct_jump_shots if (action_type == "press_tab" and direct_jump) else fields.get("screenshot", [])
                        if screenshots:
                            img_data = screenshots[0]["data"]
                            stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
                            img_path = SCREENSHOTS_DIR / f"{wf_name}_step{step_num}_{stamp}.png"
                            img_path.write_bytes(img_data)
                            step["screenshot"] = img_path.name
                            if _vision_keys_available():
                                try:
                                    analyse_desc = description
                                    if action_type == "press_tab" and direct_jump:
                                        analyse_desc = description or "Click the final target after tab navigation"
                                    coords = analyse_screenshot_for_click(img_path, analyse_desc)
                                    step["x"] = coords.get("x", 0)
                                    step["y"] = coords.get("y", 0)
                                    if action_type == "press_tab" and direct_jump:
                                        step["trained_x"] = int(coords.get("x", 0) or 0)
                                        step["trained_y"] = int(coords.get("y", 0) or 0)
                                        step["confidence"] = float(coords.get("confidence", 0.0) or 0.0)
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
                        elif action_type == "open_tab":
                            print(f"  ✓ Step {step_num} saved: open_tab — {open_url or '(blank tab)'}")
                        elif action_type == "open_whatsapp":
                            print(f"  ✓ Step {step_num} saved: open_whatsapp — {_OPEN_WHATSAPP_WEB_URL}")
                        elif action_type == "completion_link":
                            print(f"  ✓ Step {step_num} saved: completion_link")
                        elif action_type == "completion_message":
                            print(f"  ✓ Step {step_num} saved: completion_message")
                        elif action_type == "completion_clipboard_refresh":
                            print(f"  ✓ Step {step_num} saved: completion_clipboard_refresh")
                        elif action_type == "type_project_name":
                            print(f"  ✓ Step {step_num} saved: type_project_name")
                        elif action_type == "type_whatsapp_number":
                            print(f"  ✓ Step {step_num} saved: type_whatsapp_number")
                        elif action_type == "type_completion_message":
                            print(f"  ✓ Step {step_num} saved: type_completion_message")
                        elif action_type == "type_image_text_caption":
                            print(f"  ✓ Step {step_num} saved: type_image_text_caption")
                        elif action_type == "type_video_text_caption":
                            print(f"  ✓ Step {step_num} saved: type_video_text_caption")
                        elif action_type == "upload":
                            print(f"  ✓ Step {step_num} saved: upload — {step.get('description', '')[:80]}")
                        elif action_type == "ai_type":
                            print(f"  ✓ Step {step_num} saved: ai_type — {step.get('description', '')[:80]}")
                        elif action_type == "ai_image":
                            print(f"  ✓ Step {step_num} saved: ai_image — {step.get('description', '')[:80]}")
                        elif action_type == "wait":
                            print(f"  ✓ Step {step_num} saved: wait {step.get('wait_seconds', '?')}s")
                        elif action_type in _TRAINER_TAB_COUNT_ACTIONS:
                            print(
                                f"  ✓ Step {step_num} saved: {action_type} x{step.get('tab_count', '?')}"
                            )
                        elif action_type == "shell":
                            print(f"  ✓ Step {step_num} saved: shell — {step.get('shell_command', '')[:80]}")
                        elif action_type == "press_automation_grid_nav":
                            print(
                                f"  ✓ Step {step_num} saved: press_automation_grid_nav "
                                f"{step.get('grid_nav_cols', '?')}×{step.get('grid_nav_rows', '?')}"
                            )
                        elif action_type == "hotkey":
                            print(
                                f"  ✓ Step {step_num} saved: hotkey "
                                f"{'+'.join(step.get('hotkey_keys') or [])}"
                            )
                        elif action_type == "best_ai_copy_query_bundle":
                            print(f"  ✓ Step {step_num} saved: best_ai_copy_query_bundle")
                        elif action_type == "best_ai_capture_slot_from_clipboard":
                            print(
                                f"  ✓ Step {step_num} saved: best_ai_capture_slot_from_clipboard "
                                f"({step.get('best_ai_slot', '')})"
                            )
                        elif action_type == "best_ai_run_synthesizer":
                            print(f"  ✓ Step {step_num} saved: best_ai_run_synthesizer")
                        elif action_type == "close_chrome":
                            print(f"  ✓ Step {step_num} saved: close_chrome")
                        else:
                            print(f"  ✓ Step {step_num} saved: {action_type} — {description}")

                    err_rng = _trainer_apply_automation_run_range_to_step(step, fields, old={})
                    if err_rng:
                        self._json({"error": err_rng}, 400)
                        return

                    if insert_at_idx is None:
                        wf["steps"].append(step)
                    else:
                        wf["steps"].insert(insert_at_idx, step)
                    if (
                        add_wait_after
                        and action_type != "wait"
                        and action_type != "completion_clipboard_refresh"
                        and action_type
                        not in (
                            "best_ai_copy_query_bundle",
                            "best_ai_capture_slot_from_clipboard",
                            "best_ai_run_synthesizer",
                        )
                    ):
                        wait_step = {
                            "step": 0,
                            "action_type": "wait",
                            "description": "Wait 5s",
                            "x": 0,
                            "y": 0,
                            "status": "saved",
                            "wait_seconds": 5.0,
                        }
                        if insert_at_idx is None:
                            wf["steps"].append(wait_step)
                        else:
                            wf["steps"].insert(insert_at_idx + 1, wait_step)
                    for i, s in enumerate(wf["steps"], 1):
                        s["step"] = i
                    wf["total_steps"] = len(wf["steps"])
                    wf_path.write_text(json.dumps(wf, indent=2))
                    out = {
                        "success": True, "step": step.get("step", step_num),
                        "total_steps": len(wf["steps"]),
                        "x": step["x"], "y": step["y"],
                        "status": step["status"],
                    }
                self._json(out)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── UPDATE ONE STEP ──────────────────────────────────────────────────
        elif p.startswith("/workflow/") and p.endswith("/update") and "/step/" in p:
            try:
                m = re.match(r"^/workflow/(.+)/step/(\d+)/update$", p)
                if not m:
                    self._json({"error": "invalid update path"}, 400); return
                wf_name = unquote(m.group(1))
                step_num = int(m.group(2))
                bnd = re.search(r'boundary=([^\s;]+)', ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400); return
                fields = parse_multipart(body, bnd.group(1))
                wf_path = WORKFLOWS_DIR / f"{wf_name}.json"
                if not wf_path.exists():
                    self._json({"error": "workflow not found"}, 404); return

                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(wf_path.read_text())
                    steps = wf.get("steps", [])
                    idx = next((i for i, s in enumerate(steps) if int(s.get("step", 0)) == step_num), None)
                    if idx is None:
                        self._json({"error": "step not found"}, 404); return
                    old = dict(steps[idx])

                    action_type = (fields.get("action_type") or old.get("action_type", "click")).strip()
                    description = (fields.get("description") or "").strip()
                    type_text = fields.get("type_text", old.get("type_text", ""))
                    ai_prompt_f = fields.get("ai_prompt", old.get("ai_prompt", ""))
                    ai_model_f = fields.get("ai_model", old.get("ai_model", ""))
                    url_field = fields.get("url", old.get("url", ""))
                    focus_field = fields.get("focus_target", old.get("focus_target", ""))
                    tab_count_raw = fields.get("tab_count", old.get("tab_count", 1))
                    if isinstance(type_text, str):
                        type_text = type_text.replace("\r\n", "\n")
                    else:
                        type_text = ""
                    if isinstance(ai_prompt_f, str):
                        ai_prompt = ai_prompt_f.replace("\r\n", "\n").strip()
                    else:
                        ai_prompt = ""
                    ai_model = ai_model_f.strip() if isinstance(ai_model_f, str) else ""
                    open_url = url_field.strip() if isinstance(url_field, str) else ""
                    focus_target = focus_field.strip() if isinstance(focus_field, str) else ""
                    tab_count_text = str(tab_count_raw).strip()
                    shell_cmd_f = fields.get("shell_command", old.get("shell_command", ""))
                    shell_cmd = shell_cmd_f.strip() if isinstance(shell_cmd_f, str) else ""

                    if action_type == "type" and not type_text:
                        self._json({"error": "type_text required for type action"}, 400); return
                    if action_type in ("ai_type", "ai_image") and not ai_prompt:
                        self._json({"error": f"ai_prompt required for {action_type} action"}, 400); return
                    if action_type == "open_url" and not open_url:
                        self._json({"error": "url required for open_url action"}, 400); return
                    tab_count_val = 1
                    if action_type in _TRAINER_TAB_COUNT_ACTIONS:
                        try:
                            tab_count_val = int(tab_count_text or "1")
                        except ValueError:
                            self._json({"error": "tab_count must be an integer"}, 400); return
                        if not (1 <= tab_count_val <= 200):
                            self._json({"error": "tab_count must be between 1 and 200"}, 400); return
                    if action_type == "wait":
                        try:
                            _ws = float(str(fields.get("wait_seconds", old.get("wait_seconds", "2")) or "2").strip())
                        except ValueError:
                            self._json({"error": "wait_seconds must be a number"}, 400); return
                        if not (0.0 <= _ws <= 120.0):
                            self._json({"error": "wait_seconds must be between 0 and 120"}, 400); return
                    if action_type == "shell" and not shell_cmd:
                        self._json({"error": "shell_command required for shell action"}, 400); return

                    cal_layer_raw_u = fields.get("calendar_upload_layer", old.get("calendar_upload_layer", ""))
                    calendar_upload_layer_u = (
                        str(cal_layer_raw_u or "").strip().lower()
                        if isinstance(cal_layer_raw_u, str)
                        else ""
                    )
                    cal_pick_raw_u = fields.get("calendar_asset_pick", old.get("calendar_asset_pick", "auto"))
                    calendar_asset_pick_u = (
                        str(cal_pick_raw_u or "").strip().lower()
                        if isinstance(cal_pick_raw_u, str)
                        else "auto"
                    )
                    if calendar_asset_pick_u not in ("auto", "image", "video", "text"):
                        calendar_asset_pick_u = "auto"

                    hk_list_for_step: Optional[list[str]] = None
                    if action_type == "hotkey":
                        raw_hk = fields.get("hotkey_keys_json", "")
                        if isinstance(raw_hk, str) and raw_hk.strip():
                            hk_list_for_step, hk_err = _trainer_parse_hotkey_keys_json(raw_hk)
                        else:
                            hk_list_for_step = _trainer_hotkey_keys_for_run(old)
                            hk_err = None if hk_list_for_step else "hotkey_keys_json required (or save keys again)"
                        if hk_err:
                            self._json({"error": hk_err}, 400); return
                        if not hk_list_for_step:
                            self._json({"error": "hotkey needs at least one key"}, 400); return

                    best_ai_slot_f_u = fields.get("best_ai_slot", old.get("best_ai_slot", ""))
                    best_ai_slot_v_u = (
                        str(best_ai_slot_f_u).strip().lower()
                        if isinstance(best_ai_slot_f_u, str)
                        else ""
                    )
                    if action_type == "best_ai_capture_slot_from_clipboard":
                        if best_ai_slot_v_u not in ("chatgpt", "gemini", "claude"):
                            self._json({"error": "best_ai_slot must be chatgpt, gemini, or claude"}, 400); return

                    step = {
                        "step": step_num,
                        "action_type": action_type,
                        "description": description,
                        "x": 0,
                        "y": 0,
                        "status": "saved",
                    }
                    if action_type == "type":
                        step["type_text"] = type_text
                        if focus_target:
                            step["focus_target"] = focus_target
                        preview = type_text.replace("\n", " ").strip()
                        step["description"] = (preview[:97] + "...") if len(preview) > 100 else (preview or "Type text")
                    elif action_type == "upload":
                        note = description.strip()
                        step["description"] = (note or "Upload (manual)")[:220]
                        if calendar_upload_layer_u in ("core", "hybrid", "ai"):
                            step["calendar_upload_layer"] = calendar_upload_layer_u
                            step["calendar_asset_pick"] = calendar_asset_pick_u
                        else:
                            step.pop("calendar_upload_layer", None)
                            step.pop("calendar_asset_pick", None)
                    elif action_type in ("ai_type", "ai_image"):
                        step["ai_prompt"] = ai_prompt
                        if ai_model:
                            step["ai_model"] = ai_model
                        if focus_target:
                            step["focus_target"] = focus_target
                        preview = ai_prompt.replace("\n", " ").strip()
                        if len(preview) > 100:
                            preview = preview[:97] + "..."
                        if action_type == "ai_image":
                            step["description"] = ("AI image: " + preview) if preview else "AI image"
                        else:
                            step["description"] = ("AI: " + preview) if preview else "AI type"
                    elif action_type == "open_url":
                        step["url"] = open_url
                        step["description"] = open_url if len(open_url) <= 120 else open_url[:117] + "..."
                    elif action_type == "open_tab":
                        if open_url:
                            step["url"] = open_url
                            step["description"] = f"Open new tab: {open_url if len(open_url) <= 90 else (open_url[:87] + '...')}"
                        else:
                            step["description"] = "Open new browser tab"
                    elif action_type == "open_whatsapp":
                        step["url"] = _OPEN_WHATSAPP_WEB_URL
                        step["description"] = "Open WhatsApp Web — https://web.whatsapp.com/"
                    elif action_type == "completion_link":
                        step["description"] = "Generate WhatsApp completion link from this run"
                    elif action_type == "completion_message":
                        step["description"] = "Output completion text (same as link body); copy message; no browser"
                    elif action_type == "completion_clipboard_refresh":
                        step["description"] = (
                            "Re-copy completion body from memory to clipboard (after Type URL wiped it)"
                        )
                    elif action_type == "type_project_name":
                        step["description"] = "Typing project name"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_whatsapp_number":
                        step["description"] = "Type WhatsApp number saved for this workflow"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_completion_message":
                        step["description"] = "Type WhatsApp completion body from prior completion step"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_image_text_caption":
                        step["description"] = "Type caption for current uploaded image item"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "type_video_text_caption":
                        step["description"] = "Type caption for current uploaded video item"
                        if focus_target:
                            step["focus_target"] = focus_target
                    elif action_type == "wait":
                        ws_save = float(str(fields.get("wait_seconds", old.get("wait_seconds", "2")) or "2").strip())
                        step["wait_seconds"] = ws_save
                        note = description.strip()
                        step["description"] = (f"Wait {ws_save:g}s — {note}" if note else f"Wait {ws_save:g}s")[:220]
                    elif action_type in _TRAINER_TAB_COUNT_ACTIONS:
                        step["tab_count"] = tab_count_val
                        if action_type == "press_tab":
                            step["description"] = f"Press Tab x{tab_count_val}"
                            if bool(old.get("direct_jump")):
                                step["direct_jump"] = True
                                for key in ("trained_x", "trained_y", "confidence", "screenshot"):
                                    if key in old:
                                        step[key] = old.get(key)
                        else:
                            step["description"] = (
                                f"Press {_TRAINER_ARROW_LABELS[action_type]} x{tab_count_val}"
                            )
                        if action_type in _TRAINER_ARROW_PY_KEYS:
                            try:
                                inc_d = int(old.get("tab_press_increment", 1) or 1)
                            except (TypeError, ValueError):
                                inc_d = 1
                            inc_d = max(1, min(200, inc_d))
                            step["tab_press_increment"] = _trainer_parse_tab_press_increment(fields, inc_d)
                            if str(fields.get("repeat_scale_campaign_day", "")).strip().lower() in (
                                "1",
                                "true",
                                "yes",
                            ):
                                step["repeat_scale_campaign_day"] = True
                            else:
                                step.pop("repeat_scale_campaign_day", None)
                    elif action_type == "shell":
                        step["shell_command"] = shell_cmd
                        step["description"] = (shell_cmd[:117] + "...") if len(shell_cmd) > 120 else shell_cmd
                    elif action_type == "press_automation_grid_nav":
                        try:
                            gcols = int(
                                str(fields.get("grid_nav_cols", old.get("grid_nav_cols", "6")) or "6").strip()
                            )
                        except ValueError:
                            gcols = 6
                        try:
                            grows = int(
                                str(fields.get("grid_nav_rows", old.get("grid_nav_rows", "5")) or "5").strip()
                            )
                        except ValueError:
                            grows = 5
                        gcols = max(2, min(50, gcols))
                        grows = max(1, min(20, grows))
                        step["grid_nav_cols"] = gcols
                        step["grid_nav_rows"] = grows
                        note = description.strip()
                        step["description"] = (
                            (note + f" — {gcols}×{grows} snake")[:220]
                            if note
                            else f"Snake grid nav {gcols}×{grows} (automation run)"
                        )
                    elif action_type == "hotkey":
                        assert hk_list_for_step is not None
                        step["hotkey_keys"] = hk_list_for_step
                        note = description.strip()
                        combo = "+".join(hk_list_for_step)
                        step["description"] = (
                            (note[:120] + f" — {combo}")[:220] if note else f"Hotkey {combo}"
                        )
                    elif action_type == "best_ai_copy_query_bundle":
                        step["description"] = "Best AI: copy saved topic + platform instructions → clipboard"
                    elif action_type == "best_ai_capture_slot_from_clipboard":
                        step["best_ai_slot"] = best_ai_slot_v_u
                        step["description"] = f"Best AI: clipboard → Trainer slot ({best_ai_slot_v_u})"
                    elif action_type == "best_ai_run_synthesizer":
                        step["description"] = "Best AI: run OpenAI synthesizer (bridge slots → result)"

                    if action_type == "click":
                        live_vis = bool(old.get("live_vision"))
                        if "live_vision" in fields:
                            live_vis = str(fields.get("live_vision", "")).strip().lower() in ("1", "true", "yes")
                        step["live_vision"] = live_vis
                        step["x"] = int(old.get("x") or 0)
                        step["y"] = int(old.get("y") or 0)
                        if old.get("screenshot"):
                            step["screenshot"] = old.get("screenshot")
                        screenshots = fields.get("screenshot", [])
                        if screenshots:
                            img_data = screenshots[0]["data"]
                            stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
                            img_path = SCREENSHOTS_DIR / f"{wf_name}_step{step_num}_{stamp}.png"
                            img_path.write_bytes(img_data)
                            step["screenshot"] = img_path.name
                            if _vision_keys_available():
                                try:
                                    coords = analyse_screenshot_for_click(img_path, description or old.get("description", ""))
                                    step["x"] = coords.get("x", 0)
                                    step["y"] = coords.get("y", 0)
                                    step["status"] = "analysed"
                                except Exception:
                                    step["status"] = "saved_no_vision"
                            else:
                                step["status"] = "saved_no_api_key"
                        else:
                            if live_vis:
                                step["status"] = "live_vision_run"
                            elif (step.get("x", 0) or step.get("y", 0) or step.get("screenshot")):
                                step["status"] = "saved"
                            else:
                                self._json(
                                    {"error": "Click step needs saved coordinates/screenshot, or enable Live screen at run."},
                                    400,
                                )
                                return

                    err_rng = _trainer_apply_automation_run_range_to_step(step, fields, old=old)
                    if err_rng:
                        self._json({"error": err_rng}, 400)
                        return

                    steps[idx] = step
                    for i, s in enumerate(steps, 1):
                        s["step"] = i
                    wf["steps"] = steps
                    wf["total_steps"] = len(steps)
                    wf_path.write_text(json.dumps(wf, indent=2))
                    self._json(
                        {
                            "success": True,
                            "step": steps[idx].get("step", step_num),
                            "total_steps": len(steps),
                            "x": steps[idx].get("x", 0),
                            "y": steps[idx].get("y", 0),
                            "status": steps[idx].get("status", "saved"),
                        }
                    )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── DELETE ONE STEP ───────────────────────────────────────────────────
        elif p.startswith("/workflow/") and "/step/" in p:
            # handled in do_DELETE
            self._json({"error": "use DELETE method"}, 405)

        # ── WEBSITE BUILDER (STUB TRIGGERS) ──────────────────────────────────
        elif p in ("/website/build/basic", "/website/build/admin"):
            try:
                website_type = "admin" if p.endswith("/admin") else "basic"
                payload_raw, files = _website_payload_from_request(content_type=ct, body=body)
                payload = _website_validate_payload(payload_raw, files, website_type)
                internal_prompt = _website_build_internal_prompt(payload)
                request_id = (
                    "ws_"
                    + datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
                    + "_"
                    + secrets.token_hex(4)
                )
                self._json(
                    {
                        "ok": True,
                        "status": "queued_stub",
                        "request_id": request_id,
                        "website_type": website_type,
                        "industry": str(payload.get("industry") or ""),
                        "prompt_chars": len(internal_prompt),
                        "note": (
                            "Workflow trigger stub accepted. "
                            "Connect this request_id to workflow execution in the next step."
                        ),
                    },
                    200,
                )
            except Exception as e:
                self._json({"error": str(e)}, 400)

        # ── AI MEDIA STUDIO ───────────────────────────────────────────────────
        elif p == "/ai-media/start":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            try:
                job = _start_ai_media_job(payload if isinstance(payload, dict) else {})
                self._json({"ok": True, "job": job}, 200)
            except Exception as e:
                self._json({"error": str(e)}, 400)

        elif p == "/ai-media/stop":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            job_id = str((payload or {}).get("job_id") or "").strip()
            with _AI_MEDIA_JOBS_LOCK:
                if not job_id and _AI_MEDIA_ACTIVE_JOB_ID:
                    job_id = _AI_MEDIA_ACTIVE_JOB_ID
                job = _AI_MEDIA_JOBS.get(job_id) if job_id else None
                if not job:
                    self._json({"error": "job not found"}, 404)
                    return
                job["stop_requested"] = True
                _ai_media_mark_updated(job)
            self._json({"ok": True, "job_id": job_id})

        # ── MEDIA LIBRARY ────────────────────────────────────────────────────
        elif p == "/media/upload":
            try:
                bnd = re.search(r'boundary=([^\s;]+)', ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400)
                    return
                fields = parse_multipart(body, bnd.group(1))
                file_items = []
                for k in ("media", "file", "asset", "upload"):
                    v = fields.get(k, [])
                    if isinstance(v, list) and v:
                        file_items = v
                        break
                if not file_items:
                    self._json({"error": "no media file uploaded"}, 400)
                    return
                first = file_items[0]
                filename = str(first.get("filename") or "upload.bin").strip()
                data = first.get("data") or b""
                if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
                    self._json({"error": "uploaded file is empty"}, 400)
                    return
                content_type = str(first.get("content_type") or "").strip().lower()
                asset_type = str(fields.get("asset_type") or "").strip().lower()
                kind = _media_kind_from_upload(asset_type, content_type, filename)
                if kind not in ("image", "video"):
                    self._json({"error": "unsupported media type; use image or video"}, 400)
                    return
                asset = _register_media_asset(
                    kind=kind,
                    source="uploaded",
                    data=bytes(data),
                    original_filename=filename,
                    mime_type=content_type or (mimetypes.guess_type(filename)[0] or ""),
                    topic=str(fields.get("topic") or "").strip(),
                    industry=str(fields.get("industry") or "").strip(),
                )
                self._json({"success": True, "asset": _public_asset(asset)})
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── CAMPAIGN (MEDIA PLAN) ───────────────────────────────────────────
        elif p == "/campaign/preflight":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    plan, _days = _ensure_campaign_shape(auto)
                    if not bool(plan.get("enabled", False)):
                        self._json(
                            {
                                "error": "media_plan is disabled for this workflow. Enable campaign first, then run preflight.",
                            },
                            400,
                        )
                        return
                    report = _run_media_preflight(
                        auto,
                        topic_seed=str(payload.get("topic_seed") or "").strip(),
                        industry=str(payload.get("industry") or "").strip(),
                    )
                    fp.write_text(json.dumps(wf, indent=2))
                self._json({"success": True, "workflow_name": name, "report": report}, no_cache=True)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/campaign/create":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                with _WORKFLOW_IO_LOCK:
                    if fp.exists():
                        wf = json.loads(fp.read_text())
                    else:
                        wf = {
                            "workflow_name": name,
                            "steps": [],
                            "total_steps": 0,
                            "taught_at": datetime.datetime.utcnow().isoformat(),
                        }
                    auto = _ensure_workflow_automation_shape(wf)
                    current_plan = dict(auto.get("media_plan") or {})
                    incoming_plan = payload.get("media_plan")
                    if isinstance(incoming_plan, dict):
                        if str(incoming_plan.get("video_source") or "").strip().lower() == "ai":
                            self._json({"error": "AI video is disabled in this release. Use uploaded videos."}, 400)
                            return
                        current_plan.update(incoming_plan)
                    current_plan["enabled"] = True
                    for k in (
                        "enabled",
                        "campaign_length_days",
                        "mode",
                        "review_required",
                        "image_source",
                        "video_source",
                        "batch_size",
                        "platforms",
                        "topic_seed",
                        "industry",
                    ):
                        if k in payload:
                            current_plan[k] = payload.get(k)
                    auto["media_plan"] = _normalize_media_plan(current_plan)
                    plan, days = _ensure_campaign_shape(auto)
                    if str(plan.get("topic_seed") or "").strip():
                        auto["topic_seed"] = str(plan.get("topic_seed") or "").strip()
                    seed_topics = payload.get("topics")
                    if isinstance(seed_topics, list):
                        for i, t in enumerate(seed_topics[: len(days)]):
                            days[i]["topic"] = str(t or "").strip()
                    for d in days:
                        if not d.get("caption") and d.get("topic"):
                            d["caption"] = f"{d['topic']}\n\n#industry #dailyupdate"
                    fp.write_text(json.dumps(wf, indent=2))
                self._json(
                    {"success": True, "workflow_name": name, "media_plan": plan, "campaign_days": days},
                    no_cache=True,
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        elif p == "/campaign/assign-uploaded":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                assigns = payload.get("assignments")
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                if not isinstance(assigns, list) or not assigns:
                    self._json({"error": "assignments list required"}, 400)
                    return
                _idx, by_id = _campaign_asset_maps()
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    plan, days = _ensure_campaign_shape(auto)
                    by_day = {int(d.get("day_index") or 0): d for d in days}
                    for item in assigns:
                        if not isinstance(item, dict):
                            self._json({"error": "each assignment must be an object"}, 400)
                            return
                        try:
                            di = int(item.get("day_index"))
                        except Exception:
                            self._json({"error": "assignment.day_index must be an integer"}, 400)
                            return
                        day = by_day.get(di)
                        if not day:
                            self._json({"error": f"day_index out of range: {di}"}, 400)
                            return
                        media_type = str(item.get("media_type") or day.get("media_type") or "image").strip().lower()
                        if media_type not in ("none", "image", "video", "mixed"):
                            self._json({"error": f"day {di}: media_type must be none|image|video|mixed"}, 400)
                            return
                        day["media_type"] = media_type
                        if "topic" in item:
                            day["topic"] = str(item.get("topic") or "").strip()
                        if "caption" in item:
                            day["caption"] = str(item.get("caption") or "").strip()
                        if "image_asset_id" in item:
                            if plan.get("image_source") != "uploaded":
                                self._json({"error": f"day {di}: image_source is not uploaded"}, 400)
                                return
                            image_id = str(item.get("image_asset_id") or "").strip()
                            if image_id:
                                asset = by_id.get(image_id)
                                if not asset or str(asset.get("asset_type") or "") != "image":
                                    self._json({"error": f"day {di}: invalid uploaded image_asset_id"}, 400)
                                    return
                                if str(asset.get("source") or "") != "uploaded":
                                    self._json({"error": f"day {di}: image asset is not uploaded source"}, 400)
                                    return
                            day["image_asset_id"] = image_id
                            day["source_meta"]["image"] = "uploaded" if image_id else ""
                        if "video_asset_id" in item:
                            if plan.get("video_source") != "uploaded":
                                self._json({"error": f"day {di}: video_source is not uploaded"}, 400)
                                return
                            video_id = str(item.get("video_asset_id") or "").strip()
                            if video_id:
                                asset = by_id.get(video_id)
                                if not asset or str(asset.get("asset_type") or "") != "video":
                                    self._json({"error": f"day {di}: invalid uploaded video_asset_id"}, 400)
                                    return
                                if str(asset.get("source") or "") != "uploaded":
                                    self._json({"error": f"day {di}: video asset is not uploaded source"}, 400)
                                    return
                            day["video_asset_id"] = video_id
                            day["source_meta"]["video"] = "uploaded" if video_id else ""
                        if bool(plan.get("review_required", True)):
                            day["status"] = "pending_review"
                            day["review"]["status"] = "pending"
                            day["review"]["reviewed_at"] = ""
                        else:
                            day["status"] = "approved"
                            day["review"]["status"] = "approved"
                            day["review"]["reviewed_at"] = _now_utc_z()
                    fp.write_text(json.dumps(wf, indent=2))
                self._json({"success": True, "workflow_name": name, "campaign_days": days}, no_cache=True)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        elif p == "/campaign/review":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                reviews = payload.get("reviews")
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                if not isinstance(reviews, list) or not reviews:
                    self._json({"error": "reviews list required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    _plan, days = _ensure_campaign_shape(auto)
                    by_day = {int(d.get("day_index") or 0): d for d in days}
                    for item in reviews:
                        if not isinstance(item, dict):
                            self._json({"error": "each review item must be an object"}, 400)
                            return
                        try:
                            di = int(item.get("day_index"))
                        except Exception:
                            self._json({"error": "review.day_index must be an integer"}, 400)
                            return
                        day = by_day.get(di)
                        if not day:
                            self._json({"error": f"day_index out of range: {di}"}, 400)
                            return
                        status = str(item.get("status") or "").strip().lower()
                        if not status and "approved" in item:
                            status = "approved" if bool(item.get("approved")) else "rejected"
                        if status not in ("approved", "rejected", "pending"):
                            self._json({"error": f"day {di}: review status must be approved|rejected|pending"}, 400)
                            return
                        notes = str(item.get("notes") or "").strip()
                        day["review"]["status"] = status
                        day["review"]["notes"] = notes
                        day["review"]["reviewed_at"] = _now_utc_z() if status in ("approved", "rejected") else ""
                        if status == "approved":
                            day["status"] = "approved"
                        elif status == "rejected":
                            day["status"] = "pending_review"
                        else:
                            day["status"] = "pending_review"
                    fp.write_text(json.dumps(wf, indent=2))
                self._json({"success": True, "workflow_name": name, "campaign_days": days}, no_cache=True)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        elif p == "/campaign/validate":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    plan, days = _ensure_campaign_shape(auto)
                    errors = _validate_campaign(auto)
                    fp.write_text(json.dumps(wf, indent=2))
                self._json(
                    {
                        "workflow_name": name,
                        "valid": len(errors) == 0,
                        "errors": errors,
                        "media_plan": plan,
                        "campaign_days": days,
                    },
                    no_cache=True,
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        elif p == "/campaign/generate-batch":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    plan, days = _ensure_campaign_shape(auto)
                    if not bool(plan.get("enabled", False)):
                        self._json({"error": "campaign is not enabled for this workflow"}, 400)
                        return
                    if plan.get("mode") != "ai_batch_7":
                        self._json({"error": "campaign mode must be ai_batch_7"}, 400)
                        return
                    try:
                        count = int(payload.get("count", plan.get("batch_size", 7)))
                    except Exception:
                        self._json({"error": "count must be an integer"}, 400)
                        return
                    count = max(1, min(count, 30))
                    targets = _next_unfilled_days(days, count)
                    seed = str(payload.get("topic_seed") or plan.get("topic_seed") or auto.get("topic_seed") or "").strip()
                    if not seed:
                        self._json({"error": "topic_seed required for ai batch generation"}, 400)
                        return
                    existing = [str(d.get("topic") or "").strip() for d in days if str(d.get("topic") or "").strip()]
                    topics = _generate_topics_from_seed(seed, existing, count=len(targets))
                    updated: list[dict] = []
                    for i, day in enumerate(targets):
                        topic = topics[i] if i < len(topics) else f"{seed} — day {day.get('day_index')}"
                        day["topic"] = topic
                        _generate_campaign_day_content(auto, day, topic_seed=seed)
                        updated.append(
                            {
                                "day_index": day["day_index"],
                                "topic": day["topic"],
                                "status": day["status"],
                                "image_asset_id": day.get("image_asset_id", ""),
                                "video_asset_id": day.get("video_asset_id", ""),
                            }
                        )
                    fp.write_text(json.dumps(wf, indent=2))
                self._json(
                    {
                        "success": True,
                        "workflow_name": name,
                        "generated_count": len(updated),
                        "updated_days": updated,
                        "note": "AI image assets are generated when configured. Video assets remain uploaded-only in this release.",
                    },
                    no_cache=True,
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        elif p == "/campaign/generate-day":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                name = str(payload.get("workflow_name") or "").strip()
                if not name:
                    self._json({"error": "workflow_name required"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    auto = _ensure_workflow_automation_shape(wf)
                    plan, days = _ensure_campaign_shape(auto)
                    if not bool(plan.get("enabled", False)):
                        self._json({"error": "campaign is not enabled for this workflow"}, 400)
                        return
                    if plan.get("mode") != "ai_daily_auto":
                        self._json({"error": "campaign mode must be ai_daily_auto"}, 400)
                        return
                    day_idx_raw = payload.get("day_index")
                    target = None
                    if day_idx_raw is not None:
                        try:
                            day_idx = int(day_idx_raw)
                        except Exception:
                            self._json({"error": "day_index must be an integer"}, 400)
                            return
                        if 1 <= day_idx <= len(days):
                            target = days[day_idx - 1]
                    if target is None:
                        for d in days:
                            if str(d.get("status") or "") not in ("posted", "scheduled"):
                                target = d
                                break
                    if target is None:
                        self._json({"error": "no remaining day to generate"}, 400)
                        return
                    seed = str(payload.get("topic_seed") or plan.get("topic_seed") or auto.get("topic_seed") or "").strip()
                    _generate_campaign_day_content(auto, target, topic_seed=seed)
                    fp.write_text(json.dumps(wf, indent=2))
                self._json(
                    {
                        "success": True,
                        "workflow_name": name,
                        "day": {
                            "day_index": target["day_index"],
                            "topic": target["topic"],
                            "status": target["status"],
                            "image_asset_id": target.get("image_asset_id", ""),
                            "video_asset_id": target.get("video_asset_id", ""),
                        },
                        "note": "AI image assets are generated when configured. Video assets remain uploaded-only in this release.",
                    },
                    no_cache=True,
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── AUTOMATION SETTINGS ──────────────────────────────────────────────
        elif p.startswith("/workflow/") and p.endswith("/automation"):
            try:
                name = unquote(p[len("/workflow/") : -len("/automation")]).strip("/")
                if not name:
                    self._json({"error": "workflow name required"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{name}.json"
                if not fp.exists():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text())
                    was_enabled = bool((wf.get("automation") or {}).get("enabled", False))
                    auto = _ensure_workflow_automation_shape(wf)
                    def _as_int(v: Any, default: int) -> int:
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            return default
                    prev_mode = str(auto.get("mode", "daily"))
                    prev_interval_minutes = _as_int(auto.get("interval_minutes", 5), 5)
                    prev_interval_hours = _as_int(auto.get("interval_hours", 24), 24)
                    prev_daily_time = str(auto.get("daily_time", "21:00") or "21:00")
                    prev_weekly_times = dict(auto.get("weekly_times") or {})
                    mode = str(payload.get("mode", auto.get("mode", "daily"))).strip().lower()
                    if mode not in ("minutes", "hourly", "daily", "weekly"):
                        self._json({"error": "mode must be minutes, hourly, daily, or weekly"}, 400)
                        return
                    auto["mode"] = mode
                    try:
                        im = int(payload.get("interval_minutes", auto.get("interval_minutes", 5)))
                    except Exception:
                        self._json({"error": "interval_minutes must be an integer"}, 400)
                        return
                    auto["interval_minutes"] = max(1, min(im, 1440))
                    try:
                        ih = int(payload.get("interval_hours", auto.get("interval_hours", 24)))
                    except Exception:
                        self._json({"error": "interval_hours must be an integer"}, 400)
                        return
                    auto["interval_hours"] = max(1, min(ih, 168))
                    daily_time = str(payload.get("daily_time", auto.get("daily_time", "21:00"))).strip()
                    if daily_time and not _valid_hhmm(daily_time):
                        self._json({"error": "daily_time must be HH:MM"}, 400)
                        return
                    auto["daily_time"] = daily_time or "21:00"
                    weekly_raw = payload.get("weekly_times", auto.get("weekly_times", {}))
                    if not isinstance(weekly_raw, dict):
                        self._json({"error": "weekly_times must be an object"}, 400)
                        return
                    weekly_times = {"mon": "", "tue": "", "wed": "", "thu": "", "fri": "", "sat": "", "sun": ""}
                    for day in weekly_times.keys():
                        raw = str(weekly_raw.get(day, "") or "").strip()
                        if raw and not _valid_hhmm(raw):
                            self._json({"error": f"weekly_times[{day}] must be HH:MM or empty"}, 400)
                            return
                        weekly_times[day] = raw
                    auto["weekly_times"] = weekly_times
                    if "topic_seed" in payload:
                        auto["topic_seed"] = str(payload.get("topic_seed") or "").strip()
                    media_plan_in = payload.get("media_plan")
                    if isinstance(media_plan_in, dict):
                        if str(media_plan_in.get("video_source") or "").strip().lower() == "ai":
                            self._json({"error": "AI video is disabled in this release. Use uploaded videos."}, 400)
                            return
                        current_mp = dict(auto.get("media_plan") or {})
                        current_mp.update(media_plan_in)
                        auto["media_plan"] = _normalize_media_plan(current_mp)
                        _ensure_campaign_shape(auto)
                    if "mode" in payload:
                        auto["topics_pending"] = []
                        auto["topic_seed"] = ""
                        auto["auto_generate_topics"] = False
                        mp = dict(auto.get("media_plan") or {})
                        mp["enabled"] = False
                        auto["media_plan"] = _normalize_media_plan(mp)
                        auto["auto_last_error"] = ""
                    if bool(payload.get("reset_completed", False)):
                        auto["topics_completed"] = []
                    if bool(payload.get("reset_scheduler_run_count", False)):
                        auto["scheduler_run_count"] = 0
                    whatsapp_number_raw = str(
                        payload.get("whatsapp_number", auto.get("whatsapp_number", "")) or ""
                    ).strip()
                    whatsapp_number = _normalize_whatsapp_phone(whatsapp_number_raw)
                    if whatsapp_number_raw and not whatsapp_number:
                        self._json({"error": "whatsapp_number must contain digits"}, 400)
                        return
                    auto["whatsapp_number"] = whatsapp_number
                    enabled = bool(payload.get("enabled", auto.get("enabled", False)))
                    auto["enabled"] = enabled
                    schedule_keys = {
                        "mode",
                        "interval_minutes",
                        "interval_hours",
                        "daily_time",
                        "weekly_times",
                    }
                    schedule_payload_present = any(k in payload for k in schedule_keys)
                    schedule_changed = (
                        str(auto.get("mode", "daily")) != prev_mode
                        or _as_int(auto.get("interval_minutes", 5), 5) != prev_interval_minutes
                        or _as_int(auto.get("interval_hours", 24), 24) != prev_interval_hours
                        or str(auto.get("daily_time", "21:00") or "21:00") != prev_daily_time
                        or dict(auto.get("weekly_times") or {}) != prev_weekly_times
                    )
                    if auto["enabled"] and not was_enabled:
                        # Reliability default: do NOT run immediately on enable. Schedule the next future run.
                        auto["next_run_at"] = _compute_next_run_local(auto, datetime.datetime.now())
                        auto["in_progress_topic"] = ""
                        auto["in_progress_day"] = 0
                        auto["in_progress_started_at"] = ""
                        _, days_en = _ensure_campaign_shape(auto)
                        for d in days_en:
                            if str(d.get("status") or "") == "scheduled":
                                d["status"] = "approved"
                    elif auto["enabled"] and schedule_payload_present and schedule_changed:
                        # If user edits the schedule while already enabled, honor the new timing now.
                        auto["next_run_at"] = _compute_next_run_local(auto, datetime.datetime.now())
                    elif not auto["enabled"]:
                        auto["next_run_at"] = ""
                        auto["in_progress_topic"] = ""
                        auto["in_progress_day"] = 0
                        auto["in_progress_started_at"] = ""
                    if not auto.get("auto_last_error"):
                        auto["auto_last_error"] = ""
                    fp.write_text(json.dumps(wf, indent=2))
                self._json({"success": True, "workflow_name": name, "automation": auto})
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)
        # ── JOIN WORKFLOW (append workflow into another) ────────────────────
        elif p.startswith("/workflow/") and p.endswith("/join"):
            try:
                target_name = unquote(p[len("/workflow/") : -len("/join")]).strip("/")
                payload = json.loads(body.decode("utf-8") or "{}")
                source_name = str(payload.get("source_workflow") or "").strip()
                if not target_name:
                    self._json({"error": "target workflow required in URL"}, 400); return
                if not source_name:
                    self._json({"error": "source_workflow required"}, 400); return
                if source_name == target_name:
                    self._json({"error": "source_workflow must be different from target workflow"}, 400); return

                source_path = WORKFLOWS_DIR / f"{source_name}.json"
                if not source_path.exists():
                    self._json({"error": f"workflow '{source_name}' not found"}, 404); return

                with _WORKFLOW_IO_LOCK:
                    source_wf = json.loads(source_path.read_text())
                    source_steps = source_wf.get("steps", [])
                    if not isinstance(source_steps, list):
                        source_steps = []

                    target_path = WORKFLOWS_DIR / f"{target_name}.json"
                    if target_path.exists():
                        target_wf = json.loads(target_path.read_text())
                    else:
                        target_wf = {
                            "workflow_name": target_name,
                            "steps": [],
                            "taught_at": datetime.datetime.utcnow().isoformat(),
                        }
                    if not isinstance(target_wf.get("steps"), list):
                        target_wf["steps"] = []

                    start = len(target_wf["steps"])
                    for idx, raw in enumerate(source_steps, 1):
                        cloned = dict(raw) if isinstance(raw, dict) else {}
                        cloned["step"] = start + idx
                        target_wf["steps"].append(cloned)

                    target_wf["total_steps"] = len(target_wf["steps"])
                    target_path.write_text(json.dumps(target_wf, indent=2))
                self._json(
                    {
                        "success": True,
                        "joined_steps": len(source_steps),
                        "total_steps": target_wf["total_steps"],
                    }
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── REORDER STEPS (Step Builder drag-and-drop; renumbers step field 1..n) ─
        elif p.startswith("/workflow/") and p.endswith("/reorder"):
            try:
                wf_name = unquote(p[len("/workflow/") : -len("/reorder")]).strip("/")
                if not wf_name:
                    self._json({"error": "workflow name required in URL"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                order = payload.get("order") if isinstance(payload, dict) else None
                if not isinstance(order, list) or not order:
                    self._json({"error": "order must be a non-empty list of step numbers"}, 400)
                    return
                fp = WORKFLOWS_DIR / f"{wf_name}.json"
                if not fp.is_file():
                    self._json({"error": "workflow not found"}, 404)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(fp.read_text(encoding="utf-8"))
                    steps = wf.get("steps")
                    if not isinstance(steps, list):
                        self._json({"error": "invalid workflow steps"}, 400)
                        return
                    by_num: dict[int, dict[str, Any]] = {}
                    for s in steps:
                        if not isinstance(s, dict):
                            continue
                        try:
                            sn = int(s.get("step"))
                        except (TypeError, ValueError):
                            continue
                        by_num[sn] = s
                    new_steps: list[dict[str, Any]] = []
                    seen: set[int] = set()
                    for raw_n in order:
                        try:
                            n = int(raw_n)
                        except (TypeError, ValueError):
                            self._json({"error": f"invalid step number in order: {raw_n!r}"}, 400)
                            return
                        if n in seen:
                            self._json({"error": f"duplicate step {n} in order"}, 400)
                            return
                        seen.add(n)
                        if n not in by_num:
                            self._json({"error": f"step {n} not in workflow"}, 400)
                            return
                        new_steps.append(by_num[n])
                    if len(new_steps) != len(by_num):
                        self._json({"error": "order must include every step exactly once"}, 400)
                        return
                    for i, st in enumerate(new_steps, 1):
                        st["step"] = i
                    wf["steps"] = new_steps
                    wf["total_steps"] = len(new_steps)
                    fp.write_text(json.dumps(wf, indent=2), encoding="utf-8")
                self._json({"success": True, "total_steps": len(new_steps)})
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── RENAME WORKFLOW (new filename; steps and automation data unchanged) ─
        elif p.startswith("/workflow/") and p.endswith("/rename"):
            try:
                old_name = unquote(p[len("/workflow/") : -len("/rename")]).strip("/")
                if not old_name:
                    self._json({"error": "workflow name required in URL"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid JSON body"}, 400)
                    return
                new_name = _normalize_clone_target_name(str(payload.get("new_name") or ""))
                if not new_name:
                    self._json(
                        {
                            "error": "new_name required (non-empty; no / \\ or <>:\"|?* characters)",
                        },
                        400,
                    )
                    return
                if new_name == old_name:
                    self._json({"error": "new_name must differ from the current workflow name"}, 400)
                    return
                old_path = WORKFLOWS_DIR / f"{old_name}.json"
                if not old_path.is_file():
                    self._json({"error": f"workflow '{old_name}' not found"}, 404)
                    return
                new_path = WORKFLOWS_DIR / f"{new_name}.json"
                if new_path.exists():
                    self._json({"error": f"a workflow named '{new_name}' already exists"}, 409)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf: Any = json.loads(old_path.read_text())
                    if not isinstance(wf, dict):
                        self._json({"error": "invalid workflow file"}, 500)
                        return
                    # Only metadata that tracks the on-disk name; all steps and nested automation are untouched.
                    wf["workflow_name"] = new_name
                    new_path.write_text(json.dumps(wf, indent=2))
                    try:
                        old_path.unlink()
                    except OSError as oe:
                        try:
                            if new_path.is_file():
                                new_path.unlink()
                        except OSError:
                            pass
                        self._json({"error": f"could not complete rename: {oe}"}, 500)
                        return
                self._json({"success": True, "workflow_name": new_name, "previous": old_name})
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── CLONE WORKFLOW (copy JSON to a new file; original unchanged) ───
        elif p.startswith("/workflow/") and p.endswith("/clone"):
            try:
                source_name = unquote(p[len("/workflow/") : -len("/clone")]).strip("/")
                if not source_name:
                    self._json({"error": "source workflow name required in URL"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid JSON body"}, 400)
                    return
                new_name = _normalize_clone_target_name(str(payload.get("new_name") or ""))
                if not new_name:
                    self._json(
                        {"error": "new_name required (non-empty; no / \\ or <>:\"|?* characters)"},
                        400,
                    )
                    return
                if new_name == source_name:
                    self._json({"error": "new_name must differ from the source workflow name"}, 400)
                    return
                src_path = WORKFLOWS_DIR / f"{source_name}.json"
                if not src_path.exists():
                    self._json({"error": f"workflow '{source_name}' not found"}, 404)
                    return
                dst_path = WORKFLOWS_DIR / f"{new_name}.json"
                if dst_path.exists():
                    self._json({"error": f"a workflow named '{new_name}' already exists"}, 409)
                    return
                with _WORKFLOW_IO_LOCK:
                    wf = json.loads(src_path.read_text())
                    if not isinstance(wf, dict):
                        self._json({"error": "invalid workflow file"}, 500)
                        return
                    copy_wf: dict[str, Any] = json.loads(json.dumps(wf))
                    copy_wf["workflow_name"] = new_name
                    steps = copy_wf.get("steps")
                    if not isinstance(steps, list):
                        copy_wf["steps"] = []
                    copy_wf["total_steps"] = len(copy_wf["steps"])
                    copy_wf["cloned_from"] = source_name
                    copy_wf["cloned_at"] = _now_utc_z()
                    dst_path.write_text(json.dumps(copy_wf, indent=2))
                self._json({"success": True, "workflow_name": new_name, "source": source_name})
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── STOP CURRENT RUN ──────────────────────────────────────────────────
        elif p == "/run/stop":
            _RUN_STOP_EVENT.set()
            self._json({"success": True, "status": "stop_requested"})

        elif p == "/consumer/prompts":
            try:
                if not locked_consumer_ui:
                    self._json({"error": "not available"}, 404)
                    return
                if "application/json" not in (ct or "").lower():
                    self._json({"error": "Content-Type must be application/json"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                prompts_in = payload.get("prompts")
                if not isinstance(prompts_in, dict):
                    self._json({"error": "prompts must be an object: { workflow_name: prompt }"}, 400)
                    return
                fpb = _bundle_path(locked_slug)
                if not fpb.exists():
                    self._json({"error": "bundle not found"}, 404)
                    return
                with _BUNDLE_IO_LOCK:
                    bundle = _normalize_bundle(json.loads(fpb.read_text()))
                allowed_children = {
                    str(x or "").strip()
                    for x in (bundle.get("children") or [])
                    if str(x or "").strip()
                }
                updated = []
                with _WORKFLOW_IO_LOCK:
                    for wf_name_raw, prompt_raw in prompts_in.items():
                        wf_name = str(wf_name_raw or "").strip()
                        if not wf_name or wf_name not in allowed_children:
                            continue
                        prompt = str(prompt_raw or "").strip()
                        if len(prompt) > 4000:
                            self._json({"error": f"prompt too long for {wf_name} (max 4000 chars)"}, 400)
                            return
                        fp = WORKFLOWS_DIR / f"{wf_name}.json"
                        if not fp.exists():
                            continue
                        wf = json.loads(fp.read_text())
                        if not isinstance(wf, dict):
                            continue
                        auto = _ensure_workflow_automation_shape(wf)
                        auto["topic_seed"] = prompt
                        auto["auto_generate_topics"] = True
                        completed = _completed_topic_strings(auto)
                        auto["topics_pending"] = _generate_topics_from_seed(prompt, completed, count=30)[:30]
                        auto["in_progress_topic"] = ""
                        auto["in_progress_day"] = 0
                        auto["in_progress_started_at"] = ""
                        auto["auto_last_error"] = ""
                        # Ensure campaign media plan uses same seed (keeps internal consistency).
                        mp = dict(auto.get("media_plan") or {})
                        mp["topic_seed"] = prompt
                        auto["media_plan"] = _normalize_media_plan(mp)
                        wf["automation"] = auto
                        fp.write_text(json.dumps(wf, indent=2))
                        updated.append(wf_name)
                self._json({"success": True, "updated": updated}, no_cache=True)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/consumer/topics/regenerate":
            try:
                if not locked_consumer_ui:
                    self._json({"error": "not available"}, 404)
                    return
                payload = {}
                if "application/json" in (ct or "").lower() and body:
                    try:
                        payload = json.loads(body.decode("utf-8") or "{}")
                    except Exception:
                        payload = {}
                count_raw = payload.get("count", 30) if isinstance(payload, dict) else 30
                try:
                    count = int(count_raw)
                except Exception:
                    count = 30
                count = max(5, min(count, 60))

                fpb = _bundle_path(locked_slug)
                if not fpb.exists():
                    self._json({"error": "bundle not found"}, 404)
                    return
                with _BUNDLE_IO_LOCK:
                    bundle = _normalize_bundle(json.loads(fpb.read_text()))
                children = [
                    str(x or "").strip()
                    for x in (bundle.get("children") or [])
                    if str(x or "").strip()
                ]
                updated: list[str] = []
                skipped: dict[str, str] = {}
                with _WORKFLOW_IO_LOCK:
                    for wf_name in children:
                        fp = WORKFLOWS_DIR / f"{wf_name}.json"
                        if not fp.exists():
                            skipped[wf_name] = "workflow file missing"
                            continue
                        wf = json.loads(fp.read_text())
                        if not isinstance(wf, dict):
                            skipped[wf_name] = "invalid workflow JSON"
                            continue
                        auto = _ensure_workflow_automation_shape(wf)
                        seed = str(auto.get("topic_seed") or "").strip()
                        if not seed:
                            skipped[wf_name] = "prompt/topic seed is empty"
                            continue
                        completed = _completed_topic_strings(auto)
                        generated = _generate_topics_from_seed(seed, completed, count=count)
                        if not generated:
                            skipped[wf_name] = "AI returned no topics"
                            continue
                        auto["auto_generate_topics"] = True
                        auto["topics_pending"] = generated[:30]
                        auto["in_progress_topic"] = ""
                        auto["in_progress_day"] = 0
                        auto["in_progress_started_at"] = ""
                        auto["auto_last_error"] = ""
                        wf["automation"] = auto
                        fp.write_text(json.dumps(wf, indent=2))
                        updated.append(wf_name)
                self._json(
                    {
                        "success": True,
                        "updated_workflows": len(updated),
                        "updated": updated,
                        "skipped": skipped,
                        "count": count,
                    },
                    no_cache=True,
                )
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/best-ai/run":
            try:
                if "application/json" not in (ct or "").lower():
                    self._json({"error": "Content-Type must be application/json"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                plat = str(payload.get("platform") or "").strip().lower()
                query = str(payload.get("query") or "").strip()
                run_id = str(payload.get("run_id") or "").strip()
                if plat not in ("chatgpt", "gemini", "claude"):
                    self._json({"error": "platform must be chatgpt, gemini, or claude"}, 400)
                    return
                if not query or len(query) > 20000:
                    self._json({"error": "query required (1–20000 chars)"}, 400)
                    return
                job_id = secrets.token_hex(12)
                with _BEST_AI_JOBS_LOCK:
                    _BEST_AI_JOBS[job_id] = {
                        "status": "running",
                        "platform": plat,
                        "started_at": _utc_now_z(),
                        "finished_at": "",
                    }
                threading.Thread(
                    target=_best_ai_run_worker,
                    args=(job_id, plat, query, run_id),
                    daemon=True,
                    name="best-ai-run",
                ).start()
                self._json({"job_id": job_id, "status": "running"})
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/best-ai/synthesize":
            try:
                if "application/json" not in (ct or "").lower():
                    self._json({"error": "Content-Type must be application/json"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                query = str(payload.get("query") or "").strip()
                raw_resp = payload.get("responses")
                responses: dict[str, str] = {}
                if isinstance(raw_resp, dict):
                    for k, v in raw_resp.items():
                        responses[str(k)] = str(v) if v is not None else ""
                out = _best_ai_synthesize_openai(query, responses)
                self._json({"success": True, **out})
            except ValueError as ve:
                self._json({"error": str(ve)}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/best-ai/ui-bridge":
            try:
                if "application/json" not in (ct or "").lower():
                    self._json({"error": "Content-Type must be application/json"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                if "topic" not in payload:
                    self._json({"error": "topic field required"}, 400)
                    return

                def _topic_mut(d: dict[str, Any]) -> None:
                    d["topic"] = str(payload.get("topic") or "").strip()

                _best_ai_bridge_mutate(_topic_mut)
                self._json({"success": True, **_best_ai_bridge_get()})
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p == "/permissions/trial":
            checks: dict[str, Any] = {"pyautogui": False, "screenshot": False, "keyboard": False, "error": ""}
            try:
                import pyautogui  # type: ignore

                checks["pyautogui"] = True
                try:
                    x, y = pyautogui.position()
                    pyautogui.moveTo(x, y, duration=0.01)
                    checks["keyboard"] = True
                except Exception:
                    checks["keyboard"] = False
                try:
                    cap_path = SCREENSHOTS_DIR / "_permissions_trial.png"
                    _capture_screen_png(cap_path)
                    checks["screenshot"] = bool(cap_path.exists() and cap_path.stat().st_size > 0)
                except Exception:
                    checks["screenshot"] = False
            except Exception as e:
                checks["error"] = str(e)
            ok = bool(checks.get("pyautogui") and checks.get("keyboard") and checks.get("screenshot"))
            self._json({"success": ok, "checks": checks}, no_cache=True)

        # ── EXPORT DESKTOP (single-AR consumer build) ─────────────────────────
        elif p == "/export-desktop-ar":
            try:
                if not _desktop_export_allowed():
                    self._json({"error": "desktop export disabled (TRAINER_ALLOW_DESKTOP_EXPORT=0)"}, 403)
                    return
                if "application/json" not in (ct or "").lower():
                    self._json({"error": "Content-Type must be application/json"}, 400)
                    return
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                slug = _bundle_slug(str(payload.get("bundle_slug") or payload.get("slug") or "").strip())
                if not slug:
                    self._json({"error": "bundle_slug required"}, 400)
                    return
                if not _bundle_path(slug).is_file():
                    self._json({"error": "bundle not found"}, 404)
                    return
                platform_target = str(payload.get("platform") or "").strip().lower()
                if platform_target not in ("mac", "win"):
                    self._json({"error": "platform must be mac or win"}, 400)
                    return
                cap = _desktop_export_capabilities()
                if platform_target == "mac" and not cap.get("can_build_mac"):
                    self._json(
                        {
                            "error": "Cannot build for macOS here — run Trainer on a Mac with PyInstaller installed.",
                        },
                        400,
                    )
                    return
                if platform_target == "win" and not cap.get("can_build_win"):
                    self._json(
                        {
                            "error": "Cannot build for Windows here — run Trainer on Windows with PyInstaller installed.",
                        },
                        400,
                    )
                    return
                embed_keys = bool(payload.get("embed_keys"))
                job_id = secrets.token_hex(16)
                with _DESKTOP_EXPORT_JOBS_LOCK:
                    _DESKTOP_EXPORT_JOBS[job_id] = {
                        "status": "running",
                        "bundle_slug": slug,
                        "platform": platform_target,
                        "embed_keys": embed_keys,
                        "log": "",
                        "error": "",
                        "artifact": "",
                        "started_at": _utc_now_z(),
                        "finished_at": "",
                    }
                agency = BASE_DIR.resolve()
                threading.Thread(
                    target=_desktop_export_worker,
                    args=(job_id, agency, slug, platform_target, embed_keys),
                    daemon=True,
                    name="desktop-export",
                ).start()
                self._json({"job_id": job_id, "status": "running"}, no_cache=True)
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── BUNDLE SAVE/UPDATE ────────────────────────────────────────────────
        elif p == "/bundle":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                raw_slug = str(payload.get("slug") or payload.get("display_name") or "").strip()
                slug = _bundle_slug(raw_slug)
                if not slug:
                    self._json({"error": "slug (or display_name) required"}, 400)
                    return
                if locked_consumer_ui:
                    if slug != locked_slug:
                        self._json({"error": "bundle not found"}, 404)
                        return
                    sch = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
                    daily_time = str((sch or {}).get("daily_time") or "").strip()
                    if daily_time and not _valid_hhmm(daily_time):
                        self._json({"error": "daily_time must be HH:MM"}, 400)
                        return
                    if not daily_time:
                        daily_time = "21:00"
                    with _BUNDLE_IO_LOCK:
                        fp = _bundle_path(slug)
                        if not fp.exists():
                            self._json({"error": "bundle not found"}, 404)
                            return
                        current = _normalize_bundle(json.loads(fp.read_text()))
                        current.setdefault("schedule", {})
                        current["schedule"] = _normalize_bundle_schedule(
                            {"enabled": True, "mode": "daily", "daily_time": daily_time}
                        )
                        current = _normalize_bundle(current)
                        current["next_run_at"] = _bundle_next_run_local(current, datetime.datetime.now())
                        fp.write_text(json.dumps(current, indent=2))
                    self._json({"success": True, "bundle": current})
                    return
                schedule_in_payload = "schedule" in payload and isinstance(payload.get("schedule"), dict)
                with _BUNDLE_IO_LOCK:
                    fp = _bundle_path(slug)
                    if fp.exists():
                        current = _normalize_bundle(json.loads(fp.read_text()))
                    else:
                        current = _normalize_bundle({"slug": slug, "display_name": slug, "children": [], "schedule": {}})
                    if "display_name" in payload:
                        current["display_name"] = str(payload.get("display_name") or slug).strip() or slug
                    if "children" in payload:
                        children: list[str] = []
                        for item in (payload.get("children") or []):
                            nm = str(item or "").strip()
                            if nm and nm not in children:
                                children.append(nm)
                        current["children"] = children
                    if "notify_same_for_all_flows" in payload:
                        current["notify_same_for_all_flows"] = bool(payload.get("notify_same_for_all_flows"))
                    if "notify_number" in payload:
                        current["notify_number"] = _normalize_whatsapp_phone(str(payload.get("notify_number") or ""))
                    if "notify_numbers_by_flow" in payload and isinstance(payload.get("notify_numbers_by_flow"), dict):
                        incoming_map = payload.get("notify_numbers_by_flow") or {}
                        cleaned_map = {}
                        for wf_name, num in incoming_map.items():
                            k = str(wf_name or "").strip()
                            if not k:
                                continue
                            nv = _normalize_whatsapp_phone(str(num or ""))
                            if nv:
                                cleaned_map[k] = nv
                        current["notify_numbers_by_flow"] = cleaned_map
                    if "notify_mode" in payload:
                        nm = str(payload.get("notify_mode") or "").strip().lower()
                        if nm in ("one", "per_flow", "from_workflows"):
                            current["notify_mode"] = nm
                    if "schedule" in payload and isinstance(payload.get("schedule"), dict):
                        current["schedule"] = _normalize_bundle_schedule(payload.get("schedule") or {})
                    current = _normalize_bundle(current)
                    if bool(current.get("schedule", {}).get("enabled")):
                        if schedule_in_payload or not str(current.get("next_run_at") or "").strip():
                            current["next_run_at"] = _bundle_next_run_local(current, datetime.datetime.now())
                    else:
                        current["next_run_at"] = ""
                    fp.write_text(json.dumps(current, indent=2))
                self._json({"success": True, "bundle": current})
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        elif p.startswith("/bundle/") and p.endswith("/run"):
            try:
                slug = unquote(p[len("/bundle/") : -len("/run")]).strip("/")
                if not slug:
                    self._json({"error": "bundle slug required"}, 400)
                    return
                fp = _bundle_path(slug)
                if not fp.exists():
                    self._json({"error": "bundle not found"}, 404)
                    return
                with _BUNDLE_IO_LOCK:
                    bundle = _normalize_bundle(json.loads(fp.read_text()))
                if not bundle.get("children"):
                    self._json({"error": "bundle has no children"}, 400)
                    return
                if not _RUN_WORKFLOW_LOCK.acquire(blocking=False):
                    self._json({"error": "another workflow run is already in progress"}, 409)
                    return
                _RUN_STOP_EVENT.clear()
                try:
                    run_results: list[dict[str, Any]] = []
                    for child in bundle.get("children") or []:
                        if _stop_requested():
                            run_results.append(
                                {
                                    "child": child,
                                    "status": "stopped",
                                    "started_at": _utc_now_z(),
                                    "finished_at": _utc_now_z(),
                                    "error": "Stopped by user",
                                }
                            )
                            break
                        st = _utc_now_z()
                        child_steps = run_workflow(
                            child,
                            dry_run=False,
                            runtime_vars_seed={
                                "CURRENT_AR_BUNDLE_SLUG": str(bundle.get("slug") or ""),
                                "CURRENT_AR_BUNDLE_NAME": str(bundle.get("display_name") or bundle.get("slug") or ""),
                                "CURRENT_AR_FLOW_NAME": str(bundle.get("display_name") or bundle.get("slug") or ""),
                                "WHATSAPP_NOTIFY_PHONE": _bundle_notify_number_for_child(bundle, child),
                            },
                            run_mode="smart",
                            run_source="ar_bundle_manual",
                        )
                        child_errs = [s for s in child_steps if s.get("status") == "error"]
                        run_results.append(
                            {
                                "child": child,
                                "status": "error" if child_errs else "ok",
                                "started_at": st,
                                "finished_at": _utc_now_z(),
                                "error": child_errs[0].get("error", "") if child_errs else "",
                            }
                        )
                    with _BUNDLE_IO_LOCK:
                        bundle = _normalize_bundle(json.loads(fp.read_text()))
                        bundle["last_run_results"] = run_results[-200:]
                        bundle["last_run_at"] = _utc_now_z()
                        if bool(bundle.get("schedule", {}).get("enabled")):
                            bundle["next_run_at"] = _bundle_next_run_local(bundle, datetime.datetime.now())
                        fp.write_text(json.dumps(bundle, indent=2))
                finally:
                    _RUN_WORKFLOW_LOCK.release()
                self._json({"success": True, "bundle": bundle, "results": run_results})
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

        # ── RUN ──────────────────────────────────────────────────────────────
        elif p == "/run":
            name = ""
            dry_run = False
            mode = "smart"
            try:
                data    = json.loads(body)
                name    = data.get("workflow_name", "")
                dry_run = data.get("dry_run", False)
                mode    = str(data.get("mode", "smart") or "smart").strip().lower()
                if mode not in ("fast", "smart", "wra"):
                    mode = "smart"
                if not name:
                    self._json({"error": "workflow_name required"}, 400); return
                if not _RUN_WORKFLOW_LOCK.acquire(blocking=False):
                    self._json({"error": "another workflow run is already in progress"}, 409); return
                _RUN_STOP_EVENT.clear()
                try:
                    if mode == "wra":
                        results = run_wra_workflow(name, dry_run=bool(dry_run), run_source="manual_run")
                    else:
                        results = run_workflow(name, dry_run=dry_run, run_mode=mode, run_source="manual_run")
                    audit_path = save_run_audit(name, bool(dry_run), results)
                    if not _stop_requested():
                        _send_whatsapp_run_notification(
                            name,
                            source="manual_run",
                            dry_run=bool(dry_run),
                            mode=mode,
                            steps=results,
                            error="",
                        )
                    self._json(
                        {
                            "success": True,
                            "mode": mode,
                            "note": f"Executed in {mode.upper()} mode",
                            "steps": results,
                            "audit_file": audit_path.name,
                        }
                    )
                finally:
                    _RUN_WORKFLOW_LOCK.release()
            except FileNotFoundError as e:
                if name:
                    save_run_audit(name, bool(dry_run), [], error=str(e))
                    # Do not run WhatsApp notify — workflow file never loaded (avoids confusing Chrome-only runs).
                self._json({"error": str(e)}, 404)
            except PermissionError as e:
                traceback.print_exc()
                if name:
                    save_run_audit(name, bool(dry_run), [], error=str(e))
                self._json({"error": str(e)}, 403)
            except Exception as e:
                traceback.print_exc()
                if name:
                    save_run_audit(name, bool(dry_run), [], error=str(e))
                    if not _stop_requested():
                        _send_whatsapp_run_notification(
                            name,
                            source="manual_run",
                            dry_run=bool(dry_run),
                            mode=mode,
                            steps=[],
                            error=str(e),
                        )
                self._json({"error": str(e)}, 500)

        elif p == "/control/engine/start":
            _CONTROL_ENGINE_STOP_REQUESTED = False
            _control_append_engine_log("Start Engine (Control Center).")
            self._json({"success": True, "engine_ready": True})
        elif p == "/control/engine/stop":
            _CONTROL_ENGINE_STOP_REQUESTED = True
            _control_append_engine_log("Stop Engine (Control Center).")
            self._json({"success": True, "engine_ready": False})
        elif p == "/control/settings":
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            cur = _load_control_settings()
            incoming = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
            if isinstance(incoming, dict):
                for k, v in incoming.items():
                    if k in cur or k in _control_default_settings():
                        cur[k] = v
            _save_control_settings(cur)
            _apply_control_center_paths()
            self._json({"success": True, "settings": _load_control_settings()})
        elif p == "/wra/lucky/cancel":
            _LUCKY_CANCEL.set()
            self._json({"success": True, "cancel_requested": True})
        elif p == "/wra/lucky/run":
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            wf_name = str(payload.get("workflow_name") or "").strip()
            if not wf_name:
                self._json({"error": "workflow_name required"}, 400)
                return
            path = WORKFLOWS_DIR / f"{wf_name}.json"
            if not path.exists():
                self._json({"error": "workflow not found"}, 404)
                return
            with _LUCKY_JOB_LOCK:
                if _LUCKY_JOB_STATUS.get("running"):
                    self._json({"error": "Lucky run already in progress"}, 409)
                    return
                _LUCKY_CANCEL.clear()
                _LUCKY_JOB_STATUS.update({"running": True, "error": "", "report": None})

            def _lucky_thread() -> None:
                global _LAST_LUCKY_REPORT  # noqa: PLW0603
                try:
                    wf = json.loads(path.read_text(encoding="utf-8"))
                    from cusear.engine.lucky import Lucky as _Lucky
                    from cusear.engine.paths import WraPaths as _WraPaths

                    pr = _WraPaths(root=str(BASE_DIR))
                    rep = _Lucky(logs_dir=str(pr.lucky_logs_dir), stop_event=_LUCKY_CANCEL).run(wf).to_dict()
                    _LAST_LUCKY_REPORT = rep
                    with _LUCKY_JOB_LOCK:
                        _LUCKY_JOB_STATUS.update({"running": False, "error": "", "report": rep})
                except Exception as exc:
                    with _LUCKY_JOB_LOCK:
                        _LUCKY_JOB_STATUS.update({"running": False, "error": str(exc), "report": None})

            threading.Thread(target=_lucky_thread, daemon=True).start()
            self._json({"success": True, "started": True})
        elif p == "/export/workflow-encrypted":
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            wf_name = str(payload.get("workflow_name") or "").strip()
            if not wf_name:
                self._json({"error": "workflow_name required"}, 400)
                return
            fp = WORKFLOWS_DIR / f"{wf_name}.json"
            if not fp.is_file():
                self._json({"error": "workflow not found"}, 404)
                return
            try:
                from security.license import get_machine_id
                from security.workflow_crypto import encrypt_workflow

                wf_obj = json.loads(fp.read_text(encoding="utf-8"))
                blob = encrypt_workflow(wf_obj, get_machine_id())
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            safe_fn = re.sub(r"[^\w.\-]+", "_", wf_name).strip("._-") or "workflow"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_fn}.cusear.enc"')
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(blob)
        elif p == "/wra/rekky/start":
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            wf_name = str(payload.get("workflow_name") or "").strip()
            platform_name = str(payload.get("platform") or "").strip()
            url = str(payload.get("url") or "").strip()
            per_keypress = payload.get("per_keypress")
            if per_keypress is None:
                batch_tabs = bool(payload.get("batch_tabs", False))
            else:
                batch_tabs = not bool(per_keypress)
            if not wf_name or not platform_name or not url:
                self._json({"error": "workflow_name, platform, url are required"}, 400)
                return
            with _WRA_REKKY_LOCK:
                if _WRA_REKKY_STATUS.get("running"):
                    self._json({"error": "Rekky already running"}, 409)
                    return
                _WRA_REKKY_STOP.clear()
                _WRA_REKKY_STATUS.update(
                    {
                        "running": True,
                        "mode": "record",
                        "workflow_name": wf_name,
                        "error": "",
                        "saved_path": "",
                        "enrich_path": "",
                        "enrich_report": {},
                    }
                )

                def _run():
                    try:
                        from cusear.engine.rekky import start_rekky_recording

                        wf = start_rekky_recording(
                            workflow_name=wf_name,
                            platform=platform_name,
                            url=url,
                            workflows_dir=str(WORKFLOWS_DIR),
                            stop_event=_WRA_REKKY_STOP,
                            batch_tabs=batch_tabs,
                        )
                        saved = str(WORKFLOWS_DIR / f"{wf.get('workflow_name')}.json")
                        with _WRA_REKKY_LOCK:
                            _WRA_REKKY_STATUS.update(
                                {"running": False, "mode": "", "saved_path": saved, "error": "", "enrich_report": {}}
                            )
                    except Exception as exc:
                        with _WRA_REKKY_LOCK:
                            _WRA_REKKY_STATUS.update({"running": False, "mode": "", "error": str(exc)})

                global _WRA_REKKY_THREAD
                _WRA_REKKY_THREAD = threading.Thread(target=_run, daemon=True)
                _WRA_REKKY_THREAD.start()
            self._json({"success": True, "running": True, "workflow_name": wf_name})

        elif p == "/wra/rekky/stop":
            with _WRA_REKKY_LOCK:
                if not _WRA_REKKY_STATUS.get("running"):
                    self._json({"success": True, "running": False})
                    return
                _WRA_REKKY_STOP.set()
            self._json({"success": True, "running": False})

        elif p == "/wra/rekky/enrich":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "invalid JSON"}, 400)
                return
            wf_name = str(payload.get("workflow_name") or "").strip()
            if not wf_name:
                self._json({"error": "workflow_name required"}, 400)
                return
            fp = WORKFLOWS_DIR / f"{wf_name}.json"
            if not fp.is_file():
                self._json({"error": "workflow not found"}, 404)
                return
            wf_path_str = str(fp.resolve())
            with _WRA_REKKY_LOCK:
                if _WRA_REKKY_STATUS.get("running"):
                    self._json({"error": "Rekky already running"}, 409)
                    return
                _WRA_REKKY_STATUS.update(
                    {
                        "running": True,
                        "mode": "enrich",
                        "workflow_name": wf_name,
                        "error": "",
                        "saved_path": "",
                        "enrich_path": wf_path_str,
                        "enrich_report": {},
                    }
                )

                def _run_enrich():
                    try:
                        from cusear.engine.rekky import enrich_workflow

                        rep = enrich_workflow(wf_path_str)
                        with _WRA_REKKY_LOCK:
                            _WRA_REKKY_STATUS.update(
                                {
                                    "running": False,
                                    "mode": "",
                                    "saved_path": wf_path_str,
                                    "enrich_report": rep,
                                    "error": "",
                                }
                            )
                    except Exception as exc:
                        with _WRA_REKKY_LOCK:
                            _WRA_REKKY_STATUS.update({"running": False, "mode": "", "error": str(exc), "enrich_report": {}})

                threading.Thread(target=_run_enrich, daemon=True).start()

            self._json({"success": True, "started": True, "workflow_name": wf_name})

        elif p == "/cusear/save-calendar-media":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                bnd = re.search(r"boundary=([^\s;]+)", ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400)
                    return
                fields = parse_multipart(body, bnd.group(1))
                layer = str(fields.get("calendar_layer") or "").strip().lower()
                slot_kind = str(fields.get("slot_kind") or "auto").strip().lower()
                file_items = []
                for fk in ("media", "file", "upload"):
                    lst = fields.get(fk)
                    if isinstance(lst, list):
                        file_items.extend(lst)
                if layer not in ("core", "hybrid", "ai"):
                    self._json({"error": "calendar_layer must be core, hybrid, or ai"}, 400)
                    return
                if slot_kind not in ("auto", "image", "video", "text"):
                    slot_kind = "auto"
                if not file_items:
                    self._json({"error": "no file uploaded"}, 400)
                    return
                try:
                    single_day = int(str(fields.get("calendar_day") or "1").strip())
                except (TypeError, ValueError):
                    single_day = 1
                try:
                    start_raw = str(fields.get("calendar_start_day") or "").strip()
                    start_day = int(start_raw) if start_raw else single_day
                except (TypeError, ValueError):
                    start_day = single_day
                max_d = calendar_total_days_env()
                dl = _downloads_dir()
                saved_rows: list[dict[str, Any]] = []
                if len(file_items) == 1:
                    day_plan = [single_day]
                else:
                    day_plan = list(range(start_day, start_day + len(file_items)))
                    if day_plan[-1] > max_d:
                        self._json(
                            {
                                "error": (
                                    f"bulk upload exceeds calendar length "
                                    f"(last day would be {day_plan[-1]}; max {max_d})"
                                )
                            },
                            400,
                        )
                        return
                for idx, fi in enumerate(file_items):
                    d_day = day_plan[idx]
                    data_blob = fi.get("data") or b""
                    if not isinstance(data_blob, (bytes, bytearray)) or len(data_blob) < 1:
                        self._json({"error": f"empty file at index {idx}"}, 400)
                        return
                    fn_src = str(fi.get("filename") or "").strip()
                    ctype_src = str(fi.get("content_type") or "").strip()
                    try:
                        row = write_calendar_slot_media(
                            dl,
                            flow_label="",
                            workflow_key="",
                            workflow_display="",
                            layer_key=layer,
                            day=d_day,
                            slot_kind=slot_kind or "auto",
                            data=bytes(data_blob),
                            original_filename=fn_src,
                            content_type=ctype_src,
                        )
                    except ValueError as ve:
                        self._json({"error": str(ve)}, 400)
                        return
                    saved_rows.append(row)
                self._json(
                    {
                        "ok": True,
                        "saved": saved_rows,
                        "calendar_max_days": max_d,
                        "vault_root": str(vault_root(dl).resolve()),
                    },
                    no_cache=True,
                )
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/init":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                dl = _downloads_dir()
                payload: dict[str, Any] = {}
                try:
                    raw = body.decode("utf-8") if body else ""
                    if raw.strip():
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            payload = parsed
                except Exception:
                    payload = {}
                layout_raw = str(payload.get("layout") or payload.get("mode") or "").strip().lower()
                layout = "full" if layout_raw in ("full", "all") else "minimal"
                if (os.environ.get("CUSEAR_STORAGE_LAYOUT") or "").strip().lower() in ("full", "all", "1", "true", "yes"):
                    layout = "full"
                self._json(bootstrap_storage_vault(dl, layout=layout), no_cache=True)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/ensure-plan":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                plan_raw = str(payload.get("plan") or "").strip().lower()
                platform_raw = str(payload.get("platform") or "").strip().lower()
                plan = _cusear_storage_norm_plan(plan_raw)
                if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
                    self._json({"error": "plan must be core, hybrid, ai_budget, or ai_pro"}, 400)
                    return
                if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                    self._json({"error": "platform required for AI Pro"}, 400)
                    return
                dl = _downloads_dir()
                if plan == "ai_pro":
                    d = ensure_plan_vault(dl, "ai_pro", platform=platform_raw)  # type: ignore[arg-type]
                else:
                    d = ensure_plan_vault(dl, plan)  # type: ignore[arg-type]
                self._json({"ok": True, **d}, no_cache=True)
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/cleanup-legacy":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                if not bool(payload.get("confirm")):
                    self._json({"error": 'Set "confirm": true to remove legacy folders'}, 400)
                    return
                raw_names = payload.get("names")
                names: list[str] | None = None
                if isinstance(raw_names, list) and raw_names:
                    names = [str(x).strip() for x in raw_names if str(x).strip()]
                dl = _downloads_dir()
                self._json(cleanup_cusear_legacy_top_level(dl, names=names), no_cache=True)
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/upload-slot":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                bnd = re.search(r"boundary=([^\s;]+)", ct)
                if not bnd:
                    self._json({"error": "no boundary"}, 400)
                    return
                fields = parse_multipart(body, bnd.group(1))
                plan_raw = str(fields.get("plan") or "").strip().lower()
                media_raw = str(fields.get("media_kind") or fields.get("kind") or "").strip().lower()
                if not media_raw:
                    mv = fields.get("media")
                    media_raw = str(mv or "").strip().lower() if not isinstance(mv, list) else ""
                platform_raw = str(fields.get("platform") or "").strip().lower()
                try:
                    day = int(str(fields.get("day") or "1").strip())
                except (TypeError, ValueError):
                    day = 1
                file_items: list[dict[str, Any]] = []
                for fk in ("media", "file", "upload"):
                    lst = fields.get(fk)
                    if isinstance(lst, list):
                        file_items.extend(lst)
                if not file_items:
                    self._json({"error": "no file uploaded"}, 400)
                    return

                def norm_plan(x: str) -> str:
                    t = (x or "").strip().lower().replace("-", "_").replace(" ", "_")
                    if t in ("core",):
                        return "core"
                    if t in ("hybrid",):
                        return "hybrid"
                    if t in ("ai_budget", "aibudget", "budget", "ai"):
                        return "ai_budget"
                    if t in ("ai_pro", "aipro", "pro"):
                        return "ai_pro"
                    return ""

                def norm_media(x: str) -> str:
                    t = (x or "").strip().lower()
                    if t in ("text", "texts", "caption", "captions"):
                        return "text"
                    if t in ("image", "images", "img"):
                        return "image"
                    if t in ("video", "videos", "vid"):
                        return "video"
                    return ""

                plan = norm_plan(plan_raw)
                media = norm_media(media_raw)
                if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
                    self._json({"error": "plan must be Core, Hybrid, AI Budget, or AI Pro"}, 400)
                    return
                if media not in ("text", "image", "video"):
                    self._json({"error": "media must be Texts, Images, or Videos"}, 400)
                    return
                if day < 1 or day > calendar_total_days_env():
                    self._json({"error": f"day must be 1..{calendar_total_days_env()}"}, 400)
                    return
                if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                    self._json({"error": "platform required for AI Pro (facebook/linkedin/instagram/x/whatsapp)"}, 400)
                    return

                fi = file_items[0]
                data_blob = fi.get("data") or b""
                if not isinstance(data_blob, (bytes, bytearray)) or len(data_blob) < 1:
                    self._json({"error": "uploaded file is empty"}, 400)
                    return
                dl = _downloads_dir()
                _cusear_ensure_storage_plan(dl, plan, platform_raw)
                dest = slot_path(
                    dl,
                    plan=plan,
                    media=media,
                    day=day,
                    platform=(platform_raw or None),
                )
                from cusear.storage_vault import atomic_write_bytes as _awb

                _awb(dest, bytes(data_blob))
                self._json(
                    {
                        "ok": True,
                        "path": str(dest.resolve()),
                        "relative_to_downloads": _rel_under_downloads(dl, dest),
                        "plan": plan,
                        "media": media,
                        "platform": platform_raw,
                        "day": day,
                        "size_bytes": int(dest.stat().st_size) if dest.exists() else 0,
                    },
                    no_cache=True,
                )
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/set-text":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                plan_raw = str(payload.get("plan") or "").strip().lower()
                platform_raw = str(payload.get("platform") or "").strip().lower()
                text = str(payload.get("text") or "")
                try:
                    day = int(str(payload.get("day") or "1").strip())
                except (TypeError, ValueError):
                    day = 1

                def norm_plan(x: str) -> str:
                    t = (x or "").strip().lower().replace("-", "_").replace(" ", "_")
                    if t in ("core",):
                        return "core"
                    if t in ("hybrid",):
                        return "hybrid"
                    if t in ("ai_budget", "aibudget", "budget", "ai"):
                        return "ai_budget"
                    if t in ("ai_pro", "aipro", "pro"):
                        return "ai_pro"
                    return ""

                plan = norm_plan(plan_raw)
                if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
                    self._json({"error": "plan required"}, 400)
                    return
                if day < 1 or day > calendar_total_days_env():
                    self._json({"error": f"day must be 1..{calendar_total_days_env()}"}, 400)
                    return
                if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                    self._json({"error": "platform required for AI Pro"}, 400)
                    return
                dl = _downloads_dir()
                _cusear_ensure_storage_plan(dl, plan, platform_raw)
                dest = slot_path(dl, plan=plan, media="text", day=day, platform=(platform_raw or None))
                from cusear.storage_vault import atomic_write_text as _awt

                # Always end with newline for portability with shell tools.
                out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
                if not out.endswith("\n"):
                    out += "\n"
                _awt(dest, out)
                try:
                    rel = str(dest.resolve().relative_to(dl.resolve()))
                except ValueError:
                    rel = str(dest.resolve())
                self._json(
                    {
                        "ok": True,
                        "plan": plan,
                        "day": day,
                        "platform": platform_raw,
                        "path": str(dest.resolve()),
                        "relative_to_downloads": rel,
                        "size_bytes": int(dest.stat().st_size) if dest.exists() else 0,
                    },
                    no_cache=True,
                )
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/topics":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                main_topic = str(payload.get("main_topic") or payload.get("topic") or "").strip()
                if not main_topic:
                    self._json({"error": "main_topic required"}, 400)
                    return
                if not _openai_api_key_configured():
                    self._json(
                        {
                            "ok": False,
                            "error": "OPENAI_API_KEY is not set — add it to .env.local and restart the dashboard for AI topic generation.",
                        },
                        400,
                        no_cache=True,
                    )
                    return
                total = calendar_total_days_env()
                topics = _generate_topics_from_seed(main_topic, completed=[], count=total)
                self._json({"ok": True, "topics": topics, "total_days": total}, no_cache=True)
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/generate-seq":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                plan_raw = str(payload.get("plan") or "").strip().lower()
                media_raw = str(payload.get("media") or "").strip().lower()
                platform_raw = str(payload.get("platform") or "").strip().lower()
                main_topic = str(payload.get("main_topic") or payload.get("topic") or "").strip()
                industry = str(payload.get("industry") or "").strip()
                topics_in = payload.get("topics")
                topics_list: list[str] = []
                if isinstance(topics_in, list):
                    topics_list = [str(x).strip() for x in topics_in if str(x).strip()]
                plan = _cusear_storage_norm_plan(plan_raw)
                media = _cusear_storage_norm_media(media_raw)
                if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
                    self._json({"error": "plan must be Core, Hybrid, AI Budget, or AI Pro"}, 400)
                    return
                if plan == "core":
                    self._json({"error": "Core plan has no AI slots — use paste + Save for texts, upload for media"}, 400)
                    return
                if media not in ("text", "image", "video"):
                    self._json({"error": "media must be text/image/video"}, 400)
                    return
                if not main_topic:
                    self._json({"error": "main_topic required"}, 400)
                    return
                if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                    self._json({"error": "platform required for AI Pro (facebook/linkedin/instagram/x/whatsapp)"}, 400)
                    return
                if not _openai_api_key_configured():
                    self._json(
                        {
                            "ok": False,
                            "error": "OPENAI_API_KEY is not set — add it to .env.local and restart the dashboard for AI generation.",
                        },
                        400,
                        no_cache=True,
                    )
                    return
                total = calendar_total_days_env()
                dl = _downloads_dir()
                days_done: list[dict[str, Any]] = []
                for day in range(1, total + 1):
                    t = topics_list[day - 1] if day - 1 < len(topics_list) else ""
                    topic = t or f"{main_topic} — day {day}"
                    row = _cusear_storage_generate_one(
                        dl,
                        plan=plan,
                        media=media,
                        day=day,
                        platform=platform_raw,
                        industry=industry,
                        main_topic=main_topic,
                        topic=topic,
                    )
                    days_done.append({"day": day, **row})
                self._json(
                    {
                        "ok": True,
                        "plan": plan,
                        "media": media,
                        "total_days": total,
                        "saved": len(days_done),
                        "days": days_done,
                    },
                    no_cache=True,
                )
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/generate-slot":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                plan_raw = str(payload.get("plan") or "").strip().lower()
                media_raw = str(payload.get("media") or "").strip().lower()
                platform_raw = str(payload.get("platform") or "").strip().lower()
                main_topic = str(payload.get("main_topic") or payload.get("topic") or "").strip()
                topic = str(payload.get("topic") or "").strip() or main_topic
                industry = str(payload.get("industry") or "").strip()
                try:
                    day = int(str(payload.get("day") or "1").strip())
                except (TypeError, ValueError):
                    day = 1

                plan = _cusear_storage_norm_plan(plan_raw)
                media = _cusear_storage_norm_media(media_raw)
                if plan not in ("core", "hybrid", "ai_budget", "ai_pro"):
                    self._json({"error": "plan must be Core, Hybrid, AI Budget, or AI Pro"}, 400)
                    return
                if media not in ("text", "image", "video"):
                    self._json({"error": "media must be text/image/video"}, 400)
                    return
                if day < 1 or day > calendar_total_days_env():
                    self._json({"error": f"day must be 1..{calendar_total_days_env()}"}, 400)
                    return
                if plan == "ai_pro" and platform_raw not in PLATFORM_DIR:
                    self._json({"error": "platform required for AI Pro (facebook/linkedin/instagram/x/whatsapp)"}, 400)
                    return
                if not topic:
                    topic = f"Day {day}"
                if not main_topic:
                    main_topic = topic
                if plan == "core":
                    self._json({"error": "Core plan has no AI generate — use paste + Save for texts"}, 400)
                    return
                if not _openai_api_key_configured():
                    self._json(
                        {
                            "ok": False,
                            "error": "OPENAI_API_KEY is not set — add it to .env.local and restart the dashboard for AI generation.",
                        },
                        400,
                        no_cache=True,
                    )
                    return

                dl = _downloads_dir()
                out = _cusear_storage_generate_one(
                    dl,
                    plan=plan,
                    media=media,
                    day=day,
                    platform=platform_raw,
                    industry=industry,
                    main_topic=main_topic,
                    topic=topic,
                )
                self._json({"ok": True, **out}, no_cache=True)
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/storage/hybrid/generate-texts":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            try:
                if not isinstance(payload, dict):
                    self._json({"error": "invalid payload"}, 400)
                    return
                main_topic = str(payload.get("main_topic") or payload.get("topic") or "").strip()
                industry = str(payload.get("industry") or "").strip()
                if not main_topic:
                    self._json({"error": "main_topic required"}, 400)
                    return
                if not _openai_api_key_configured():
                    self._json(
                        {
                            "ok": False,
                            "error": "OPENAI_API_KEY is not set — add it to .env.local and restart the dashboard for AI generation.",
                        },
                        400,
                        no_cache=True,
                    )
                    return
                total = calendar_total_days_env()
                topics = _generate_topics_from_seed(main_topic, completed=[], count=total)
                dl = _downloads_dir()
                _cusear_ensure_storage_plan(dl, "hybrid", "")
                saved = 0
                for i, t in enumerate(topics[:total], 1):
                    _cusear_storage_generate_one(
                        dl,
                        plan="hybrid",
                        media="text",
                        day=i,
                        platform="",
                        industry=industry,
                        main_topic=main_topic,
                        topic=t,
                    )
                    saved += 1
                self._json(
                    {
                        "ok": True,
                        "plan": "hybrid",
                        "media": "text",
                        "saved": saved,
                        "total_days": total,
                        "industry": industry,
                    },
                    no_cache=True,
                )
            except Exception as exc:
                traceback.print_exc()
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        elif p == "/cusear/create-folders":
            if self.command != "POST":
                self._json({"error": "POST required"}, 405)
                return
            try:
                dl = _downloads_dir()
                d = bootstrap_storage_vault(dl, layout="minimal")
                self._json(
                    {
                        "ok": bool(d.get("ok")),
                        "roots_created": 1 if d.get("ok") else 0,
                        "sample_root": str(d.get("root") or ""),
                        "thirty_day_stub_files": int(d.get("stub_files_created") or 0),
                        "total_days": int(d.get("total_days") or 30),
                        "detail": str(d.get("detail") or ""),
                    },
                    no_cache=True,
                )
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500, no_cache=True)

        else:
            self._json({"error": "not found"}, 404)


def _trainer_free_tcp_listeners(port: int) -> None:
    """
    macOS/Linux: stop processes that are LISTEN on this TCP port (stale Trainer from a prior run).
    Skips the current process. No-op on Windows or if lsof is unavailable.
    """
    if sys.platform == "win32":
        return
    try:
        cp = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    me = os.getpid()
    seen: set[int] = set()
    for tok in (cp.stdout or "").replace("\n", " ").split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        if pid == me or pid in seen:
            continue
        seen.add(pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            print(f"  ⚠ Port {port} still held by pid {pid} (could not signal — quit that app or use sudo).")
    if seen:
        time.sleep(0.5)
        for pid in seen:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.2)


def run_trainer_server() -> None:
    """Start the local Trainer HTTP server (file-backed workflows; no cloud DB required)."""
    _trainer_warn_if_pyautogui_missing()
    bind_host = (os.environ.get("TRAINER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    ui_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
    ui_url = f"http://{ui_host}:{PORT}"
    try:
        server = ThreadingHTTPServer((bind_host, PORT), Handler)
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.EADDRINUSE:
            raise
        print(f"  Port {PORT} in use — stopping stale listener(s) and retrying…")
        _trainer_free_tcp_listeners(PORT)
        server = ThreadingHTTPServer((bind_host, PORT), Handler)
    _sched_env = (os.environ.get("TRAINER_SCHEDULER_ENABLED") or "").strip()
    _sched_default = "1"
    if _env_truthy("TRAINER_SCHEDULER_ENABLED", _sched_default):
        global _TRAINER_SCHEDULER_SESSION_STARTED_AT
        _TRAINER_SCHEDULER_SESSION_STARTED_AT = datetime.datetime.now()
        threading.Thread(target=_scheduler_loop, daemon=True, name="trainer-scheduler").start()
    else:
        print("  (scheduler disabled — set TRAINER_SCHEDULER_ENABLED=1 to enable auto-workflow runs)")
    print(f"\n{'━'*50}")
    if os.environ.get("DESKTOP_APP", "").strip().lower() in ("1", "true", "yes"):
        print("  Desktop app — local Trainer engine (workflows/ on disk)")
    if getattr(sys, "frozen", False):
        print(
            f"  Replay workflows: export AGENCY_HOME={BASE_DIR!s}  "
            "then run: python main.py --run-workflow <name> --mode fast"
        )
    print(f"  ⚡ Public site:    {ui_url}/")
    print(f"  ⚡ Control Center: {ui_url}/trainer")
    _ah = (os.environ.get("AGENCY_HOME") or "").strip()
    print(f"  Data root: {BASE_DIR.resolve()}")
    print(f"  Workflows: {WORKFLOWS_DIR.resolve()}")
    if _ah:
        print(f"  AGENCY_HOME: {_ah}")
    else:
        print("  AGENCY_HOME: (unset — using data root above; set only if workflows live elsewhere)")
    if _sched_env:
        print(f"  TRAINER_SCHEDULER_ENABLED={_sched_env!r} (explicit)")
    elif os.environ.get("DESKTOP_APP", "").strip().lower() in ("1", "true", "yes"):
        print("  Scheduler: enabled by default in desktop app (set TRAINER_SCHEDULER_ENABLED=0 to disable)")
    if _env_truthy("TRAINER_WHATSAPP_NOTIFY", "0"):
        _qc = _env_truthy("TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND", "0")
        _sw = (os.environ.get("TRAINER_WHATSAPP_SEND_WORKFLOW") or "").strip()
        print(
            "  Note: TRAINER_WHATSAPP_NOTIFY=1 runs after every workflow (separate from step list). "
            f"Quit Chrome after notify={'on' if _qc else 'off'} (TRAINER_WHATSAPP_QUIT_CHROME_AFTER_SEND)."
        )
        if _sw:
            print(f"  Note: TRAINER_WHATSAPP_SEND_WORKFLOW={_sw!r} runs that workflow before opening /send.")
    if bind_host in ("0.0.0.0", "::"):
        lan_ip = ""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                lan_ip = s.getsockname()[0]
        except Exception:
            pass
        if lan_ip and not lan_ip.startswith("127."):
            print(f"  📱 iPhone/iPad on same Wi-Fi: http://{lan_ip}:{PORT}")
    print(f"  Stop: Ctrl+C")
    print(
        "  Automation scheduler: topic queue + Main topic only (no media campaign in scheduler "
        "unless TRAINER_AUTOMATION_USE_MEDIA_CAMPAIGN=1; no OpenAI topic refill unless "
        "TRAINER_AUTOMATION_TOPIC_AI=1; no campaign AI images unless TRAINER_CAMPAIGN_AI_IMAGES=1)."
    )
    _o = bool((os.environ.get("OPENAI_API_KEY") or "").strip())
    _a = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    _prov = _trainer_vision_provider()
    _active = "OPENAI_API_KEY" if _prov != "anthropic" else "ANTHROPIC_API_KEY"
    _active_ok = _o if _active == "OPENAI_API_KEY" else _a
    print(
        f"  Click training vision: TRAINER_VISION_PROVIDER={_prov} "
        f"· {_active}={'yes' if _active_ok else 'no'}"
    )
    if not _active_ok:
        print(
            "  ⚠ Set OPENAI_API_KEY for vision (training + live-screen clicks). "
            "Use ANTHROPIC_API_KEY only when TRAINER_VISION_PROVIDER=anthropic."
        )
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
    if platform.system() == "Darwin":
        print(f"  Python executable: {sys.executable}")
        print(f"  macOS: {_mac_automation_hint()}")
        ctx = _mac_shell_context_hint()
        if ctx:
            print(f"  {ctx}")
        print(f"  Tip: TRAINER_NO_OPEN_BROWSER=1  → do not auto-open a browser tab on start")
        print(
            "  Tip: TRAINER_ACTIVATE_APP='Google Chrome'  → activate Chrome before each Click (not before Type; "
            "Type uses current focus). Set TRAINER_ACTIVATE_APP_BEFORE_TYPE=1 to also activate before Type."
        )
    print(f"{'━'*50}\n")
    if os.environ.get("TRAINER_NO_OPEN_BROWSER", "").strip().lower() not in ("1", "true", "yes"):
        try:
            webbrowser.open(f"{ui_url}/trainer")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    _maybe_reexec_into_dot_venv()
    run_trainer_server()
