from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    failed_check: str = ""
    details: str = ""


def check_internet(timeout: float = 3.0) -> tuple[bool, str]:
    try:
        sock = socket.create_connection(("1.1.1.1", 53), timeout=timeout)
        sock.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def check_chrome() -> tuple[bool, str]:
    # Best-effort: presence in PATH is enough for this layer.
    if shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chrome.exe"):
        return True, ""
    # On macOS, Chrome is typically an app bundle.
    if os.path.exists("/Applications/Google Chrome.app"):
        return True, ""
    return False, "Chrome not found (PATH or /Applications)"


def check_disk_space(min_mb: int = 200) -> tuple[bool, str]:
    usage = shutil.disk_usage(".")
    free_mb = usage.free / (1024 * 1024)
    if free_mb >= min_mb:
        return True, ""
    return False, f"Free space {free_mb:.1f}MB < {min_mb}MB"


def check_content_files(paths: list[str]) -> tuple[bool, str]:
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return False, f"Missing content files: {missing}"
    return True, ""


def run_preflight(checks: list[tuple[str, Callable[[], tuple[bool, str]]]]) -> PreflightResult:
    for name, fn in checks:
        ok, details = fn()
        if not ok:
            return PreflightResult(ok=False, failed_check=name, details=details)
    return PreflightResult(ok=True)

