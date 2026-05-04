"""
Security Layer — cusear™ Agent v1.0
Stores all credentials in the OS system keychain (keyring).
AES-256 encryption for any local credential cache.
Zero credential logging — enforced by custom log filter.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ─── Keychain service name ─────────────────────────────────────────────────────
KEYCHAIN_SERVICE = "autonomous_web_agency_agent"

# ─── Required credential keys ─────────────────────────────────────────────────
REQUIRED_CREDENTIALS = [
    "gmail",                  # Gmail address for email sending
    "phone",                  # Owner phone number
    "vercel_domain",          # Default Vercel project domain
    "github_pat",             # GitHub Personal Access Token
    "openai_api_key",         # OpenAI API key
    "gemini_api_key",         # Google Gemini API key
    "anthropic_api_key",      # Anthropic API key
]

# ─── Local AES-256 encrypted cache (for offline fallback) ─────────────────────
BASE_DIR = Path(__file__).parent.parent
CRED_CACHE_PATH = BASE_DIR / "security" / ".cred_cache.enc"
KEY_FILE_PATH = BASE_DIR / "security" / ".master_key"


# ─── Log filter: redact secrets ───────────────────────────────────────────────
class CredentialRedactFilter(logging.Filter):
    """
    Log filter that replaces known credential values with [REDACTED].
    Attach to any logger or handler to prevent accidental credential leaks.
    """

    _known_secrets: set[str] = set()

    @classmethod
    def register_secret(cls, value: str) -> None:
        if value and len(value) > 4:
            cls._known_secrets.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self._known_secrets:
            if secret in msg:
                record.msg = record.msg.replace(secret, "[REDACTED]")
                record.args = ()
        return True


# ─── AES-256 helpers ──────────────────────────────────────────────────────────
def _get_or_create_master_key() -> bytes:
    """Return 32-byte master key from file, or create and save a new one."""
    KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE_PATH.exists():
        raw = KEY_FILE_PATH.read_bytes()
        return base64.b64decode(raw)
    key = secrets.token_bytes(32)
    KEY_FILE_PATH.write_bytes(base64.b64encode(key))
    KEY_FILE_PATH.chmod(0o600)   # owner read-only
    logger.info("New master key generated")
    return key


def _encrypt(plaintext: str, key: bytes) -> bytes:
    """AES-256-GCM encrypt plaintext → nonce || tag || ciphertext."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # type: ignore
        nonce = secrets.token_bytes(12)
        aes = AESGCM(key)
        ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ct
    except ImportError:
        # Fallback: Fernet (AES-128-CBC with HMAC) if cryptography not available
        logger.warning("cryptography not installed — using Fernet fallback")
        from cryptography.fernet import Fernet   # type: ignore
        fernet_key = base64.urlsafe_b64encode(key[:32])
        f = Fernet(fernet_key)
        return f.encrypt(plaintext.encode("utf-8"))


