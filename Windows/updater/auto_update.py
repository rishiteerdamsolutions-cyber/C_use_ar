"""
Auto-Update System — cusear™ Agent v1.0
Checks Firebase for a newer version, downloads delta, relaunches.
Previous version is kept for 7 days for one-click rollback.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
VERSION_FILE = BASE_DIR / "VERSION"
BACKUP_DIR = BASE_DIR / ".backups"
UPDATE_CACHE = BASE_DIR / ".update_cache"

# ─── Defaults ──────────────────────────────────────────────────────────────────
FIREBASE_UPDATE_URL_KEY = "FIREBASE_UPDATE_URL"   # env var key
DEFAULT_TIMEOUT = 10
BACKUP_RETENTION_DAYS = 7


# ─── Version helpers ───────────────────────────────────────────────────────────
def _parse_version(v: str) -> tuple[int, ...]:
    """Convert '1.2.3' → (1, 2, 3)."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _read_local_version() -> str:
    """Return installed version string from VERSION file."""
    try:
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return "0.0.0"


# ─── Core functions ────────────────────────────────────────────────────────────
def check_for_updates(firebase_url: str | None = None) -> dict[str, Any]:
    """
    Compare local version against Firebase remote manifest.

    Args:
        firebase_url: Firebase RTDB URL serving the update manifest JSON.
                      Falls back to env var FIREBASE_UPDATE_URL.

    Returns:
        dict:
            update_available (bool)
            version          (str)   — latest remote version
            download_url     (str)   — URL to delta zip
            changelog        (str)   — what's new
            local_version    (str)   — current installed version

    Example manifest JSON at Firebase:
        {
          "latest_version": "1.0.4",
          "download_url": "https://storage.../delta_1.0.4.zip",
          "changelog": "Fixed Vercel OTP timing, improved fallback manager",
          "min_required": "1.0.0"
        }
    """
    url = firebase_url or os.environ.get(FIREBASE_UPDATE_URL_KEY, "")
    local_version = _read_local_version()

    if not url:
        logger.debug("No update URL configured — skipping update check")
        return {
            "update_available": False,
            "version": local_version,
            "download_url": "",
            "changelog": "",
            "local_version": local_version,
        }

    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        manifest: dict[str, Any] = resp.json()

        remote_version: str = manifest.get("latest_version", "0.0.0")
        download_url: str = manifest.get("download_url", "")
        changelog: str = manifest.get("changelog", "")

        update_available = _parse_version(remote_version) > _parse_version(local_version)

        logger.info(
            "Update check: local=%s remote=%s available=%s",
            local_version, remote_version, update_available,
        )

        return {
            "update_available": update_available,
            "version":          remote_version,
            "download_url":     download_url,
            "changelog":        changelog,
            "local_version":    local_version,
        }

    except requests.exceptions.RequestException as exc:
        logger.warning("Update check failed (network): %s", exc)
    except (ValueError, KeyError) as exc:
        logger.warning("Update check failed (parse): %s", exc)

    return {
        "update_available": False,
        "version": local_version,
        "download_url": "",
        "changelog": "",
        "local_version": local_version,
    }


def keep_previous_version() -> Path:
    """
    Backup the current installation before applying an update.

    Creates .backups/v{VERSION}_{timestamp}/ with a copy of all .py files.

    Returns:
        Path to the backup directory.
    """
    local_version = _read_local_version()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"v{local_version}_{ts}"
    backup_path.mkdir(parents=True, exist_ok=True)

    # Copy all Python source files (delta updates only touch .py and .json)
    for ext in ("*.py", "*.json"):
        for src_file in BASE_DIR.rglob(ext):
            # Skip backup dir itself and session/cache files
            if ".backups" in src_file.parts or "sessions" in src_file.parts:
                continue
            dest = backup_path / src_file.relative_to(BASE_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    logger.info("Previous version backed up → %s", backup_path)

    # ── Prune old backups (> BACKUP_RETENTION_DAYS) ───────────────────────────
    _prune_old_backups()

    return backup_path


def _prune_old_backups() -> None:
    """Delete backups older than BACKUP_RETENTION_DAYS."""
    if not BACKUP_DIR.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    for backup in BACKUP_DIR.iterdir():
        if not backup.is_dir():
            continue
        mtime = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            shutil.rmtree(backup, ignore_errors=True)
            logger.info("Pruned old backup: %s", backup.name)


def rollback_to_previous() -> bool:
    """
    Restore the most recent backup (one-click rollback).

    Returns:
        True if rollback succeeded, False otherwise.
    """
    if not BACKUP_DIR.exists():
        logger.error("No backups found — cannot rollback")
        return False

    backups = sorted(BACKUP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    backups = [b for b in backups if b.is_dir()]

    if not backups:
        logger.error("No backup directories found")
        return False

    latest_backup = backups[0]
    logger.info("Rolling back to %s", latest_backup.name)

    for src_file in latest_backup.rglob("*"):
        if src_file.is_file():
            dest = BASE_DIR / src_file.relative_to(latest_backup)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)

    logger.info("Rollback complete from %s", latest_backup.name)
    return True


def download_and_apply_update(download_url: str, new_version: str) -> bool:
    """
    Download the delta zip, backup current version, apply update, relaunch.

    Args:
        download_url: URL to delta .zip file.
        new_version:  Version string of the update (e.g. '1.0.4').

    Returns:
        True if download + extraction succeeded (app will relaunch).
        False if download or extraction failed.
    """
    UPDATE_CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = UPDATE_CACHE / f"delta_{new_version}.zip"

    # ── 1. Download ────────────────────────────────────────────────────────────
    logger.info("Downloading update %s from %s", new_version, download_url)
    try:
        with requests.get(download_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
        logger.info("Downloaded %d bytes → %s", zip_path.stat().st_size, zip_path)

    except requests.exceptions.RequestException as exc:
        logger.error("Update download failed: %s", exc)
        return False

    # ── 2. Backup current version ──────────────────────────────────────────────
    keep_previous_version()

    # ── 3. Extract delta zip ───────────────────────────────────────────────────
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(BASE_DIR)
        logger.info("Delta extracted to %s", BASE_DIR)
    except zipfile.BadZipFile as exc:
        logger.error("Update zip corrupt: %s", exc)
        rollback_to_previous()
        return False

    # ── 4. Write new version number ───────────────────────────────────────────
    VERSION_FILE.write_text(new_version, encoding="utf-8")
    logger.info("VERSION updated to %s", new_version)

    # ── 5. Relaunch app ───────────────────────────────────────────────────────
    logger.info("Relaunching application…")
    time.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

    return True   # unreachable after execv, satisfies type checker


# ─── tkinter banner ───────────────────────────────────────────────────────────
def show_update_banner(update_info: dict[str, Any]) -> bool:
    """
    Display a tkinter banner notifying the user of an available update.

    Args:
        update_info: Dict returned by check_for_updates().

    Returns:
        True if user clicked "Install Now", False if "Later".
    """
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        version = update_info.get("version", "?")
        changelog = update_info.get("changelog", "Bug fixes and improvements.")

        msg = (
            f"🆕  Update Available — v{version}\n\n"
            f"What's new:\n{changelog}\n\n"
            "Click OK to install and relaunch automatically.\n"
            "Click Cancel to skip and update later."
        )

        answer = messagebox.askokcancel(
            title=f"Autonomous Agency Agent — Update v{version}",
            message=msg,
            parent=root,
        )
        root.destroy()
        return bool(answer)

    except Exception as exc:
        logger.warning("Could not show update banner: %s", exc)
        return False
