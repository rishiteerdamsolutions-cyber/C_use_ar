"""
Local agent: WebSocket control plane + HTTP ingest for ``company_endpoint`` reports.

Paths (no /api/v1 prefix):
  WS   /agent/ws/{token}
  POST /agent/report/{run_id}?s=<report_secret>
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from agency_api import agent_hub
from agency_api.supabase_client import (
    find_user_by_agent_token,
    insert_run_row,
    update_run_status,
    upsert_agent_status,
    verify_dev_agent_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Local Agent"])


@router.websocket("/agent/ws/{token}")
async def agent_websocket(websocket: WebSocket, token: str) -> None:
    await websocket.accept()

    user = find_user_by_agent_token(token) or verify_dev_agent_token(token)
    if not user:
        await websocket.close(code=4001)
        return
    if user.get("active") is False:
        await websocket.close(code=4003)
        return

    user_id = str(user["id"])
    await agent_hub.register_agent(user_id, websocket)

    upsert_agent_status(
        user_id,
        {
            "connected": True,
            "last_seen": None,
        },
    )

    try:
        for msg in await agent_hub.drain_pending(user_id):
            await websocket.send_json(msg)

        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_agent_message(websocket, user_id, data)
    except WebSocketDisconnect:
        logger.info("WebSocketDisconnect user_id=%s", user_id)
    finally:
        await agent_hub.unregister_agent(user_id)
        upsert_agent_status(user_id, {"connected": False})


async def _handle_agent_message(
    websocket: WebSocket,
    user_id: str,
    msg: dict[str, Any],
) -> None:
    mtype = msg.get("type")

    if mtype == "agent_hello":
        upsert_agent_status(
            user_id,
            {
                "connected": True,
                "os": msg.get("os"),
                "version": msg.get("version"),
                "chrome": msg.get("chrome"),
            },
        )
        return

    if mtype == "run_started":
        rid = str(msg.get("run_id") or "")
        if rid:
            update_run_status(rid, status="running")
        return

    if mtype == "run_complete":
        rid = str(msg.get("run_id") or "")
        ok = bool(msg.get("success"))
        if rid:
            update_run_status(rid, status="success" if ok else "failed", completed=True)
            agent_hub.clear_run(rid)
        return

    if mtype == "run_error":
        rid = str(msg.get("run_id") or "")
        if rid:
            update_run_status(
                rid,
                status="error",
                error=str(msg.get("error") or "unknown"),
                completed=True,
            )
            agent_hub.clear_run(rid)
        return

    if mtype == "status_report":
        upsert_agent_status(
            user_id,
            {
                "connected": True,
                "os": msg.get("os"),
                "version": msg.get("version"),
                "chrome": msg.get("chrome"),
                "disk_free_mb": msg.get("disk_free_mb"),
                "workflows": msg.get("workflows"),
            },
        )
        return

    if mtype == "log_line":
        # Optional streaming; store in meta later if needed
        return

    if mtype == "whatsapp_send":
        # Outbound WhatsApp is server-side (Twilio/Meta); stub for now.
        logger.info("whatsapp_send stub user_id=%s msg=%s", user_id, msg.get("message"))
        return


@router.post("/agent/report/{run_id}", include_in_schema=False)
async def agent_company_report(
    run_id: str,
    request: Request,
    s: str | None = Query(default=None, description="report_secret from run instruction"),
) -> dict[str, Any]:
    """Ingest JSON POSTs from WRA ``send_to_company`` (``company_endpoint``)."""
    meta = agent_hub.verify_run_report(run_id, s)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown run or invalid secret")

    try:
        body = await request.json()
    except Exception:
        body = {}

    logger.info(
        "company_report run_id=%s trigger=%s step=%s",
        run_id,
        body.get("trigger"),
        body.get("step"),
    )
    return {"ok": True, "received": True}
