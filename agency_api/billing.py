"""
Billing — Razorpay wallet top-up and webhook handler.
Autonomous Web Agency Platform · API Layer

Flow:
  1. Developer calls POST /billing/create-order  → gets Razorpay order_id
  2. Developer completes payment in their frontend (Razorpay Checkout)
  3. Razorpay calls POST /billing/webhook  → we verify signature + credit wallet
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


# ─── Create Razorpay order ────────────────────────────────────────────────────
def create_order(amount_inr: int, pack: str, key_id: str) -> dict[str, Any]:
    """
    Create a Razorpay order for a credit pack top-up.

    Args:
        amount_inr: Amount in Indian Rupees (e.g. 999).
        pack:       Pack name for metadata (starter / professional / agency).
        key_id:     API key MongoDB _id — stored in order notes for webhook.

    Returns:
        Razorpay order dict: {id, amount, currency, receipt, ...}
    """
    try:
        import razorpay  # type: ignore
    except ImportError as exc:
        raise ImportError("razorpay package not installed — pip install razorpay") from exc

    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

    order_data = {
        "amount":   amount_inr * 100,    # Razorpay expects paise
        "currency": "INR",
        "receipt":  f"agency_{pack}_{key_id[:8]}",
        "notes": {
            "pack":   pack,
            "key_id": key_id,
        },
        "payment_capture": 1,
    }

    order = client.order.create(data=order_data)
    logger.info(
        "Razorpay order created  order_id=%s  amount=₹%d  pack=%s",
        order["id"], amount_inr, pack,
    )

    # Persist to billing collection
    _save_billing_event(
        event_type="order_created",
        order_id=order["id"],
        key_id=key_id,
        amount_inr=amount_inr,
        pack=pack,
        credits=0,
        status="pending",
    )

    return {
        "order_id":     order["id"],
        "amount_paise": amount_inr * 100,
        "currency":     "INR",
        "pack":         pack,
        "key_id_public": RAZORPAY_KEY_ID,   # sent to frontend for Razorpay Checkout
    }


# ─── Webhook handler ──────────────────────────────────────────────────────────
def handle_webhook(raw_body: bytes, signature: str) -> dict[str, Any]:
    """
    Verify Razorpay webhook signature and credit the wallet on success.

    Args:
        raw_body:  Raw request body bytes (must not be parsed first).
        signature: X-Razorpay-Signature header value.

    Returns:
        {"ok": True, "credited": int} on success
        {"ok": False, "reason": str}  on failure
    """
    # ── 1. Verify HMAC-SHA256 signature ───────────────────────────────────────
    if not _verify_signature(raw_body, signature):
        logger.warning("Razorpay webhook signature mismatch — rejected")
        return {"ok": False, "reason": "invalid_signature"}

    # ── 2. Parse event ────────────────────────────────────────────────────────
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "invalid_json"}

    event_type = event.get("event", "")
    logger.info("Razorpay webhook event: %s", event_type)

    # ── 3. Handle payment captured ────────────────────────────────────────────
    if event_type == "payment.captured":
        return _on_payment_captured(event)

    # ── 4. Handle payment failed ──────────────────────────────────────────────
    if event_type == "payment.failed":
        logger.warning("Payment failed: %s", event.get("payload", {}).get("payment", {}).get("entity", {}).get("id"))
        return {"ok": True, "reason": "payment_failed_logged"}

    return {"ok": True, "reason": f"event_ignored: {event_type}"}


def _on_payment_captured(event: dict[str, Any]) -> dict[str, Any]:
    """Credit the API key wallet after confirmed payment."""
    from agency_api.models import CREDIT_PACKS
    from agency_api.keys import top_up_credits, get_key_by_id

    try:
        payment = event["payload"]["payment"]["entity"]
        notes   = payment.get("notes", {})
        pack    = notes.get("pack", "starter")
        key_id  = notes.get("key_id", "")
        amount_paise = int(payment.get("amount", 0))
        payment_id   = payment.get("id", "")

        if not key_id:
            logger.error("Webhook: no key_id in payment notes")
            return {"ok": False, "reason": "missing_key_id"}

        pack_info = CREDIT_PACKS.get(pack, CREDIT_PACKS["starter"])
        credits   = pack_info["credits"]
        amount_inr = amount_paise // 100

        # Credit the wallet
        new_balance = top_up_credits(key_id, credits, payment_id)

        # Persist billing event
        _save_billing_event(
            event_type="payment_captured",
            order_id=payment.get("order_id", ""),
            key_id=key_id,
            amount_inr=amount_inr,
            pack=pack,
            credits=credits,
            status="completed",
            payment_id=payment_id,
        )

        logger.info(
            "Wallet credited  key_id=%s  credits=%d  new_balance=%d  payment_id=%s",
            key_id, credits, new_balance, payment_id,
        )
        return {"ok": True, "credited": credits, "new_balance": new_balance}

    except Exception as exc:
        logger.error("Webhook processing error: %s", exc, exc_info=True)
        return {"ok": False, "reason": str(exc)}


# ─── Signature verification ───────────────────────────────────────────────────
def _verify_signature(raw_body: bytes, signature: str) -> bool:
    """Verify Razorpay HMAC-SHA256 webhook signature."""
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.warning("RAZORPAY_WEBHOOK_SECRET not set — skipping signature check")
        return True   # allow in dev; enforce in production

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ─── Persist billing events ───────────────────────────────────────────────────
def _save_billing_event(
    event_type: str,
    order_id:   str,
    key_id:     str,
    amount_inr: int,
    pack:       str,
    credits:    int,
    status:     str,
    payment_id: Optional[str] = None,
) -> None:
    try:
        from agency_api.database import get_collection, Collections
        col = get_collection(Collections.BILLING)
        col.insert_one({
            "event_type":  event_type,
            "order_id":    order_id,
            "payment_id":  payment_id,
            "key_id":      key_id,
            "amount_inr":  amount_inr,
            "pack":        pack,
            "credits":     credits,
            "status":      status,
            "created_at":  datetime.now(timezone.utc),
        })
    except Exception as exc:
        logger.error("Failed to save billing event: %s", exc)
