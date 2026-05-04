"""
Workflow Encryption — cusear™ Platform
=====================================================
Encrypts trained workflow JSON files before distributing to clients.

Key design decisions
────────────────────
1. Each workflow file is encrypted with AES-256-GCM.

2. The encryption key is derived from TWO things combined:
     - Your WORKFLOW_MASTER_KEY  (embedded in the binary — you own this)
     - The client's machine_id   (from license.py — unique per computer)
   This means even if a client extracts the .enc file and sends it to
   a friend, it won't decrypt on the friend's machine.

3. Decryption happens ONLY IN MEMORY at runtime.
   Decrypted JSON is never written back to disk.

4. File format: [4-byte magic] [1-byte version] [nonce] [ciphertext+tag]
   Magic = b'AWAF'  (cusear™ Flow)

Usage
─────
  # Encrypt all workflows before building the client installer:
  python build/encrypt_workflows.py

  # In your runner code, load a workflow:
  from security.workflow_crypto import load_workflow
  wf = load_workflow("workflows/salon_build.enc", machine_id)
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Any

# Try cryptography first, fall back to pycryptodome
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    _LIB = "cryptography"
except ImportError:
    try:
        from Crypto.Cipher import AES as _AES       # type: ignore
        from Crypto.Protocol.KDF import HKDF as _HKDF  # type: ignore
        from Crypto.Hash import SHA256 as _SHA256    # type: ignore
        _LIB = "pycryptodome"
    except ImportError:
        _LIB = "none"

# ─── File format constants ────────────────────────────────────────────────────
MAGIC         = b"AWAF"          # cusear™ Flow
FORMAT_VER    = 1
NONCE_SIZE    = 12               # AES-GCM nonce
HEADER_SIZE   = len(MAGIC) + 1  # 5 bytes: magic + version


# ─── Master key ───────────────────────────────────────────────────────────────

def _workflow_master_key() -> bytes:
    """
    32-byte master key — set via WORKFLOW_MASTER_KEY env var.
    Embedded at build time. NEVER share this.
    Generate with: python -c "import os,binascii; print(binascii.hexlify(os.urandom(32)).decode())"
    """
    raw = os.environ.get("WORKFLOW_MASTER_KEY", "")
    if raw:
        key = bytes.fromhex(raw) if len(raw) == 64 else raw.encode()
        return key[:32].ljust(32, b"\x00")
    # Dev fallback — REPLACE BEFORE BUILD
    return b"WF_DEV_KEY_REPLACE_BEFORE_BUILD!"


# ─── Key derivation ───────────────────────────────────────────────────────────

def _derive_key(machine_id: str) -> bytes:
    """
    Derive a 32-byte AES key specific to this machine.
    Key = HKDF(master_key, salt=machine_id, info=b"workflow-enc")
    Different machine → completely different key → .enc file is useless elsewhere.
    """
    master = _workflow_master_key()
    salt   = machine_id.encode()[:32]
    info   = b"workflow-enc-v1"

    if _LIB == "cryptography":
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=info,
        ).derive(master)

    elif _LIB == "pycryptodome":
        return _HKDF(
            master=master,
            key_len=32,
            salt=salt,
            hashmod=_SHA256,
            context=info,
            num_keys=1,
        )

    else:
        # Pure Python fallback — less secure but works without any library
        combined = master + salt + info
        return hashlib.sha256(combined).digest()


# ─── AES-GCM encrypt / decrypt ───────────────────────────────────────────────

def _encrypt_bytes(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(NONCE_SIZE)
    if _LIB == "cryptography":
        ct = AESGCM(key).encrypt(nonce, plaintext, None)
        return nonce + ct
    elif _LIB == "pycryptodome":
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        return nonce + ct + tag
    else:
        raise RuntimeError("No crypto library. pip install cryptography")


def _decrypt_bytes(blob: bytes, key: bytes) -> bytes:
    nonce   = blob[:NONCE_SIZE]
    payload = blob[NONCE_SIZE:]
    if _LIB == "cryptography":
        return AESGCM(key).decrypt(nonce, payload, None)
    elif _LIB == "pycryptodome":
        ct, tag = payload[:-16], payload[-16:]
        cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    else:
        raise RuntimeError("No crypto library.")


# ─── Public API ───────────────────────────────────────────────────────────────

def encrypt_workflow(workflow_json: dict, machine_id: str) -> bytes:
    """
    Encrypt a workflow dict and return raw bytes ready to write to a .enc file.

    Args:
        workflow_json:  The plain workflow dict (from teach/*.json)
        machine_id:     Client's machine ID from license.get_machine_id()

    Returns:
        Encrypted bytes with header: MAGIC + VERSION + encrypted_payload
    """
    plaintext = json.dumps(workflow_json, ensure_ascii=False).encode()
    key       = _derive_key(machine_id)
    encrypted = _encrypt_bytes(plaintext, key)
    header    = MAGIC + struct.pack("B", FORMAT_VER)
    return header + encrypted


def decrypt_workflow(enc_bytes: bytes, machine_id: str) -> dict:
    """
    Decrypt an encrypted workflow file back to a dict IN MEMORY.
    Never writes the decrypted content to disk.

    Raises:
        ValueError: if magic bytes don't match (wrong file type)
        Exception:  if decryption fails (wrong machine / tampered file)
    """
    # Check header
    if len(enc_bytes) < HEADER_SIZE:
        raise ValueError("File too small to be a valid .enc workflow")
    if enc_bytes[:4] != MAGIC:
        raise ValueError(f"Not a valid workflow file (bad magic bytes)")
    version = struct.unpack("B", enc_bytes[4:5])[0]
    if version != FORMAT_VER:
        raise ValueError(f"Unsupported workflow format version: {version}")

    payload   = enc_bytes[HEADER_SIZE:]
    key       = _derive_key(machine_id)
    plaintext = _decrypt_bytes(payload, key)
    return json.loads(plaintext.decode())


def encrypt_workflow_file(
    source_json_path: Path,
    dest_enc_path:    Path,
    machine_id:       str,
) -> None:
    """Encrypt a single workflow JSON file → .enc file."""
    wf = json.loads(source_json_path.read_text())
    encrypted = encrypt_workflow(wf, machine_id)
    dest_enc_path.write_bytes(encrypted)


def decrypt_workflow_file(enc_path: Path, machine_id: str) -> dict:
    """Load and decrypt a .enc workflow file → dict (in memory only)."""
    return decrypt_workflow(enc_path.read_bytes(), machine_id)


def load_workflow(path: str | Path, machine_id: str) -> dict:
    """
    Convenience loader used by the runners.
    Accepts either .json (dev mode) or .enc (production/client mode).
    """
    p = Path(path)

    if p.suffix == ".enc":
        return decrypt_workflow_file(p, machine_id)

    # Plain JSON — only allowed in dev mode
    if os.environ.get("APP_MODE", "production") == "development":
        return json.loads(p.read_text())

    # In production, plain JSON is rejected
    raise RuntimeError(
        f"Workflow {p.name} is not encrypted. "
        "This build only accepts .enc workflow files."
    )


def encrypt_all_workflows(
    workflows_dir: Path,
    output_dir:    Path,
    machine_id:    str,
    *,
    delete_originals: bool = False,
) -> list[str]:
    """
    Encrypt every .json workflow in workflows_dir → output_dir as .enc files.

    Args:
        workflows_dir:     Directory containing plain .json workflow files
        output_dir:        Where to write .enc files
        machine_id:        Target client's machine ID
        delete_originals:  If True, delete source .json files after encryption
                           (use this when building the client distribution)

    Returns:
        List of encrypted file names
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    encrypted = []

    for json_path in sorted(workflows_dir.glob("*.json")):
        enc_name = json_path.stem + ".enc"
        enc_path = output_dir / enc_name
        encrypt_workflow_file(json_path, enc_path, machine_id)
        encrypted.append(enc_name)

        if delete_originals:
            json_path.unlink()

    return encrypted


# ─── Integrity check ─────────────────────────────────────────────────────────

def verify_workflow_integrity(enc_path: Path, machine_id: str) -> bool:
    """
    Quick check: can this .enc file be decrypted by this machine?
    Returns True/False without raising.
    Used at startup to pre-validate all workflow files.
    """
    try:
        decrypt_workflow_file(enc_path, machine_id)
        return True
    except Exception:
        return False


def startup_integrity_check(workflows_dir: Path, machine_id: str) -> dict[str, bool]:
    """
    Check all .enc files in workflows_dir at app startup.
    Returns {filename: is_valid} for every .enc found.
    """
    results = {}
    for enc in sorted(workflows_dir.glob("*.enc")):
        results[enc.name] = verify_workflow_integrity(enc, machine_id)
    return results
