"""
In-process registry of connected local agents (WebSocket) and pending instructions.

Designed for Railway with ``uvicorn --workers 1``. Multiple workers require Redis pub/sub.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
# user_id (str) -> WebSocket
_connected: dict[str, WebSocket] = {}
# user_id -> list of JSON-serializable instruction dicts
_pending: dict[str, list[dict[str, Any]]] = {}
# run_id (str) -> { "report_secret": str, "user_id": str }
_run_registry: dict[str, dict[str, str]] = {}


def _norm_user_id(user_id: Any) -> str:
    return str(user_id)


async def register_agent(user_id: Any, websocket: WebSocket) -> None:
    uid = _norm_user_id(user_id)
    async with _lock:
        _connected[uid] = websocket
    logger.info("Agent connected user_id=%s", uid)


async def unregister_agent(user_id: Any) -> None:
    uid = _norm_user_id(user_id)
    async with _lock:
        _connected.pop(uid, None)
    logger.info("Agent disconnected user_id=%s", uid)


def is_agent_connected(user_id: Any) -> bool:
    return _norm_user_id(user_id) in _connected


async def drain_pending(user_id: Any) -> list[dict[str, Any]]:
    uid = _norm_user_id(user_id)
    async with _lock:
        q = _pending.pop(uid, [])
    return q


async def queue_instruction(user_id: Any, instruction: dict[str, Any]) -> bool:
    """
    If agent is connected, send immediately. Otherwise append to pending queue.
    Returns True if sent on the wire, False if queued.
    """
    uid = _norm_user_id(user_id)
    async with _lock:
        ws = _connected.get(uid)
        if ws is not None:
            try:
                await ws.send_json(instruction)
                return True
            except Exception as exc:
                logger.warning("send_json failed user_id=%s: %s — queueing", uid, exc)
        _pending.setdefault(uid, []).append(instruction)
        return False


async def send_to_agent(user_id: Any, instruction: dict[str, Any]) -> bool:
    """Send JSON to connected agent; on failure queue for reconnect."""
    return await queue_instruction(user_id, instruction)


def new_run_id() -> str:
    return str(uuid.uuid4())


def new_report_secret() -> str:
    return secrets.token_urlsafe(24)


def register_run(run_id: str, user_id: Any, report_secret: str) -> None:
    _run_registry[run_id] = {"report_secret": report_secret, "user_id": _norm_user_id(user_id)}


def verify_run_report(run_id: str, secret: str | None) -> dict[str, str] | None:
    meta = _run_registry.get(run_id)
    if not meta:
        return None
    if not secret or secrets.compare_digest(meta["report_secret"], secret):
        return meta
    return None


def clear_run(run_id: str) -> None:
    _run_registry.pop(run_id, None)
