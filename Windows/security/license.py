"""
License System — cusear™ Platform
================================================
Protects the client app from running without a valid, paid, machine-bound license.

How it works
────────────
1. YOU (the seller) generate a license key for each client using generate_license().
   The key is an encrypted blob containing: client email, machine ID, expiry, plan.

2. The client puts license.key in the app folder.

3. On every startup the app calls validate_license(), which:
   a. Decrypts the key using your embedded MASTER_SECRET
   b. Verifies the machine ID matches the computer it's running on
   c. Verifies the expiry date has not passed
   d. Pings your license server (optional — works offline too with a grace period)

4. If any check fails → app refuses to start.

Environment variables (set by YOU before building client binary)
────────────────────────────────────────────────────────────────
  LICENSE_MASTER_SECRET   32-byte hex string — same for all clients, embedded in binary
  LICENSE_SERVER_URL      https://licenses.yourplatform.com/validate  (optional)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import socket
import struct
import time
import uuid
from base64 import b64decode, b64encode
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── try to import cryptography; fall back to pure-python AES via pycryptodome ──
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_LIB = "cryptography"
except ImportError:
    try:
        from Crypto.Cipher import AES as _AES  # type: ignore
        _CRYPTO_LIB = "pycryptodome"
    except ImportError:
        _CRYPTO_LIB = "none"


# ─── Constants ────────────────────────────────────────────────────────────────

LICENSE_FILE     = Path("license.key")
GRACE_PERIOD_S   = 60 * 60 * 24 * 3    # 3 days offline grace period
NONCE_SIZE       = 12                   # AES-GCM nonce bytes


# ─── Master secret ────────────────────────────────────────────────────────────

def _master_secret() -> bytes:
    """
    Returns the 32-byte master key used to encrypt/decrypt all license keys.
    Embedded at build time via LICENSE_MASTER_SECRET env var.
    In the compiled binary this is baked in — clients never see it.
    """
    raw = os.environ.get("LICENSE_MASTER_SECRET", "")
    if raw:
        key = bytes.fromhex(raw) if len(raw) == 64 else raw.encode()
        return key[:32].ljust(32, b"\x00")
    # Development fallback — replace with a real secret before building!
    return b"DEV_SECRET_REPLACE_BEFORE_BUILD!"


# ─── Machine ID ───────────────────────────────────────────────────────────────

def get_machine_id() -> str:
    """
    Returns a stable, unique identifier for this specific computer.
    Combines: MAC address + CPU info + hostname → SHA-256 hash.
    This changes if the client installs the app on a different machine.
    """
    parts = []

    # MAC address (most stable)
    try:
        mac = uuid.getnode()
        parts.append(str(mac))
    except Exception:
        pass

    # Hostname
    try:
        parts.append(socket.gethostname())
    except Exception:
        pass

    # Platform details
    try:
        parts.append(platform.node())
        parts.append(platform.machine())
        parts.append(platform.processor())
    except Exception:
        pass

    # Windows: volume serial number (very stable)
    if platform.system() == "Windows":
        try:
            import subprocess
            result = subprocess.check_output(["cmd", "/c", "vol", "C:"], stderr=subprocess.DEVNULL)
            parts.append(result.decode(errors="ignore"))
        except Exception:
            pass

    # macOS: hardware UUID
    if platform.system() == "Darwin":
        try:
            import subprocess
            result = subprocess.check_output(
                ["system_profiler", "SPHardwareDataType"], stderr=subprocess.DEVNULL
            )
            for line in result.decode().splitlines():
                if "Hardware UUID" in line:
                    parts.append(line.strip())
        except Exception:
            pass

    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


# ─── AES-GCM helpers ─────────────────────────────────────────────────────────

def _aes_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Returns nonce + ciphertext + tag as a single bytes object."""
    nonce = os.urandom(NONCE_SIZE)
    if _CRYPTO_LIB == "cryptography":
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        return nonce + ct
    elif _CRYPTO_LIB == "pycryptodome":
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        return nonce + ct + tag
    else:
        raise RuntimeError("No crypto library available. pip install cryptography")