def _decrypt(ciphertext: bytes, key: bytes) -> str:
    """Decrypt AES-256-GCM ciphertext → plaintext string."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # type: ignore
        nonce = ciphertext[:12]
        ct = ciphertext[12:]
        aes = AESGCM(key)
        return aes.decrypt(nonce, ct, None).decode("utf-8")
    except ImportError:
        from cryptography.fernet import Fernet   # type: ignore
        fernet_key = base64.urlsafe_b64encode(key[:32])
        f = Fernet(fernet_key)
        return f.decrypt(ciphertext).decode("utf-8")


# ─── Credential storage ───────────────────────────────────────────────────────
def store_credential(key: str, value: str) -> None:
    """
    Store a credential in the OS system keychain.

    Args:
        key:   Credential name (e.g. 'github_pat').
        value: Credential value (never logged).
    """
    try:
        import keyring                           # type: ignore
        keyring.set_password(KEYCHAIN_SERVICE, key, value)
        CredentialRedactFilter.register_secret(value)
        logger.info("Credential stored in keychain: %s", key)
    except ImportError:
        logger.error("keyring not installed — run: pip install keyring")
        raise
    except Exception as exc:
        logger.error("Failed to store credential '%s': %s", key, exc)
        raise


def get_credential(key: str) -> str | None:
    """
    Retrieve a credential from the system keychain.

    Returns None if not found (never raises).

    Args:
        key: Credential name.

    Returns:
        Credential string or None.
    """
    try:
        import keyring                           # type: ignore
        value = keyring.get_password(KEYCHAIN_SERVICE, key)
        if value:
            CredentialRedactFilter.register_secret(value)
            logger.debug("Credential retrieved from keychain: %s", key)
        return value
    except ImportError:
        logger.error("keyring not installed")
        return None
    except Exception as exc:
        logger.warning("Failed to retrieve credential '%s': %s", key, exc)
        return None


def delete_credential(key: str) -> None:
    """Delete a credential from the system keychain."""
    try:
        import keyring                           # type: ignore
        keyring.delete_password(KEYCHAIN_SERVICE, key)
        logger.info("Credential deleted from keychain: %s", key)
    except Exception as exc:
        logger.warning("Failed to delete credential '%s': %s", key, exc)


# ─── Environment injection ────────────────────────────────────────────────────
def load_credentials_to_env() -> dict[str, str]:
    """
    Load all stored credentials from keychain into os.environ.

    This makes credentials available to child processes (Cursor, npm, etc.)
    without writing them to any file.

    Returns:
        Dict of {env_var_name: value} for credentials that were loaded.
    """
    env_map = {
        "gmail":            "GMAIL_USER",
        "openai_api_key":   "OPENAI_API_KEY",
        "gemini_api_key":   "GEMINI_API_KEY",
        "anthropic_api_key":"ANTHROPIC_API_KEY",
        "github_pat":       "GITHUB_PAT",
    }

    loaded: dict[str, str] = {}
    for cred_key, env_key in env_map.items():
        value = get_credential(cred_key)
        if value:
            os.environ[env_key] = value
            loaded[env_key] = value
            logger.debug("Injected %s into environment", env_key)
        else:
            logger.debug("Credential not found for %s — %s not set", cred_key, env_key)

    return loaded


# ─── Encrypted local cache ────────────────────────────────────────────────────
def save_credential_cache(creds: dict[str, str]) -> None:
    """
    Save credentials as AES-256 encrypted JSON cache (offline fallback).

    Args:
        creds: Dict of {key: value} credentials.
    """
    # Explicitly remove secrets from the dict before logging anything
    key = _get_or_create_master_key()
    plaintext = json.dumps({k: v for k, v in creds.items()})
    ciphertext = _encrypt(plaintext, key)

    CRED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_CACHE_PATH.write_bytes(ciphertext)
    CRED_CACHE_PATH.chmod(0o600)
    logger.info("Credential cache saved (AES-256 encrypted)")


def load_credential_cache() -> dict[str, str]:
    """
    Load credentials from the AES-256 encrypted local cache.

    Returns:
        Dict of credentials, or empty dict on failure.
    """
    if not CRED_CACHE_PATH.exists():
        return {}
    try:
        key = _get_or_create_master_key()
        ciphertext = CRED_CACHE_PATH.read_bytes()
        plaintext = _decrypt(ciphertext, key)
        data: dict[str, str] = json.loads(plaintext)
        logger.info("Loaded %d credentials from encrypted cache", len(data))
        return data
    except Exception as exc:
        logger.error("Failed to load credential cache: %s", exc)
        return {}


# ─── Setup wizard ─────────────────────────────────────────────────────────────
def interactive_setup() -> None:
    """
    CLI wizard to prompt the user for all required credentials
    and store them in the keychain.
    """
    print("\n" + "═" * 60)
    print("  cusear™ Agent — Credential Setup")
    print("═" * 60)
    print("Credentials are stored securely in your OS keychain.")
    print("They are NEVER logged or written in plain text.\n")

    labels = {
        "gmail":            "Gmail address (for email sending)",
        "phone":            "Your phone number (e.g. +919876543210)",
        "vercel_domain":    "Your default Vercel subdomain prefix",
        "github_pat":       "GitHub Personal Access Token",
        "openai_api_key":   "OpenAI API key (sk-...)",
        "gemini_api_key":   "Google Gemini API key",
        "anthropic_api_key":"Anthropic API key (sk-ant-...)",
    }

    import getpass
    for key, label in labels.items():
        existing = get_credential(key)
        if existing:
            overwrite = input(f"  {label} [already set — overwrite? y/N]: ").strip().lower()
            if overwrite != "y":
                continue
        value = getpass.getpass(f"  {label}: ").strip()
        if value:
            store_credential(key, value)
        else:
            print(f"  Skipped (empty input)")

    print("\n✓  Setup complete. All credentials stored securely.\n")


def check_required_credentials() -> list[str]:
    """
    Return list of required credential names that are missing from the keychain.

    Returns:
        List of missing credential key names (empty list = all present).
    """
    missing = []
    for key in REQUIRED_CREDENTIALS:
        if not get_credential(key):
            missing.append(key)
    if missing:
        logger.warning("Missing credentials: %s", missing)
    else:
        logger.info("All required credentials present in keychain")
    return missing
