"""
Remote Config Module — Autonomous Web Agency Agent v1.0
Fetches JSON config from Firebase on startup, caches locally, falls back to cache on failure.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

# ─── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
CACHE_FILE = BASE_DIR / "config" / "config_cache.json"
VERSION_FILE = BASE_DIR / "VERSION"

# ─── Defaults ──────────────────────────────────────────────────────────────────
FIREBASE_URL_KEY = "FIREBASE_CONFIG_URL"   # resolved at runtime from env / keychain
DEFAULT_TIMEOUT = 8                        # seconds


def _load_cache() -> dict[str, Any]:
    """Load the locally cached config. Returns empty dict on any error."""
    try:
        if CACHE_FILE.exists():
            with CACHE_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                logger.debug("Remote config: loaded from local cache")
                return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Remote config: cache read failed — %s", exc)
    return {}


def _save_cache(config: dict[str, Any]) -> None:
    """Persist config to local cache file."""
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        logger.debug("Remote config: cache saved to %s", CACHE_FILE)
    except OSError as exc:
        logger.warning("Remote config: could not save cache — %s", exc)


def _read_local_version() -> str:
    """Return the local installed version string (e.g. '1.0.3')."""
    try:
        if VERSION_FILE.exists():
            return VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return "0.0.0"


def fetch_remote_config(firebase_url: str | None = None) -> dict[str, Any]:
    """
    Fetch JSON config from Firebase and cache locally.

    Falls back to the local cache silently if Firebase is unreachable.

    Args:
        firebase_url: Full Firebase RTDB URL ending in '.json'.
                      If None, read from environment variable FIREBASE_CONFIG_URL.

    Returns:
        Parsed config dict (may be cached copy if Firebase unavailable).

    Raises:
        RuntimeError: If neither Firebase nor cache is available.
    """
    import os

    url = firebase_url or os.environ.get(FIREBASE_URL_KEY, "")

    # ── 1. Try Firebase ────────────────────────────────────────────────────────
    if url:
        for attempt in range(1, 4):
            try:
                resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
                config: dict[str, Any] = resp.json()

                # Log version from remote
                remote_version = config.get("version", "unknown")
                local_version = _read_local_version()
                logger.info(
                    "Remote config: fetched from Firebase  "
                    "(remote_version=%s, local_version=%s)",
                    remote_version,
                    local_version,
                )

                # Cache for offline use
                _save_cache(config)
                return config

            except requests.exceptions.Timeout:
                logger.warning(
                    "Remote config: Firebase timeout (attempt %d/3)", attempt
                )
                if attempt < 3:
                    time.sleep(2 ** attempt)  # exponential back-off: 2s, 4s

            except requests.exceptions.ConnectionError:
                logger.warning(
                    "Remote config: Firebase unreachable (attempt %d/3)", attempt
                )
                break   # no point retrying a connection error immediately

            except (requests.exceptions.RequestException, ValueError) as exc:
                logger.error("Remote config: unexpected error — %s", exc)
                break
    else:
        logger.warning(
            "Remote config: no Firebase URL configured — falling back to cache"
        )

    # ── 2. Fallback to cache ───────────────────────────────────────────────────
    cached = _load_cache()
    if cached:
        logger.info(
            "Remote config: using cached config (version=%s)",
            cached.get("version", "unknown"),
        )
        return cached

    # ── 3. Nothing available ───────────────────────────────────────────────────
    raise RuntimeError(
        "Remote config unavailable: Firebase unreachable and no local cache found."
    )


def get_config_value(key: str, default: Any = None, config: dict[str, Any] | None = None) -> Any:
    """
    Convenience helper to read a nested key from the config.

    Supports dot-notation, e.g. 'platforms.github.button_labels'.

    Args:
        key: Dot-separated path into the config dict.
        default: Value returned when key is missing.
        config: Config dict to search; fetches fresh copy if None.

    Returns:
        Value at the specified path, or `default`.
    """
    cfg = config if config is not None else _load_cache()
    parts = key.split(".")
    node: Any = cfg
    for part in parts:
        if not isinstance(node, dict):
            return default
        node = node.get(part, default)
        if node is default:
            return default
    return node
