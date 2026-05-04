#!/usr/bin/env python3
"""
Internal Trainer application entrypoint.

This is the developer-only studio shell. It intentionally keeps the existing
HTML Control Center stack for now, but it is no longer the entrypoint used by
customer exports.
"""
from __future__ import annotations

import os


def main() -> int:
    os.environ.setdefault("DESKTOP_APP", "1")
    os.environ.setdefault("AGENCY_USER_MODE", "trainer")
    os.environ.setdefault("TRAINER_NO_OPEN_BROWSER", "1")
    os.environ.setdefault("DESKTOP_LICENSE_CHECK", "0")

    from desktop import main as desktop_main

    return int(desktop_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
