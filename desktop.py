#!/usr/bin/env python3
"""
Desktop app entry — local Trainer UI + on-disk workflows only.

No MongoDB or hosted API is required to record or edit workflows. Replay from a
second terminal:

  python main.py --list-workflows
  python main.py --run-workflow <name> --mode fast

Smart (vision) replay needs a valid license and Premium plan unless APP_MODE=development.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib import request


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


def _writable_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_ROOT = _writable_root()
os.environ.setdefault("AGENCY_HOME", str(_ROOT))


def _maybe_reexec_into_dot_venv() -> None:
    """Use repo .venv when present so pyautogui and other deps match START.sh."""
    if getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        vpy = _ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        vpy = _ROOT / ".venv" / "bin" / "python3"
        if not vpy.is_file():
            vpy = _ROOT / ".venv" / "bin" / "python"
    if not vpy.is_file():
        return
    try:
        if Path(sys.executable).resolve() == vpy.resolve():
            return
    except OSError:
        return
    main_py = Path(__file__).resolve()
    os.execv(str(vpy), [str(vpy), str(main_py), *sys.argv[1:]])

try:
    from dotenv import load_dotenv  # type: ignore

    # Use literal values (no ${...} interpolation) and let .env.local override shell vars.
    load_dotenv(_ROOT / ".env.local", override=True, interpolate=False)
    load_dotenv(_ROOT / ".env", override=False, interpolate=False)
except Exception:
    pass

# Always apply a literal parse fallback for key material.
_load_env_file_literal(_ROOT / ".env.local", override=True)
_load_env_file_literal(_ROOT / ".env", override=False)

os.environ.setdefault("DESKTOP_APP", "1")
# Desktop shell manages its own window opening.
os.environ.setdefault("TRAINER_NO_OPEN_BROWSER", "1")
os.environ.setdefault("AGENCY_USER_MODE", "consumer")
# Single-AR exports set CUSEAR_DEFAULT_AR_SLUG in .env.local — always use consumer UI for those
# builds, even if AGENCY_HOME pointed at another tree that set AGENCY_USER_MODE=trainer.
if (os.environ.get("CUSEAR_DEFAULT_AR_SLUG") or "").strip():
    os.environ["AGENCY_USER_MODE"] = "consumer"

_maybe_reexec_into_dot_venv()


def _maybe_validate_desktop_license() -> None:
    """Match main.py: paid desktop builds should not start without a valid machine-bound license."""
    if (os.environ.get("APP_MODE", "production") or "").strip() == "development":
        return
    # Frozen (PyInstaller) bundle: default ON. Dev from source: default OFF unless explicitly enabled.
    if getattr(sys, "frozen", False):
        default_check = "1"
    else:
        default_check = "0"
    raw = (os.environ.get("DESKTOP_LICENSE_CHECK") or default_check).strip().lower()
    if raw in ("0", "false", "no", "off"):
        return
    try:
        from security.license import LicenseError, validate_license

        validate_license()
    except LicenseError as exc:
        print(f"License required: {exc}")
        sys.exit(2)
    except Exception as exc:
        print(f"License check failed: {exc}")
        sys.exit(2)


_maybe_validate_desktop_license()

from dashboard import PORT, bootstrap_cusear_folders_on_desktop_launch, run_trainer_server  # noqa: E402


def _ui_url() -> str:
    host = (os.environ.get("TRAINER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{PORT}"


def _pywebview_trainer_url(base: str) -> str:
    """macOS WKWebView fills under the traffic lights; TRAINER.html uses #trainerDesktopShell for safe insets."""
    if sys.platform == "darwin":
        return f"{base.rstrip('/')}/#trainerDesktopShell=1"
    return base


def _wait_for_server(url: str, timeout_sec: float = 40.0) -> bool:
    """Poll /health until the dashboard thread is accepting connections."""
    health = f"{url.rstrip('/')}/health"
    deadline = time.time() + max(1.0, timeout_sec)
    while time.time() < deadline:
        try:
            with request.urlopen(health, timeout=2.5) as resp:
                if resp.status == 200:
                    time.sleep(0.35)
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _launch_native_window(url: str) -> bool:
    # On Windows consumer app mode, older pywebview backends can fall back to
    # legacy engines that do not support modern JS syntax used by TRAINER.html,
    # making UI buttons appear non-responsive. Default to system browser there
    # unless explicitly opted in; trainer mode keeps native window behavior.
    #
    # On macOS, the “desktop UI” is still WKWebView (same web stack as Safari),
    # not a second native widget toolkit. pywebview defaults private_mode=True,
    # which clears all website data on launch and can interact badly with the
    # Trainer SPA; we pass private_mode=False in webview.start().
    #
    # DESKTOP_FORCE_BROWSER unset → native window when pywebview is available.
    # DESKTOP_FORCE_BROWSER=1 → system browser. DESKTOP_FORCE_BROWSER=0 →
    # native window (explicit; same as unset on Mac/Linux).
    raw_fb = (os.environ.get("DESKTOP_FORCE_BROWSER") or "").strip().lower()
    if raw_fb in ("1", "true", "yes"):
        force_browser = True
    elif raw_fb in ("0", "false", "no"):
        force_browser = False
    else:
        force_browser = False
    if sys.platform == "win32":
        user_mode = (os.environ.get("AGENCY_USER_MODE") or "").strip().lower() or "consumer"
        native_opt_in = (os.environ.get("DESKTOP_WINDOWS_NATIVE_WEBVIEW") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if user_mode == "consumer" and not native_opt_in:
            force_browser = True
    if force_browser:
        return False
    try:
        import webview  # type: ignore
    except Exception:
        print("  Tip: install native shell with  pip install pywebview  (see requirements.txt)")
        return False

    shell_url = _pywebview_trainer_url(url)
    # Empty native title on macOS avoids a second title string drawn in the title-bar / traffic-light region.
    win_title = "" if sys.platform == "darwin" else "cusear Trainer"
    webview.create_window(
        win_title,
        shell_url,
        width=1320,
        height=900,
        min_size=(1024, 680),
        text_select=True,
        background_color="#0d0d0d",
    )
    dbg = (os.environ.get("DESKTOP_WEBVIEW_DEBUG") or "").strip().lower() in ("1", "true", "yes")
    if not dbg and (os.environ.get("APP_MODE", "production") or "").strip() == "development":
        dbg = True
    webview.start(debug=dbg, private_mode=False)
    return True


if __name__ == "__main__":
    try:
        boot = bootstrap_cusear_folders_on_desktop_launch()
        if int(boot.get("roots_created") or 0) > 0:
            print(
                "  cusear folders ready: "
                f"{int(boot.get('roots_created') or 0)} workflow roots "
                f"(sample: {str(boot.get('sample_root') or '')})"
            )
    except Exception as e:
        print(f"  ⚠ Could not pre-create cusear folders: {e}")
    ui_url = _ui_url()
    server_thread = threading.Thread(target=run_trainer_server, daemon=True, name="trainer-server")
    server_thread.start()
    if not _wait_for_server(ui_url):
        print(
            f"  ⚠ /health did not respond in time — the window may show “Reconnecting” briefly.\n"
            f"  URL: {ui_url}  — use ↻ Retry in the header, or wait; if it persists, run from Terminal to see errors."
        )
    if not _launch_native_window(ui_url):
        print("  Native app window unavailable — opening your default browser instead.")
        print(f"  (Same Trainer UI; internal URL: {ui_url})")
        try:
            webbrowser.open(ui_url)
        except Exception:
            pass
        while server_thread.is_alive():
            time.sleep(0.4)
