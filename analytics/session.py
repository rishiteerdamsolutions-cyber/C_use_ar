"""
Session Recorder — Autonomous Web Agency Agent v1.0
Logs every step, method, duration, success rate.
Proof of delivery + analytics for identifying failure-prone steps.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"

# ─── Step status literals ──────────────────────────────────────────────────────
StepStatus = Literal["SUCCESS", "FAILURE", "SKIP", "RETRY", "HUMAN_NEEDED"]


class SessionRecorder:
    """
    Records a complete agent workflow session to a JSON file.

    Usage::

        rec = SessionRecorder(user_email="client@example.com", workflow_name="salon_website")
        rec.log_step("create_cursor_project", "SUCCESS", 12.4, "direct")
        rec.log_step("push_github",            "FAILURE", 3.1, "direct", error="auth failed")
        rec.save_session("https://salon.vercel.app")
    """

    def __init__(self, user_email: str, workflow_name: str) -> None:
        self.session_id: str = str(uuid.uuid4())
        self.user_email: str = user_email
        self.workflow_name: str = workflow_name
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self._steps: list[dict[str, Any]] = []
        self._step_counter: int = 0

        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(
            "SessionRecorder started  session_id=%s  workflow=%s  user=%s",
            self.session_id, workflow_name, user_email,
        )

    # ── Logging a step ────────────────────────────────────────────────────────
    def log_step(
        self,
        step_name: str,
        status: StepStatus,
        duration_seconds: float,
        method: str,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Record a single workflow step.

        Args:
            step_name:        Human-readable step name (e.g. 'push_github').
            status:           One of SUCCESS / FAILURE / SKIP / RETRY / HUMAN_NEEDED.
            duration_seconds: Wall-clock time the step took.
            method:           How it was executed (e.g. 'direct', 'scroll_down', 'ai_recovery').
            error:            Error message if status == FAILURE (empty string otherwise).
            metadata:         Optional extra key-value data to attach to this step.
        """
        self._step_counter += 1
        record: dict[str, Any] = {
            "seq":              self._step_counter,
            "step_name":        step_name,
            "status":           status,
            "duration_seconds": round(duration_seconds, 3),
            "method":           method,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "error":            error,
        }
        if metadata:
            record["metadata"] = metadata

        self._steps.append(record)

        log_fn = logger.info if status == "SUCCESS" else logger.warning
        log_fn(
            "Step [%02d] %-35s  %-12s  %.2fs  method=%s%s",
            self._step_counter,
            step_name,
            status,
            duration_seconds,
            method,
            f"  ERROR={error}" if error else "",
        )

    # ── Success rate ──────────────────────────────────────────────────────────
    def calculate_success_rate(self) -> float:
        """
        Return the success rate as a float between 0.0 and 1.0.

        SKIP steps are excluded from the denominator.
        Returns 1.0 if no steps have been logged.
        """
        countable = [s for s in self._steps if s["status"] != "SKIP"]
        if not countable:
            return 1.0
        successes = sum(1 for s in countable if s["status"] == "SUCCESS")
        return round(successes / len(countable), 4)

    # ── Step failure analysis ─────────────────────────────────────────────────
    def most_failed_steps(self, top_n: int = 5) -> list[dict[str, Any]]:
        """
        Return the `top_n` step names that fail most often (useful for analytics).
        """
        from collections import Counter
        failures = [s["step_name"] for s in self._steps if s["status"] == "FAILURE"]
        return [
            {"step_name": name, "failure_count": count}
            for name, count in Counter(failures).most_common(top_n)
        ]

    # ── Save session ──────────────────────────────────────────────────────────
    def save_session(self, live_url: str = "") -> Path:
        """
        Finalize and write session JSON to sessions/<SESSION_ID>.json.

        Args:
            live_url: The deployed website URL (proof of delivery).

        Returns:
            Path object pointing to the written session file.
        """
        finished_at = datetime.now(timezone.utc).isoformat()
        total_duration = sum(s["duration_seconds"] for s in self._steps)

        payload: dict[str, Any] = {
            "session_id":       self.session_id,
            "user_email":       self.user_email,
            "workflow_name":    self.workflow_name,
            "live_url":         live_url,
            "started_at":       self.started_at,
            "finished_at":      finished_at,
            "total_duration_s": round(total_duration, 2),
            "total_steps":      len(self._steps),
            "success_rate":     self.calculate_success_rate(),
            "most_failed_steps": self.most_failed_steps(),
            "steps":            self._steps,
        }

        out_path = SESSIONS_DIR / f"{self.session_id}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

        logger.info(
            "Session saved → %s  (success_rate=%.1f%%  duration=%.1fs  url=%s)",
            out_path.name,
            self.calculate_success_rate() * 100,
            total_duration,
            live_url or "—",
        )
        return out_path

    # ── Context manager support ───────────────────────────────────────────────
    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            self.log_step(
                step_name="__session_exception__",
                status="FAILURE",
                duration_seconds=0,
                method="exception",
                error=f"{exc_type.__name__}: {exc_val}",
            )
        self.save_session()
        return False   # don't suppress the exception