def _aes_decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypts a blob produced by _aes_encrypt."""
    nonce = blob[:NONCE_SIZE]
    payload = blob[NONCE_SIZE:]
    if _CRYPTO_LIB == "cryptography":
        return AESGCM(key).decrypt(nonce, payload, None)
    elif _CRYPTO_LIB == "pycryptodome":
        ct, tag = payload[:-16], payload[-16:]
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    else:
        raise RuntimeError("No crypto library available.")


# ─── License generation (YOUR tool — never in client binary) ─────────────────

def generate_license(
    client_email:   str,
    machine_id:     str,
    plan:           str = "free",          # free | premium
    valid_days:     int = 365,
    issued_by:      str = "cusear™",
) -> str:
    """
    Generate a license key for a specific client + machine.

    Args:
        client_email: Client's email address
        machine_id:   Hash from get_machine_id() run on client's machine
        plan:         "free" or "premium"
        valid_days:   How many days the license is valid (default 365)
        issued_by:    Your name/brand

    Returns:
        Base64-encoded encrypted license string — give this to the client.
    """
    payload = {
        "email":      client_email,
        "machine_id": machine_id,
        "plan":       plan,
        "issued_at":  datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(days=valid_days)).isoformat(),
        "issued_by":  issued_by,
        "version":    "1",
    }
    plaintext = json.dumps(payload).encode()
    encrypted = _aes_encrypt(plaintext, _master_secret())
    return b64encode(encrypted).decode()


# ─── License validation (in client binary) ────────────────────────────────────

class LicenseError(Exception):
    """Raised when the license check fails — app should exit."""
    pass


def validate_license(
    license_path: Path = LICENSE_FILE,
    check_server: bool = True,
) -> dict:
    """
    Validate the license on startup. Call this before showing the UI.

    Returns the license payload dict if valid.
    Raises LicenseError with a human-readable message if invalid.
    """
    # 1. File exists?
    if not license_path.exists():
        raise LicenseError(
            "No license file found.\n"
            "Please contact support@yourplatform.com to activate your copy."
        )

    # 2. Decrypt
    try:
        blob = b64decode(license_path.read_text().strip())
        payload = json.loads(_aes_decrypt(blob, _master_secret()).decode())
    except Exception:
        raise LicenseError(
            "License file is corrupted or invalid.\n"
            "Please contact support@yourplatform.com."
        )

    # 3. Machine ID check
    current_machine = get_machine_id()
    if not hmac.compare_digest(payload.get("machine_id", ""), current_machine):
        raise LicenseError(
            "This license is registered to a different computer.\n"
            "Each license is valid for one machine.\n"
            "Contact support@yourplatform.com to transfer your license."
        )

    # 4. Expiry check
    try:
        expires_at = datetime.fromisoformat(payload["expires_at"])
        if datetime.utcnow() > expires_at:
            raise LicenseError(
                f"Your license expired on {expires_at.date()}.\n"
                "Please renew at https://yourplatform.com/renew"
            )
    except (KeyError, ValueError):
        raise LicenseError("License expiry date is missing or malformed.")

    # 5. Optional server check (non-blocking — if server unreachable, use grace period)
    if check_server:
        server_url = os.environ.get(
            "LICENSE_SERVER_URL",
            "https://licenses.yourplatform.com/validate",
        )
        _server_check(payload, server_url, license_path)

    return payload


def _server_check(payload: dict, server_url: str, license_path: Path) -> None:
    """
    Ping the license server. If unreachable, allow a 3-day grace period.
    If server says REVOKED → fail immediately.
    """
    grace_file = license_path.parent / ".license_grace"

    try:
        import urllib.request
        import urllib.error

        data = json.dumps({
            "email":      payload["email"],
            "machine_id": payload["machine_id"],
            "plan":       payload["plan"],
        }).encode()

        req = urllib.request.Request(
            server_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())

        st = str(result.get("status") or "").strip().lower()
        if st == "revoked":
            raise LicenseError(
                "Your license has been revoked.\n"
                "Please contact support@yourplatform.com."
            )
        if st == "expired":
            raise LicenseError(
                "Your subscription or license period has ended on the server.\n"
                "Please renew at https://yourplatform.com/renew"
            )

        # Server confirmed valid — reset grace period timer
        grace_file.write_text(str(time.time()))

    except LicenseError:
        raise

    except Exception:
        # Server unreachable — check grace period
        if grace_file.exists():
            last_check = float(grace_file.read_text().strip())
            if time.time() - last_check < GRACE_PERIOD_S:
                return  # Within grace period — allow
        else:
            # First time we can't reach server — start grace period
            grace_file.write_text(str(time.time()))
            return

        raise LicenseError(
            "Could not verify your license (no internet connection).\n"
            "Please connect to the internet within 3 days to continue using the app."
        )


# ─── License server endpoint (runs on YOUR VPS) ───────────────────────────────

def create_license_server_handler():
    """
    FastAPI route handler for POST /validate.
    Add this to your server to support online license verification.

    Example usage in server.py:
        from security.license import create_license_server_handler
        app.add_api_route("/validate", create_license_server_handler(), methods=["POST"])
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    async def handler(request: Request):
        body = await request.json()
        email      = body.get("email", "")
        machine_id = body.get("machine_id", "")

        # Look up in MongoDB
        from agency_api.database import get_collection, Collections
        col = get_collection(Collections.API_KEYS)
        record = col.find_one({"owner_email": email, "active": True})

        if not record:
            return JSONResponse({"status": "revoked"})
        if record.get("machine_id") != machine_id:
            return JSONResponse({"status": "revoked"})

        expires = record.get("license_expires_at")
        if expires and datetime.fromisoformat(expires) < datetime.utcnow():
            return JSONResponse({"status": "expired"})

        return JSONResponse({"status": "valid", "plan": record.get("plan", "free")})

    return handler


