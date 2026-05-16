"""
APScheduler polling loop for cloud schedules.

- Reads active schedules from Supabase every minute.
- If schedule matches current day + HH:MM, dispatches run_workflow to local agent via AgentHub.
- If agent is offline, instruction is queued (existing AgentHub behavior).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from urllib.parse import quote

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agency_api import agent_hub
from agency_api.supabase_client import (
    get_workflow_for_run,
    insert_run_row,
    list_active_schedules,
    supabase_configured,
)

logger = logging.getLogger(__name__)

_sched: AsyncIOScheduler | None = None
_fired_keys: set[str] = set()
_MAX_FIRED_KEYS = 10000
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _public_api_base() -> str:
    return (os.environ.get("PUBLIC_API_BASE_URL") or "").rstrip("/")


def _trim_fired_keys() -> None:
    if len(_fired_keys) > _MAX_FIRED_KEYS:
        # Light reset to cap memory; safe because keys are minute-scoped.
        _fired_keys.clear()


async def _tick() -> None:
    if not supabase_configured():
        return
    base = _public_api_base()
    if not base:
        return

    now = datetime.now()
    day = _DOW[now.weekday()]
    hhmm = now.strftime("%H:%M")
    date_key = now.strftime("%Y-%m-%d")

    rows = list_active_schedules()
    for sc in rows:
        try:
            if day not in (sc.get("days") or []):
                continue
            run_time = str(sc.get("run_time") or "")[:5]
            if run_time != hhmm:
                continue

            sid = str(sc.get("id") or "")
            if not sid:
                continue
            dedupe = f"{sid}:{date_key}:{hhmm}"
            if dedupe in _fired_keys:
                continue
            _fired_keys.add(dedupe)
            _trim_fired_keys()

            user_id = str(sc.get("user_id") or "")
            workflow_id = str(sc.get("workflow_id") or "")
            if not user_id or not workflow_id:
                continue

            wf_row = get_workflow_for_run(user_id, workflow_id)
            if not wf_row:
                logger.warning("schedule skipped: workflow missing user_id=%s workflow_id=%s", user_id, workflow_id)
                continue

            run_id = agent_hub.new_run_id()
            report_secret = agent_hub.new_report_secret()
            agent_hub.register_run(run_id, user_id, report_secret)
            insert_run_row(
                run_id=run_id,
                user_id=user_id,
                workflow_name=str(wf_row["name"]),
                status="queued",
                report_secret=report_secret,
                workflow_id=workflow_id,
            )
            company_endpoint = f"{base}/agent/report/{run_id}?s={quote(report_secret, safe='')}"
            instruction = {
                "type": "run_workflow",
                "run_id": run_id,
                "report_secret": report_secret,
                "workflow_name": str(wf_row["name"]),
                "platform": wf_row.get("platform"),
                "content_map": sc.get("content_map") or {},
                "workflow_data": wf_row.get("workflow_json") or {},
                "company_endpoint": company_endpoint,
                "source": "schedule",
                "schedule_id": sid,
            }
            sent = await agent_hub.send_to_agent(user_id, instruction)
            logger.info(
                "schedule dispatched schedule_id=%s user_id=%s run_id=%s mode=%s",
                sid,
                user_id,
                run_id,
                "sent" if sent else "queued",
            )
        except Exception as exc:
            logger.error("schedule tick item error: %s", exc, exc_info=True)


def start_scheduler() -> None:
    global _sched
    if _sched and _sched.running:
        return
    _sched = AsyncIOScheduler()
    _sched.add_job(_tick, IntervalTrigger(minutes=1), id="cloud_schedule_tick", max_instances=1, coalesce=True)
    _sched.start()
    logger.info("Cloud scheduler started (1-minute poll)")


def stop_scheduler() -> None:
    global _sched
    if _sched:
        try:
            _sched.shutdown(wait=False)
        except Exception:
            pass
        _sched = None
    logger.info("Cloud scheduler stopped")

