from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import time
import urllib.request
import uuid
from typing import Any

from .logging_utils import write_json


def send_to_company(
    *,
    company_endpoint: str | None,
    logs_dir: str,
    trigger: str,
    step: int,
    expected: dict | None = None,
    actual: dict | None = None,
    drift_map: list | None = None,
    extra_notes: str = "",
) -> dict[str, Any]:
    machine_id = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:16]

    payload: dict[str, Any] = {
        "report_id": str(uuid.uuid4())[:8],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine_id": machine_id,
        "trigger": trigger,
        "step": step,
        "expected": expected or {},
        "actual": actual or {},
        "drift_map": drift_map or [],
        "extra_notes": extra_notes,
        "os": platform.system(),
    }

    safe_ts = payload["timestamp"].replace(":", "").replace(" ", "_")
    log_path = os.path.join(logs_dir, f"report_{safe_ts}.json")
    write_json(log_path, payload)

    if company_endpoint:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                company_endpoint,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)  # noqa: S310
        except Exception:
            # Local save already done; network failure is not fatal for runtime.
            pass

    return payload

