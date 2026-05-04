"""
Resolve the agency data directory (workflows/, screenshots/, .env.local).

Override with AGENCY_HOME so a PyInstaller Trainer build and `main.py --run-workflow`
use the same workflows/ folder.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def agency_root() -> Path:
    override = (os.environ.get("AGENCY_HOME") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
