"""
Welcome and transactional email via Resend (https://resend.com).

Env:
  RESEND_API_KEY
  RESEND_FROM           — e.g. "cusear <onboarding@cusear.autos>"
  PUBLIC_APP_URL        — dashboard link in email (default https://app.cusear.autos/app)
  PUBLIC_API_BASE_URL   — shown for agent WebSocket / API base
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _cfg() -> tuple[str, str, str, str]:
    key = (os.environ.get("RESEND_API_KEY") or "").strip()
    from_addr = (os.environ.get("RESEND_FROM") or "cusear <onboarding@example.com>").strip()
    app_url = (os.environ.get("PUBLIC_APP_URL") or "https://app.cusear.autos/app").strip().rstrip("/")
    api_url = (os.environ.get("PUBLIC_API_BASE_URL") or "https://api.cusear.autos").strip().rstrip("/")
    return key, from_addr, app_url, api_url


def send_welcome_email(
    *,
    to_email: str,
    agent_token: str,
    plan: str | None = None,
) -> bool:
    """
    Send post-checkout welcome email with agent token and setup steps.
    Returns True if sent (or skipped in dev when RESEND_API_KEY unset — logs only).
    """
    key, from_addr, app_url, api_url = _cfg()
    subject = "Welcome to cusear™ — your agent token"
    plan_line = f"<p><strong>Plan:</strong> {plan or '—'}</p>" if plan else ""

    html = f"""<!DOCTYPE html>
<html><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#0f172a;">
  <h1>Welcome to cusear™</h1>
  <p>Your subscription is active. Use the token below with the local agent on your Mac or Windows PC.</p>
  {plan_line}
  <p><strong>Your agent token (keep secret — never share in the browser):</strong></p>
  <pre style="background:#f1f5f9;padding:12px;border-radius:8px;word-break:break-all;">{agent_token}</pre>
  <h2>Quick start</h2>
  <ol>
    <li>Open the dashboard: <a href="{app_url}">{app_url}</a></li>
    <li>Install Python 3.10+ and clone/download the repo, or use the packaged <code>cusear_agent.py</code> from your download link.</li>
    <li>From the repo root (with dependencies installed):<br/>
      <code>python3 cusear_agent.py YOUR_TOKEN</code></li>
    <li>Optional: set <code>CUSEAR_WS_BASE=wss://...</code> if your API host differs.<br/>
      API base for this environment: <code>{api_url}</code></li>
  </ol>
  <p>Automation runs on your machine — sessions stay local.</p>
  <p>— cusear™</p>
</body></html>"""

    if not key:
        logger.warning(
            "RESEND_API_KEY not set — welcome email not sent (dev). to=%s",
            to_email,
        )
        return False

    try:
        import requests
    except ImportError:
        logger.error("requests not installed")
        return False

    payload: dict[str, Any] = {
        "from":    from_addr,
        "to":      [to_email],
        "subject": subject,
        "html":    html,
    }
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code >= 400:
            logger.error("Resend API error %s: %s", r.status_code, r.text)
            return False
        logger.info("Welcome email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Resend send failed: %s", exc, exc_info=True)
        return False
