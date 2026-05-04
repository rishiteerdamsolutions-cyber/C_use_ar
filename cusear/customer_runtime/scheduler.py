"""Native customer-app scheduler for one exported ar bundle."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import threading
import time
from typing import Callable

from .paths import bundle_path, default_bundle_slug, load_bundle, write_json
from .workflow_runner import WorkflowRunResult, run_bundle

StatusCallback = Callable[[str], None]


@dataclasses.dataclass
class RuntimeStatus:
    bundle_slug: str = ""
    schedule_enabled: bool = False
    next_run_at: str = ""
    running: bool = False
    last_status: str = "idle"
    last_log_path: str = ""


def _parse_local(value: str) -> _dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1]
        return _dt.datetime.fromisoformat(raw)
    except Exception:
        return None


def _next_from_schedule(schedule: dict, now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now()
    mode = str(schedule.get("mode") or "daily").lower()
    if mode == "interval":
        minutes = int(schedule.get("interval_minutes") or 0)
        hours = int(schedule.get("interval_hours") or 0)
        delta = _dt.timedelta(minutes=minutes, hours=hours)
        if delta.total_seconds() <= 0:
            delta = _dt.timedelta(hours=24)
        return (now + delta).replace(microsecond=0).isoformat()

    daily_time = str(schedule.get("daily_time") or "09:00").strip() or "09:00"
    try:
        hh, mm = daily_time.split(":", 1)
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += _dt.timedelta(days=1)
    return target.isoformat()


class CustomerRuntime:
    """Owns one customer bundle, its scheduler thread, and stop signal."""

    def __init__(self, bundle_slug: str | None = None, status_cb: StatusCallback | None = None) -> None:
        self.bundle_slug = bundle_slug or default_bundle_slug()
        self.status_cb = status_cb
        self.stop_event = threading.Event()
        self._scheduler_stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._status = RuntimeStatus(bundle_slug=self.bundle_slug)
        self.refresh_status()

    def _emit(self, message: str) -> None:
        self._status.last_status = message
        if self.status_cb:
            self.status_cb(message)

    def refresh_status(self) -> RuntimeStatus:
        slug, bundle = load_bundle(self.bundle_slug)
        self.bundle_slug = slug
        schedule = bundle.get("schedule") if isinstance(bundle.get("schedule"), dict) else {}
        self._status.bundle_slug = slug
        self._status.schedule_enabled = bool(schedule.get("enabled"))
        self._status.next_run_at = str(bundle.get("next_run_at") or "")
        self._status.running = self._run_lock.locked()
        return dataclasses.replace(self._status)

    def ensure_next_run(self) -> RuntimeStatus:
        slug, bundle = load_bundle(self.bundle_slug)
        schedule = bundle.get("schedule") if isinstance(bundle.get("schedule"), dict) else {}
        if schedule.get("enabled") and not str(bundle.get("next_run_at") or "").strip():
            bundle["next_run_at"] = _next_from_schedule(schedule)
            write_json(bundle_path(slug), bundle)
        return self.refresh_status()

    def set_schedule_enabled(self, enabled: bool) -> RuntimeStatus:
        slug, bundle = load_bundle(self.bundle_slug)
        schedule = bundle.get("schedule") if isinstance(bundle.get("schedule"), dict) else {}
        schedule["enabled"] = bool(enabled)
        bundle["schedule"] = schedule
        if enabled:
            bundle["next_run_at"] = _next_from_schedule(schedule)
        write_json(bundle_path(slug), bundle)
        return self.refresh_status()

    def run_now(self, *, dry_run: bool = False) -> list[WorkflowRunResult]:
        if not self.bundle_slug:
            raise FileNotFoundError("No customer ar bundle is available.")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A run is already in progress.")
        try:
            self.stop_event.clear()
            self._status.running = True
            self._emit(f"Running {self.bundle_slug}")
            results = run_bundle(
                self.bundle_slug,
                dry_run=dry_run,
                stop_event=self.stop_event,
                status_cb=self._emit,
            )
            last = results[-1] if results else None
            if last:
                self._status.last_log_path = last.audit_path
            self._emit("Run complete." if all(r.ok for r in results) else "Run failed.")
            slug, bundle = load_bundle(self.bundle_slug)
            schedule = bundle.get("schedule") if isinstance(bundle.get("schedule"), dict) else {}
            if schedule.get("enabled"):
                bundle["next_run_at"] = _next_from_schedule(schedule)
            bundle["last_run_at"] = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            write_json(bundle_path(slug), bundle)
            return results
        finally:
            self._status.running = False
            self._run_lock.release()
            self.refresh_status()

    def stop_current_run(self) -> None:
        self.stop_event.set()
        self._emit("Stop requested.")

    def start_scheduler(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._scheduler_stop.clear()
        self.ensure_next_run()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="customer-scheduler")
        self._thread.start()

    def shutdown(self) -> None:
        self._scheduler_stop.set()
        self.stop_event.set()

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                slug, bundle = load_bundle(self.bundle_slug)
                schedule = bundle.get("schedule") if isinstance(bundle.get("schedule"), dict) else {}
                if schedule.get("enabled"):
                    next_at = _parse_local(str(bundle.get("next_run_at") or ""))
                    now = _dt.datetime.now()
                    if not next_at:
                        bundle["next_run_at"] = _next_from_schedule(schedule, now)
                        write_json(bundle_path(slug), bundle)
                    elif next_at <= now and not self._run_lock.locked():
                        self.run_now(dry_run=False)
                self.refresh_status()
            except Exception as exc:
                self._emit(f"Scheduler error: {exc}")
            self._scheduler_stop.wait(10.0)
