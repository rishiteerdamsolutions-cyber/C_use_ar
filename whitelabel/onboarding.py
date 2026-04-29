"""
Agency Onboarding — new white-label agency signup flow.
cusear™ Platform · White-label Layer

Flow:
  1. Agency fills signup form → POST /whitelabel/onboard
  2. Razorpay ₹9,999/mo payment
  3. Webhook confirms → tenant created → welcome email sent
  4. Agency logs into their subdomain within 5 minutes
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def onboard_agency(
    agency_name:  str,
    subdomain:    str,
    owner_name:   str,
    owner_email:  str,
    owner_phone:  str,
    branding:     dict[str, Any] | None = None,
    payment_id:   str = "",
) -> dict[str, Any]:
    """
    Full onboarding: create tenant + generate admin API key + send welcome email.

    Args:
        agency_name:  Display name, e.g. 'Digital Edge Solutions'
        subdomain:    URL slug, e.g. 'digitaledge'
        owner_name:   Primary contact name
        owner_email:  Admin email (gets welcome email)
        owner_phone:  WhatsApp number for support
        branding:     Initial branding overrides (logo, colours, etc.)
        payment_id:   Razorpay payment_id confirming ₹9,999 payment

    Returns:
        {tenant_id, subdomain, portal_url, admin_api_key, message}
    """
    from whitelabel.tenant_config import create_tenant
    from agency_api.keys import create_key
    from agency_api.models import CREDIT_PACKS

    # ── 1. Create tenant ────────────────────────────────────────────────────
    tenant_id = create_tenant(
        subdomain=subdomain,
        agency_name=agency_name,
        owner_email=owner_email,
        owner_name=owner_name,
        branding=branding or {},
        plan="whitelabel_monthly",
    )
    logger.info("Tenant created for %s: %s", agency_name, tenant_id)

    # ── 2. Generate admin API key with generous credits ──────────────────────
    raw_key, key_id = create_key(
        owner_name=owner_name,
        owner_email=owner_email,
        credits=10_000,           # 10,000 credits included in ₹9,999/mo
        pack="agency",
        razorpay_payment_id=payment_id,
    )
    logger.info("Admin API key created for %s: %s", owner_email, key_id)

    # ── 3. Send welcome email ────────────────────────────────────────────────
    portal_url = f"https://{subdomain}.{os.environ.get('PLATFORM_DOMAIN', 'yourplatform.com')}"
    _send_welcome_email(
        to_email=owner_email,
        to_name=owner_name,
        agency_name=agency_name,
        portal_url=portal_url,
        api_key=raw_key,
        subdomain=subdomain,
    )

    return {
        "tenant_id":   tenant_id,
        "subdomain":   subdomain,
        "portal_url":  portal_url,
        "admin_api_key": raw_key,   # shown ONCE
        "credits_included": 10_000,
        "message": (
            f"Welcome to the platform, {owner_name}! "
            f"Your agency portal is live at {portal_url}. "
            "Check your email for the full setup guide."
        ),
    }


def create_whitelabel_order(agency_name: str, subdomain: str, owner_email: str) -> dict[str, Any]:
    """
    Create a Razorpay order for the ₹9,999/mo white-label plan.
    Call this BEFORE onboard_agency — payment must succeed first.
    """
    from agency_api.billing import create_order

    # Temporarily create a placeholder key_id for the order notes
    placeholder_id = f"wl_{subdomain}"

    return create_order(
        amount_inr=9999,
        pack="whitelabel_monthly",
        key_id=placeholder_id,
    )


# ─── Welcome email ────────────────────────────────────────────────────────────
def _send_welcome_email(
    to_email:    str,
    to_name:     str,
    agency_name: str,
    portal_url:  str,
    api_key:     str,
    subdomain:   str,
) -> None:
    """Send welcome email via Gmail/SMTP with setup instructions."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_user or not gmail_pass:
        logger.warning("Gmail not configured — welcome email not sent to %s", to_email)
        return

    html = f"""
<html><body style="font-family:sans-serif;background:#0f0f0f;color:#e8e8e8;padding:32px">
<h1 style="color:#c9a96e">Welcome to the platform, {to_name}! 🎉</h1>
<p>Your white-label agency portal is live:</p>
<a href="{portal_url}" style="display:inline-block;background:#c9a96e;color:#000;
   padding:12px 24px;border-radius:8px;font-weight:700;text-decoration:none;margin:12px 0">
   Open {agency_name} Portal →
</a>
<h2 style="color:#c9a96e;margin-top:28px">Your Admin API Key</h2>
<p style="background:#1a1a1a;padding:12px;border-radius:6px;font-family:monospace;
   color:#86efac;word-break:break-all">{api_key}</p>
<p style="color:#888;font-size:.85rem">⚠ Save this key — it will not be shown again.</p>
<h2 style="color:#c9a96e;margin-top:28px">Next Steps</h2>
<ol style="color:#aaa;line-height:2">
  <li>Open your portal: <a href="{portal_url}" style="color:#c9a96e">{portal_url}</a></li>
  <li>Customise your branding (logo, colours) in Admin → Branding</li>
  <li>Add your first client workflow</li>
  <li>Optionally set a custom domain in Admin → Domains</li>
</ol>
<hr style="border-color:#333;margin:24px 0">
<p style="color:#555;font-size:.8rem">
  Subdomain: {subdomain} · Credits included: 10,000 · Plan: White-label Monthly ₹9,999/mo
</p>
</body></html>
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🎉 Your {agency_name} portal is live!"
        msg["From"]    = gmail_user
        msg["To"]      = to_email
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.sendmail(gmail_user, to_email, msg.as_string())

        logger.info("Welcome email sent to %s", to_email)

    except Exception as exc:
        logger.error("Failed to send welcome email to %s: %s", to_email, exc)
