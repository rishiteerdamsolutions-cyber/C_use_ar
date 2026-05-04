from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WraPaths:
    root: str

    @property
    def workflows_dir(self) -> str:
        return os.path.join(self.root, "workflows")

    @property
    def sessions_dir(self) -> str:
        return os.path.join(self.workflows_dir, "sessions")

    @property
    def content_dir(self) -> str:
        return os.path.join(self.root, "content")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.root, "logs")

    @property
    def lucky_logs_dir(self) -> str:
        return os.path.join(self.logs_dir, "lucky")

    @property
    def agami_logs_dir(self) -> str:
        return os.path.join(self.logs_dir, "agami")

    @property
    def screenshots_dir(self) -> str:
        return os.path.join(self.logs_dir, "screenshots")

    @property
    def company_logs_dir(self) -> str:
        return os.path.join(self.logs_dir, "company")

    @property
    def preflight_logs_dir(self) -> str:
        return os.path.join(self.logs_dir, "preflight")


def ensure_dirs(*dirs: str) -> None:
    for d in dirs:
        os.makedirs(d, exist_ok=True)

