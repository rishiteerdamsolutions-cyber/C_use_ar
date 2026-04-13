"""
Main Orchestrator — Autonomous Web Agency Agent v1.0
Entry point: startup checks → accept command → execute workflow → deliver URL.

Usage:
    python main.py                                      # normal agent mode
    python main.py --setup                              # run credential wizard
    python main.py --check                              # system health check
    python main.py --teach                              # screenshot teaching mode
    python main.py --run-workflow <name>                # replay a taught workflow
    python main.py --run-workflow <name> --dry-run      # preview without executing
    python main.py --run-workflow <name> --var K=V      # replay with variable substitution
    python main.py --list-workflows                     # list all saved workflows
    python main.py --show-workflow <name>               # print workflow steps
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ─── Structured logging setup ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8"))
except OSError:
    # Serverless runtimes can have readonly application filesystems.
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("main")


def _primary_mod_key() -> str:
    import platform
    return "command" if platform.system() == "Darwin" else "ctrl"

# Add credential redact filter to root logger
try:
    from security.credentials import CredentialRedactFilter
    logging.getLogger().addFilter(CredentialRedactFilter())
except Exception:
    pass


# ─── License check (FIRST thing that runs in production) ─────────────────────
def _check_license() -> dict | None:
    """
    Validate license.key on startup.
    In development mode (APP_MODE=development) this is skipped.
    In production any failure hard-exits with a user-friendly message.
    """
    import os
    if os.environ.get("APP_MODE", "production") == "development":
        logger.info("Development mode — license check skipped")
        return None

    try:
        from security.license import validate_license, LicenseError
        payload = validate_license()
        logger.info(
            "License valid · %s · plan=%s · expires=%s",
            payload.get("email"),
            payload.get("plan"),
            payload.get("expires_at", "")[:10],
        )
        return payload
    except ImportError:
        # cryptography library not installed — dev environment
        logger.warning("License module unavailable — skipping check")
        return None
    except Exception as exc:  # LicenseError or unexpected
        print("\n" + "═" * 58)
        print("  LICENSE ERROR")
        print("─" * 58)
        print(f"\n  {exc}\n")
        print("═" * 58 + "\n")
        sys.exit(1)


# ─── Startup banner ───────────────────────────────────────────────────────────
BANNER = """
╔══════════════════════════════════════════════════════════════╗
║    Autonomous Web Agency Agent  v1.0  · Karimnagar, TG       ║
║    One command → complete website delivered to client         ║
╚══════════════════════════════════════════════════════════════╝
"""


# ─── Template matcher ─────────────────────────────────────────────────────────
def _match_template(command: str) -> dict[str, Any] | None:
    """
    Match a user command against the template library.

    Searches templates/*/template.json for keyword matches.

    Args:
        command: User's natural-language command.

    Returns:
        Template dict if matched, None otherwise.
    """
    import json as _json

    templates_dir = BASE_DIR / "templates"
    if not templates_dir.exists():
        return None

    command_lower = command.lower()

    for tmpl_file in templates_dir.glob("*/template.json"):
        try:
            tmpl = _json.loads(tmpl_file.read_text(encoding="utf-8"))
            keywords: list[str] = tmpl.get("keywords", [])
            if any(kw.lower() in command_lower for kw in keywords):
                logger.info(
                    "Template matched: %s (via keywords=%s)",
                    tmpl.get("template_id"), keywords,
                )
                tmpl["_prompt_file"] = str(tmpl_file.parent / "prompt.txt")
                return tmpl
        except Exception as exc:
            logger.warning("Could not load template %s: %s", tmpl_file, exc)

    return None