# ─── CLI helper (for YOU to generate licenses) ────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="License key generator")
    sub = parser.add_subparsers(dest="cmd")

    # Generate
    gen = sub.add_parser("generate", help="Generate a license key for a client")
    gen.add_argument("--email",      required=True,  help="Client email")
    gen.add_argument("--machine-id", required=True,  help="Client machine ID hash")
    gen.add_argument("--plan",       default="free",  help="free or premium")
    gen.add_argument("--days",       type=int, default=365, help="Valid for N days")
    gen.add_argument("--output",     default="license.key", help="Output file")

    # Get machine ID (run on client machine to get their ID)
    sub.add_parser("machine-id", help="Print this machine's ID (run on client machine)")

    # Validate
    val = sub.add_parser("validate", help="Validate a license file")
    val.add_argument("--file", default="license.key", help="License file path")

    args = parser.parse_args()

    if args.cmd == "generate":
        key = generate_license(
            client_email=args.email,
            machine_id=args.machine_id,
            plan=args.plan,
            valid_days=args.days,
        )
        Path(args.output).write_text(key)
        print(f"✓ License generated → {args.output}")
        print(f"  Email:   {args.email}")
        print(f"  Plan:    {args.plan}")
        print(f"  Valid:   {args.days} days")
        print(f"  Key:     {key[:40]}...")

    elif args.cmd == "machine-id":
        mid = get_machine_id()
        print(f"Machine ID: {mid}")
        print("Send this to the seller to generate your license key.")

    elif args.cmd == "validate":
        try:
            payload = validate_license(Path(args.file), check_server=False)
            print("✓ License is VALID")
            for k, v in payload.items():
                print(f"  {k}: {v}")
        except LicenseError as e:
            print(f"✗ License INVALID: {e}")
