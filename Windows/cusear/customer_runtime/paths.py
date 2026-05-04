"""Filesystem helpers for the runner-only customer app."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config.local_paths import agency_root


def root() -> Path:
    return agency_root()


def workflows_dir() -> Path:
    return root() / "workflows"


def bundles_dir() -> Path:
    return root() / "bundles"


def run_audit_dir() -> Path:
    path = root() / "logs" / "workflow_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_stem(value: str) -> str:
    return Path(str(value or "").strip()).stem


def workflow_path(name: str) -> Path:
    stem = _safe_stem(name)
    if not stem:
        raise ValueError("Workflow name is required")
    return workflows_dir() / f"{stem}.json"


def bundle_path(slug: str) -> Path:
    stem = _safe_stem(slug)
    if not stem:
        raise ValueError("Bundle slug is required")
    return bundles_dir() / f"{stem}.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def available_bundles() -> list[str]:
    if not bundles_dir().is_dir():
        return []
    return [p.stem for p in sorted(bundles_dir().glob("*.json"))]


def default_bundle_slug() -> str:
    env_slug = (os.environ.get("CUSEAR_DEFAULT_AR_SLUG") or "").strip()
    if env_slug:
        return _safe_stem(env_slug)
    bundles = available_bundles()
    return bundles[0] if bundles else ""


def load_bundle(slug: str | None = None) -> tuple[str, dict[str, Any]]:
    chosen = _safe_stem(slug or default_bundle_slug())
    if not chosen:
        raise FileNotFoundError(f"No bundle JSON found under {bundles_dir()}")
    path = bundle_path(chosen)
    if not path.is_file():
        raise FileNotFoundError(f"Bundle not found: {path}")
    data = load_json(path)
    return chosen, data