# ─── OTP checkpoint ───────────────────────────────────────────────────────────
def _wait_for_otp_confirmation(platform: str = "Vercel") -> None:
    """
    Pause the workflow and wait for the user to complete an OTP step.
    Shows a tkinter popup; the user presses Continue when done.
    """
    logger.info("OTP checkpoint reached for %s — awaiting human", platform)

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        messagebox.showinfo(
            title=f"OTP Required — {platform}",
            message=(
                f"📱  {platform} has sent an OTP to your phone.\n\n"
                "Please:\n"
                "  1. Enter the OTP in the browser\n"
                "  2. Complete any verification steps\n"
                "  3. Click OK here when done\n\n"
                "The agent will automatically resume."
            ),
            parent=root,
        )
        root.destroy()
        logger.info("OTP checkpoint cleared — resuming workflow")

    except Exception:
        # Fallback to CLI prompt if tkinter unavailable
        input(f"\n⚠️  OTP required for {platform}. Complete it, then press ENTER to continue...\n")


# ─── Delivery notification ────────────────────────────────────────────────────
def _send_delivery_notification(
    live_url: str,
    client_info: dict[str, Any],
    config: dict[str, Any],
) -> None:
    """
    Notify the client that their website is live.
    Tries Gmail API, then WhatsApp, then falls back to console print.
    """
    client_name = client_info.get("name", "Client")
    message = (
        f"🎉 Hi {client_name}! Your website is LIVE!\n\n"
        f"URL: {live_url}\n\n"
        "Please review and let us know your feedback.\n"
        "— Autonomous Web Agency"
    )

    logger.info("Sending delivery notification to %s", client_name)

    # WhatsApp via wa.me link (opens browser)
    wa_number = os.environ.get("NEXT_PUBLIC_WHATSAPP_NUMBER", "")
    if wa_number:
        import urllib.parse
        import webbrowser
        encoded = urllib.parse.quote(message)
        webbrowser.open(f"https://wa.me/{wa_number}?text={encoded}")
        logger.info("WhatsApp delivery message opened in browser")
        return

    # Console fallback
    print(f"\n{'═'*60}")
    print("  ✅  DELIVERY COMPLETE")
    print(f"  URL: {live_url}")
    print(f"  Client: {client_name}")
    print(f"{'═'*60}\n")


