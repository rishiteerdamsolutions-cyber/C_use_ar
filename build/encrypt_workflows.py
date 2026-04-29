"""
Pre-Release Workflow Encryptor
==============================
Run this BEFORE building the client installer.
Encrypts all trained workflow JSON files for a specific client's machine.

Usage
─────
  # Step 1: Get client's machine ID (client runs this on their computer)
  python -m security.license machine-id

  # Step 2: You run this on your machine to encrypt workflows for that client
  python build/encrypt_workflows.py \\
      --machine-id  <client_machine_id_hash> \\
      --client-email  client@example.com \\
      --plan  free \\
      --output-dir  dist/client_name/workflows

  # Step 3: The dist/client_name/ folder is what you give to the client.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Add parent to path so we can import from the project
sys.path.insert(0, str(Path(__file__).parent.parent))

from security.workflow_crypto import encrypt_all_workflows, encrypt_workflow_file
from security.license import generate_license


def encrypt_for_client(
    machine_id:    str,
    client_email:  str,
    plan:          str,
    output_dir:    Path,
    valid_days:    int = 365,
    workflows_dir: Path = Path("workflows"),
    templates_dir: Path = Path("templates"),
    verbose:       bool = True,
) -> Path:
    """
    Full pre-release preparation for one client:
    1. Encrypts all workflow .json → .enc (machine-ID-bound)
    2. Generates a license.key for this client
    3. Copies templates (read-only metadata — safe to include)
    4. Creates dist/<output_dir>/ with everything needed

    Returns the output directory path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    wf_out = output_dir / "workflows"
    wf_out.mkdir(exist_ok=True)

    if verbose:
        print(f"\n{'═'*55}")
        print(f"  cusear™ — Client Build")
        print(f"{'═'*55}")
        print(f"  Client:     {client_email}")
        print(f"  Plan:       {plan}")
        print(f"  Machine ID: {machine_id[:16]}...")
        print(f"  Output:     {output_dir}")
        print(f"{'─'*55}")

    # ── 1. Encrypt workflows ──────────────────────────────────────────────────
    if not workflows_dir.exists():
        if verbose:
            print(f"  ⚠ No workflows/ dir found — skipping workflow encryption")
    else:
        encrypted = encrypt_all_workflows(
            workflows_dir=workflows_dir,
            output_dir=wf_out,
            machine_id=machine_id,
            delete_originals=False,  # Keep originals in YOUR copy
        )
        if verbose:
            for name in encrypted:
                print(f"  ✓ Encrypted  {name}")
            print(f"  → {len(encrypted)} workflow(s) encrypted")

    # ── 2. Generate license key ───────────────────────────────────────────────
    license_key = generate_license(
        client_email=client_email,
        machine_id=machine_id,
        plan=plan,
        valid_days=valid_days,
    )
    (output_dir / "license.key").write_text(license_key)
    if verbose:
        print(f"  ✓ License key generated (valid {valid_days} days)")

    # ── 3. Copy templates (metadata only — no source code) ───────────────────
    if templates_dir.exists():
        dest_templates = output_dir / "templates"
        if dest_templates.exists():
            shutil.rmtree(dest_templates)
        shutil.copytree(templates_dir, dest_templates)
        if verbose:
            count = sum(1 for _ in dest_templates.rglob("template.json"))
            print(f"  ✓ Copied {count} template(s)")

    # ── 4. Write build manifest ───────────────────────────────────────────────
    manifest = {
        "built_at":     datetime.utcnow().isoformat(),
        "client_email": client_email,
        "plan":         plan,
        "machine_id":   machine_id,
        "valid_days":   valid_days,
        "workflows":    [f.name for f in wf_out.glob("*.enc")],
    }
    (output_dir / "build_manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )

    if verbose:
        print(f"{'─'*55}")
        print(f"  ✓ Build complete → {output_dir}")
        print(f"\n  Next steps:")
        print(f"  1. Run: python build/build_client.sh  (to compile .exe)")
        print(f"  2. Copy the .exe + workflows/*.enc + license.key to client")
        print(f"  3. DO NOT include any .py or plain .json workflow files")
        print(f"{'═'*55}\n")

    return output_dir


def batch_encrypt(batch_file: Path) -> None:
    """
    Encrypt workflows for multiple clients at once.

    batch_file format (JSON):
    [
      {
        "email":      "client1@example.com",
        "machine_id": "abc123...",
        "plan":       "free",
        "valid_days": 365
      },
      ...
    ]
    """
    clients = json.loads(batch_file.read_text())
    print(f"Processing {len(clients)} client(s)...")
    for i, client in enumerate(clients, 1):
        print(f"\n[{i}/{len(clients)}]")
        safe_name = client["email"].replace("@", "_").replace(".", "_")
        output_dir = Path("dist") / safe_name
        encrypt_for_client(
            machine_id=client["machine_id"],
            client_email=client["email"],
            plan=client.get("plan", "free"),
            valid_days=client.get("valid_days", 365),
            output_dir=output_dir,
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Encrypt workflows for a client before distribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    # Single client
    single = sub.add_parser("single", help="Prepare build for one client")
    single.add_argument("--machine-id",    required=True, help="Client's machine ID hash")
    single.add_argument("--client-email",  required=True, help="Client's email address")
    single.add_argument("--plan",          default="free", choices=["free", "premium"],
                        help="Client plan (default: free)")
    single.add_argument("--days",          type=int, default=365, help="License valid days")
    single.add_argument("--output-dir",    default=None,
                        help="Output directory (default: dist/<client_email>)")
    single.add_argument("--workflows-dir", default="workflows",
                        help="Source workflows directory")

    # Batch
    batch = sub.add_parser("batch", help="Process multiple clients from a JSON file")
    batch.add_argument("batch_file", help="JSON file with client list")

    args = parser.parse_args()

    if args.cmd == "single":
        safe = args.client_email.replace("@", "_").replace(".", "_")
        out  = Path(args.output_dir) if args.output_dir else Path("dist") / safe
        encrypt_for_client(
            machine_id=args.machine_id,
            client_email=args.client_email,
            plan=args.plan,
            valid_days=args.days,
            output_dir=out,
            workflows_dir=Path(args.workflows_dir),
        )

    elif args.cmd == "batch":
        batch_encrypt(Path(args.batch_file))

    else:
        parser.print_help()
