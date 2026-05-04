"""Completion notification helpers for customer app runs."""
from __future__ import annotations

import urllib.parse
import webbrowser


def normalize_phone(number: str) -> str:
    return "".join(ch for ch in str(number or "") if ch.isdigit())


def send_whatsapp_confirmation(number: str, message: str) -> str:
    """Open a WhatsApp confirmation URL when a number is configured.

    The customer app intentionally keeps this small and local. It does not expose
    Trainer APIs; it simply opens the installed/browser WhatsApp handler.
    """
    phone = normalize_phone(number)
    if not phone:
        return "No WhatsApp number configured."
    encoded = urllib.parse.quote(str(message or "Automation run complete."))
    url = f"https://wa.me/{phone}?text={encoded}"
    webbrowser.open(url)
    return f"Opened WhatsApp confirmation for {phone}."