# ─── Full workflow executor ───────────────────────────────────────────────────
def run_website_workflow(
    command: str,
    template: dict[str, Any],
    config: dict[str, Any],
    dry_run: bool = False,
) -> str | None:
    """
    Execute the full website delivery pipeline.

    Steps:
      1.  Validate prompt (GPT ↔ Gemini loop)
      2.  Open Cursor, paste validated prompt
      3.  Wait for Cursor to complete build
      4.  Create GitHub repo via API
      5.  Push code to GitHub
      6.  Create MongoDB cluster + get connection string
      7.  Create Vercel project + link repo  [OTP checkpoint]
      8.  Set all ENV variables in Vercel
      9.  Copy live URL
      10. Send to client

    Args:
        command:  Original user command.
        template: Matched template dict.
        config:   Remote config dict.
        dry_run:  If True, skip actual GUI automation (testing mode).

    Returns:
        Live URL string, or None if workflow failed.
    """
    from analytics.session import SessionRecorder
    from ai.validator import validate_prompt
    from execution.executor import ExecutionEngine
    from execution.fallback import execute_with_fallback
    from security.credentials import get_credential

    client_email = get_credential("gmail") or "client@example.com"
    recorder = SessionRecorder(
        user_email=client_email,
        workflow_name=template.get("template_id", "custom"),
    )

    live_url: str = ""

    try:
        executor = ExecutionEngine() if not dry_run else None

        # ── Step 1: Validate prompt ────────────────────────────────────────────
        t0 = time.time()
        prompt_file = template.get("_prompt_file", "")
        initial_prompt = command
        if prompt_file and Path(prompt_file).exists():
            initial_prompt = Path(prompt_file).read_text(encoding="utf-8")

        logger.info("Step 1: Validating prompt via GPT↔Gemini loop")
        try:
            result = validate_prompt(initial_prompt, max_iterations=3)
            validated_prompt = result["validated_prompt"]
            recorder.log_step(
                "validate_prompt",
                "SUCCESS",
                time.time() - t0,
                method=f"iterations={result['iterations']}",
            )
        except Exception as exc:
            logger.warning("Prompt validation failed — using original: %s", exc)
            validated_prompt = initial_prompt
            recorder.log_step("validate_prompt", "FAILURE", time.time() - t0, "exception", str(exc))

        if dry_run:
            logger.info("[DRY RUN] Skipping GUI steps")
            recorder.log_step("dry_run_skip", "SKIP", 0, "dry_run")
            live_url = "https://dry-run-placeholder.vercel.app"
            recorder.save_session(live_url)
            return live_url

        # ── Step 2: Cursor — create project + paste prompt ────────────────────
        t0 = time.time()
        logger.info("Step 2: Opening Cursor AI")
        fb_result = execute_with_fallback(
            platform="cursor",
            action_key="open_new_project",
            config=config,
            executor=executor,
        )
        recorder.log_step("cursor_open_project", "SUCCESS" if fb_result.success else "FAILURE",
                          time.time() - t0, fb_result.method, fb_result.error)

        if fb_result.success:
            t0 = time.time()
            executor.set_clipboard(validated_prompt)
            executor.shortcut(_primary_mod_key(), "a")
            executor.paste()
            executor.press_enter()
            recorder.log_step("cursor_paste_prompt", "SUCCESS", time.time() - t0, "clipboard")

            # Monitor for completion (10-min intervals)
            _wait_for_cursor_completion(executor, recorder)

        # ── Step 3: GitHub — create repo via API ─────────────────────────────
        t0 = time.time()
        logger.info("Step 3: Creating GitHub repository")
        repo_url = _create_github_repo(
            template_id=template.get("template_id", "website"),
            github_pat=get_credential("github_pat") or "",
        )
        if repo_url:
            recorder.log_step("github_create_repo", "SUCCESS", time.time() - t0, "api", metadata={"repo_url": repo_url})
        else:
            recorder.log_step("github_create_repo", "FAILURE", time.time() - t0, "api", "API call failed")

        # ── Step 4: MongoDB Atlas — get connection string ─────────────────────
        t0 = time.time()
        logger.info("Step 4: Setting up MongoDB Atlas connection")
        mongo_uri = os.environ.get("MONGODB_URI", "")
        if not mongo_uri:
            logger.warning("MONGODB_URI not set — skipping MongoDB setup")
            recorder.log_step("mongodb_setup", "SKIP", time.time() - t0, "env_missing")
        else:
            recorder.log_step("mongodb_setup", "SUCCESS", time.time() - t0, "env_var")

        # ── Step 5: Vercel — create project + OTP ────────────────────────────
        t0 = time.time()
        logger.info("Step 5: Deploying to Vercel")
        fb_result = execute_with_fallback(
            platform="vercel",
            action_key="create_new_project",
            config=config,
            executor=executor,
        )
        recorder.log_step("vercel_create_project",
                          "SUCCESS" if fb_result.success else "FAILURE",
                          time.time() - t0, fb_result.method, fb_result.error)

        # OTP checkpoint
        _wait_for_otp_confirmation("Vercel")

        # ── Step 6: Vercel ENV variables ──────────────────────────────────────
        t0 = time.time()
        logger.info("Step 6: Setting Vercel ENV variables")
        env_vars = {
            "MONGODB_URI":             mongo_uri,
            "NEXT_PUBLIC_SITE_NAME":   template.get("display_name", "My Website"),
        }
        _set_vercel_env_vars(env_vars, config, executor, recorder)
        recorder.log_step("vercel_set_env", "SUCCESS", time.time() - t0, "gui_automation")

        # ── Step 7: Get live URL ──────────────────────────────────────────────
        t0 = time.time()
        domain = get_credential("vercel_domain") or "my-site"
        live_url = f"https://{domain}.vercel.app"
        logger.info("Step 7: Live URL → %s", live_url)
        recorder.log_step("get_live_url", "SUCCESS", time.time() - t0, "direct",
                          metadata={"url": live_url})

    except Exception as exc:
        logger.error("Workflow failed with unhandled exception: %s", exc, exc_info=True)
        recorder.log_step("workflow_exception", "FAILURE", 0, "exception", str(exc))

    finally:
        session_path = recorder.save_session(live_url)
        logger.info("Session saved: %s  success_rate=%.1f%%",
                    session_path.name, recorder.calculate_success_rate() * 100)

    return live_url or None


# ─── GitHub API helper ────────────────────────────────────────────────────────
def _create_github_repo(template_id: str, github_pat: str) -> str:
    """Create a GitHub repo via API and return its URL."""
    import requests as _req
    import re
    import time as _time

    if not github_pat:
        logger.warning("GitHub PAT not set — cannot create repo via API")
        return ""

    repo_name = re.sub(r"[^a-z0-9-]", "-", template_id.lower()) + f"-{int(_time.time())}"

    try:
        resp = _req.post(
            "https://api.github.com/user/repos",
            json={"name": repo_name, "private": False, "auto_init": True},
            headers={"Authorization": f"token {github_pat}", "Accept": "application/vnd.github.v3+json"},
            timeout=15,
        )
        resp.raise_for_status()
        url: str = resp.json().get("html_url", "")
        logger.info("GitHub repo created: %s", url)
        return url
    except Exception as exc:
        logger.error("GitHub repo creation failed: %s", exc)
        return ""


# ─── Cursor monitoring ────────────────────────────────────────────────────────
def _wait_for_cursor_completion(executor: Any, recorder: Any) -> None:
    """Poll Cursor output frequently with bounded timeout."""
    COMPLETION_KEYWORDS = ["successfully", "done", "complete", "ready", "built"]
    ERROR_KEYWORDS = ["error", "failed", "exception", "cannot", "undefined"]
    poll_seconds = int(os.environ.get("CURSOR_POLL_SECONDS", "30") or "30")
    max_wait_seconds = int(os.environ.get("CURSOR_MAX_WAIT_SECONDS", "1800") or "1800")
    max_polls = max(1, max_wait_seconds // max(1, poll_seconds))

    logger.info("Monitoring Cursor for completion (max %ds, poll=%ds)…", max_wait_seconds, poll_seconds)

    for poll in range(max_polls):
        time.sleep(poll_seconds)
        text = executor.copy_all_text().lower()

        if any(kw in text for kw in COMPLETION_KEYWORDS):
            logger.info("Cursor completion detected on poll %d", poll + 1)
            recorder.log_step("cursor_build_wait", "SUCCESS", poll * poll_seconds, "polling",
                              metadata={"polls": poll + 1})
            return

        if any(kw in text for kw in ERROR_KEYWORDS):
            logger.warning("Cursor error detected — retrying prompt")
            executor.shortcut(_primary_mod_key(), "z")
            time.sleep(2)
            executor.press_enter()

    recorder.log_step("cursor_build_wait", "FAILURE", max_polls * poll_seconds, "polling", "Timeout")


# ─── Vercel ENV ───────────────────────────────────────────────────────────────
def _set_vercel_env_vars(
    env_vars: dict[str, str],
    config: dict[str, Any],
    executor: Any,
    recorder: Any,
) -> None:
    """Set each ENV variable in Vercel dashboard via GUI automation."""
    from execution.fallback import execute_with_fallback

    for key, value in env_vars.items():
        if not value:
            continue
        t0 = time.time()
        fb_result = execute_with_fallback(
            platform="vercel",
            action_key="add_env_variable",
            config=config,
            executor=executor,
        )
        if fb_result.success:
            executor.type_text(key, clear_first=True)
            executor.shortcut("tab")
            executor.type_text(value, clear_first=True)
            executor.press_enter()
        recorder.log_step(
            f"vercel_env_{key}",
            "SUCCESS" if fb_result.success else "FAILURE",
            time.time() - t0,
            fb_result.method,
            fb_result.error,
        )


# ─── Health check ─────────────────────────────────────────────────────────────
def health_check() -> None:
    """Print a system health report."""
    from security.credentials import check_required_credentials, REQUIRED_CREDENTIALS

    print("\n" + "═" * 60)
    print("  Agent Health Check")
    print("═" * 60)

    # Python version
    print(f"  Python: {sys.version.split()[0]}")

    # Required packages
    packages = ["pyautogui", "pyperclip", "anthropic", "requests", "keyring", "cryptography"]
    for pkg in packages:
        try:
            __import__(pkg)
            print(f"  ✓ {pkg}")
        except ImportError:
            print(f"  ✗ {pkg}  (MISSING — run: pip install {pkg})")

    # Credentials
    missing = check_required_credentials()
    for cred in REQUIRED_CREDENTIALS:
        mark = "✗ MISSING" if cred in missing else "✓"
        print(f"  {mark} credential: {cred}")

    # Remote config
    try:
        from config.remote_config import fetch_remote_config
        cfg = fetch_remote_config()
        print(f"  ✓ Remote config (version={cfg.get('version', 'unknown')})")
    except Exception as exc:
        print(f"  ✗ Remote config: {exc}")

    print("═" * 60 + "\n")


# ─── Main entry point ─────────────────────────────────────────────────────────
def main() -> None:
    """Application entry point."""
    parser = argparse.ArgumentParser(description="Autonomous Web Agency Agent")
    parser.add_argument("--setup",          action="store_true", help="Run credential setup wizard")
    parser.add_argument("--check",          action="store_true", help="Run system health check")
    parser.add_argument("--dry-run",        action="store_true", help="Test workflow without GUI automation")
    parser.add_argument("--teach",          action="store_true", help="Open screenshot teaching mode")
    parser.add_argument("--run-workflow",   metavar="NAME",       help="Replay a taught workflow by name")
    parser.add_argument("--list-workflows", action="store_true", help="List all saved workflows")
    parser.add_argument("--show-workflow",  metavar="NAME",       help="Print steps of a saved workflow")
    parser.add_argument("--var",            action="append",      metavar="KEY=VALUE",
                        help="Variable substitution for workflow replay (repeatable)")
    parser.add_argument("--mode",           choices=["fast","smart"], default="smart",
                        help="fast=Training-Only (V1, no API), smart=Training+AI (V2, Claude Vision)")
    args = parser.parse_args()

    print(BANNER)

    # ── LICENSE CHECK — must pass before anything else ────────────────────────
    license_payload = _check_license()
    plan = (license_payload or {}).get("plan", "free")

    # ── Credential setup mode ─────────────────────────────────────────────────
    if args.setup:
        from security.credentials import interactive_setup
        interactive_setup()
        return

    # ── Health check mode ─────────────────────────────────────────────────────
    if args.check:
        health_check()
        return

    # ── List workflows ────────────────────────────────────────────────────────
    if args.list_workflows:
        from teach.workflow_runner import list_workflows
        wfs = list_workflows()
        if wfs:
            print("\nSaved workflows:")
            for w in wfs:
                print(f"  • {w}")
            print(f"\nRun one:  python main.py --run-workflow <name>\n")
        else:
            print("\nNo workflows saved yet.\nRun:  python main.py --teach\n")
        return

    # ── Show workflow steps ───────────────────────────────────────────────────
    if args.show_workflow:
        from teach.workflow_runner import print_workflow_summary
        print_workflow_summary(args.show_workflow)
        return

    # ── Teach mode ────────────────────────────────────────────────────────────
    if args.teach:
        from teach.teach_cli import run_teach_cli
        run_teach_cli()
        return

    # ── Run a taught workflow ─────────────────────────────────────────────────
    if args.run_workflow:
        # Parse --var KEY=VALUE pairs
        variables: dict[str, str] = {}
        if args.var:
            for item in args.var:
                if "=" in item:
                    k, v = item.split("=", 1)
                    variables[f"{{{{{k}}}}}"] = v   # e.g. {{CLIENT_NAME}} → value

        mode = getattr(args, "mode", "smart")

        if mode == "fast":
            # ── V1: Training Only — zero API calls, saved coordinates ─────────
            from teach.runner_v1 import RunnerV1
            runner = RunnerV1(args.run_workflow, dry_run=args.dry_run)
        else:
            # ── V2: Training + AI — Claude Vision re-checks every step ────────
            # Premium plan required
            if plan == "free":
                print("\n" + "═" * 58)
                print("  UPGRADE REQUIRED")
                print("─" * 58)
                print("  Smart (AI) mode requires the Premium plan.")
                print("  Your current plan: FREE")
                print("")
                print("  Upgrade at: https://yourplatform.com/upgrade")
                print("  Or run with --mode fast (free, uses saved coords)")
                print("═" * 58 + "\n")
                sys.exit(1)
            from teach.workflow_runner import WorkflowRunner
            runner = WorkflowRunner(args.run_workflow, dry_run=args.dry_run)

        success = runner.run(variables=variables)
        sys.exit(0 if success else 1)

    # ── Normal startup ─────────────────────────────────────────────────────────
    logger.info("Agent starting up…")

    # 1. Load credentials into environment
    try:
        from security.credentials import load_credentials_to_env
        loaded = load_credentials_to_env()
        logger.info("Loaded %d credentials from keychain", len(loaded))
    except Exception as exc:
        logger.warning("Credential load failed: %s", exc)

    # 2. Fetch remote config
    config: dict[str, Any] = {}
    try:
        from config.remote_config import fetch_remote_config
        config = fetch_remote_config()
        logger.info("Remote config loaded (version=%s)", config.get("version", "?"))
    except Exception as exc:
        logger.warning("Remote config unavailable: %s — using defaults", exc)
        config = {}

    # 3. Check for updates
    try:
        from updater.auto_update import check_for_updates, show_update_banner, download_and_apply_update
        update_info = check_for_updates()
        if update_info.get("update_available"):
            if show_update_banner(update_info):
                download_and_apply_update(
                    update_info["download_url"],
                    update_info["version"],
                )
    except Exception as exc:
        logger.warning("Update check failed: %s", exc)

    # 4. Main command loop
    print("\nType your command (e.g. 'build salon website for client')")
    print("Type 'quit' or 'exit' to stop.\n")

    while True:
        try:
            command = input("Agency Agent ▶  ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nShutting down. Goodbye!")
            break

        if not command:
            continue
        if command.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        logger.info("User command: %s", command)

        # Match template
        template = _match_template(command)
        if template is None:
            print(
                f"\nNo template matched for: '{command}'\n"
                "Available keywords: salon, beauty, restaurant, portfolio, ecommerce…\n"
            )
            continue

        print(f"\n✓ Template matched: {template.get('display_name', template.get('template_id'))}")
        print(f"  Estimated time: {template.get('estimated_build_minutes', '?')} minutes")
        print(f"  Stack: {template.get('tech_stack', {}).get('framework', '?')}")
        print("\nStarting workflow…\n")

        # Run workflow
        live_url = run_website_workflow(
            command=command,
            template=template,
            config=config,
            dry_run=args.dry_run,
        )

        if live_url:
            print(f"\n🎉  Website delivered successfully!")
            print(f"    URL: {live_url}\n")

            # Notify client
            from security.credentials import get_credential
            client_name = command.split("for")[-1].strip().title() if "for" in command else "client"
            _send_delivery_notification(
                live_url=live_url,
                client_info={"name": client_name},
                config=config,
            )
        else:
            print("\n⚠️  Workflow completed with errors. Check logs/agent.log for details.\n")


if __name__ == "__main__":
    main()
