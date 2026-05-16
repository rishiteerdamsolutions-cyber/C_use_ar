#!/usr/bin/env python3
"""
cusear™ Local Agent — WebSocket control plane + local WRA execution.

Usage:
  export CUSEAR_WS_BASE=wss://api.cusear.autos   # optional
  python3 cusear_agent.py YOUR_AGENT_TOKEN

Requires repo root on PYTHONPATH (run from repo root) and full ``requirements.txt`` for WRA.
Also: ``pip install websockets`` if not already installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

try:
    import websockets
except ImportError:
    print("Missing dependency: pip install websockets")
    sys.exit(1)

AGENT_VERSION = "1.0.0"
LOCAL_DIR = Path(os.environ.get("CUSEAR_LOCAL_DIR") or (Path.home() / ".cusear"))


def _repo_root() -> str:
    env = (os.environ.get("CUSEAR_REPO_ROOT") or "").strip()
    if env:
        return env
    here = Path(__file__).resolve().parent
    for p in (here, *here.parents):
        if (p / "cusear" / "engine" / "wra_engine.py").is_file():
            return str(p)
    return str(here)


def _ws_url(token: str) -> str:
    base = (os.environ.get("CUSEAR_WS_BASE") or "wss://api.cusear.autos").rstrip("/")
    return f"{base}/agent/ws/{quote(token, safe='')}"


def check_chrome() -> bool:
    system = platform.system()
    if system == "Darwin":
        return os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if system == "Windows":
        return os.path.exists(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    return False


def list_local_workflows() -> list[str]:
    d = LOCAL_DIR / "workflows"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def list_local_products() -> list[str]:
    d = LOCAL_DIR / "products"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def _normalize_plan(raw: Any) -> str | None:
    s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "core": "core",
        "hybrid": "hybrid",
        "ai_budget": "ai_budget",
        "aibudget": "ai_budget",
        "budget": "ai_budget",
        "ai_pro": "ai_pro",
        "aipro": "ai_pro",
        "pro": "ai_pro",
    }
    return aliases.get(s)


def _normalize_media(raw: Any, *, content_key_hint: str = "") -> str | None:
    s = str(raw or "").strip().lower()
    aliases = {
        "text": "text",
        "texts": "text",
        "caption": "text",
        "post_text": "text",
        "image": "image",
        "images": "image",
        "photo": "image",
        "video": "video",
        "videos": "video",
    }
    media = aliases.get(s)
    if media:
        return media
    hint = content_key_hint.strip().lower()
    if any(tok in hint for tok in ("video", "reel", "mp4")):
        return "video"
    if any(tok in hint for tok in ("image", "photo", "png", "jpg", "jpeg")):
        return "image"
    if any(tok in hint for tok in ("text", "caption", "copy", "post")):
        return "text"
    return None


def _normalize_platform(raw: Any) -> str | None:
    s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "facebook": "facebook",
        "instagram": "instagram",
        "linkedin": "linkedin",
        "x": "x",
        "twitter": "x",
        "whatsapp": "whatsapp",
        "wa": "whatsapp",
    }
    return aliases.get(s)


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resolve_storage_slot_value(content_key: str, value: dict[str, Any]) -> Any:
    """
    Resolve a content_map slot descriptor into text/path using storage_vault.slot_path().

    Expected payload (minimal): {"plan": "...", "day": 5, "media": "text|image|video", "platform": "...?"}
    """
    from cusear.storage_vault import slot_path

    plan = _normalize_plan(value.get("plan"))
    media = _normalize_media(value.get("media"), content_key_hint=content_key)
    platform = _normalize_platform(value.get("platform"))
    day_raw = value.get("day")
    try:
        day = int(day_raw)
    except (TypeError, ValueError):
        return value

    if not plan or not media:
        return value
    if day < 1:
        raise ValueError(f"Invalid day for content key '{content_key}': {day}")
    if plan == "ai_pro" and not platform:
        raise ValueError(f"Missing platform for ai_pro content key '{content_key}'")

    path = slot_path(
        None,
        plan=cast(Any, plan),
        media=cast(Any, media),
        day=day,
        platform=cast(Any, platform),
    )
    if media == "text":
        if not path.is_file():
            raise FileNotFoundError(f"Text slot not found: {path}")
        return _read_text_file(path)
    # For image/video slots, engine expects a filesystem path string.
    return str(path)


def _resolve_content_map_value(content_key: str, value: Any) -> Any:
    if isinstance(value, dict):
        # Support direct descriptor or nested storage_ref descriptor.
        if isinstance(value.get("storage_ref"), dict):
            return _resolve_storage_slot_value(content_key, value["storage_ref"])
        if any(k in value for k in ("plan", "day", "media", "platform")):
            return _resolve_storage_slot_value(content_key, value)
        return value
    if isinstance(value, str) and os.path.isfile(value):
        with open(value, "r", encoding="utf-8") as f:
            return f.read()
    return value


class CusearAgent:
    def __init__(self, token: str) -> None:
        self.token = token
        self.ws: Any = None
        self.local_dir = LOCAL_DIR
        self.local_dir.mkdir(parents=True, exist_ok=True)

    async def send(self, data: dict[str, Any]) -> None:
        if self.ws:
            await self.ws.send(json.dumps(data))

    async def connect(self) -> None:
        url = _ws_url(self.token)
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=120) as ws:
                    self.ws = ws
                    await self.on_connected()
                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        await self.on_message(data)
            except Exception as exc:
                print(f"[Agent] Disconnected: {exc} — reconnecting in 10s")
                await asyncio.sleep(10)
            finally:
                self.ws = None

    async def on_connected(self) -> None:
        await self.send(
            {
                "type": "agent_hello",
                "version": AGENT_VERSION,
                "os": platform.system(),
                "chrome": check_chrome(),
                "status": "ready",
            }
        )
        print("[Agent] Connected.")

    async def on_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "run_workflow":
            await self.handle_run(msg)
        elif mtype == "sync_workflow":
            await self.handle_sync(msg)
        elif mtype == "sync_product":
            await self.handle_sync_product(msg)
        elif mtype == "status_check":
            await self.handle_status_check()
        elif mtype == "abort":
            print("[Agent] Abort requested (not wired to WRA yet).")

    async def handle_sync(self, msg: dict[str, Any]) -> None:
        workflow_name = str(msg.get("workflow_name") or "")
        workflow_data = msg.get("workflow_data")
        path = self.local_dir / "workflows" / f"{workflow_name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(workflow_data, f, indent=2)
        await self.send({"type": "sync_complete", "workflow_name": workflow_name})
        print(f"[Agent] Synced workflow: {workflow_name}")

    async def handle_status_check(self) -> None:
        status = {
            "type": "status_report",
            "os": platform.system(),
            "version": AGENT_VERSION,
            "chrome": check_chrome(),
            "disk_free_mb": shutil.disk_usage(Path.home()).free // (1024 * 1024),
            "workflows": list_local_workflows(),
            "products": list_local_products(),
        }
        await self.send(status)

    async def handle_sync_product(self, msg: dict[str, Any]) -> None:
        package = msg.get("product_package")
        if not isinstance(package, dict):
            await self.send({"type": "sync_error", "error": "Invalid product package"})
            return
        if package.get("kind") != "cusear_ar_product":
            await self.send({"type": "sync_error", "error": "Unsupported product package kind"})
            return
        product = package.get("product")
        workflow = package.get("workflow")
        if not isinstance(product, dict) or not isinstance(workflow, dict):
            await self.send({"type": "sync_error", "error": "Malformed product package"})
            return

        slug = str(product.get("slug") or "").strip()
        workflow_name = str(workflow.get("workflow_name") or "").strip()
        workflow_data = workflow.get("workflow_json")
        if not slug or not workflow_name or not isinstance(workflow_data, dict):
            await self.send({"type": "sync_error", "error": "Incomplete product package payload"})
            return

        products_path = self.local_dir / "products" / f"{slug}.json"
        products_path.parent.mkdir(parents=True, exist_ok=True)
        with open(products_path, "w", encoding="utf-8") as f:
            json.dump(package, f, indent=2)

        # If product declares a plan, proactively create vault folders for that plan.
        plan = str(product.get("plan") or "").strip().lower().replace("-", "_")
        if plan:
            try:
                from cusear.storage_vault import ensure_plan_vault

                if plan == "ai_pro":
                    # Create all platform roots for ai_pro product packs.
                    ensure_plan_vault(None, "ai_pro")
                elif plan in ("core", "hybrid", "ai_budget"):
                    ensure_plan_vault(None, cast(Any, plan))
            except Exception as exc:
                print(f"[Agent] Plan vault ensure failed for '{plan}': {exc}")

        workflow_path = self.local_dir / "workflows" / f"{workflow_name}.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        with open(workflow_path, "w", encoding="utf-8") as f:
            json.dump(workflow_data, f, indent=2)

        await self.send(
            {
                "type": "sync_complete",
                "workflow_name": workflow_name,
                "product_slug": slug,
            }
        )
        print(f"[Agent] Synced product '{slug}' -> workflow '{workflow_name}'")

    async def handle_run(self, msg: dict[str, Any]) -> None:
        from cusear.engine.wra_engine import run_wra

        workflow_name = str(msg.get("workflow_name") or "")
        run_id = str(msg.get("run_id") or "")
        content_map_raw = msg.get("content_map") or {}
        company_endpoint = msg.get("company_endpoint")
        workflow_data = msg.get("workflow_data")

        path = self.local_dir / "workflows" / f"{workflow_name}.json"
        if workflow_data is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(workflow_data, f, indent=2)

        if not path.is_file():
            await self.send({"type": "run_error", "run_id": run_id, "error": f"Missing workflow file: {path}"})
            return

        await self.send({"type": "run_started", "run_id": run_id})

        resolved: dict[str, Any] = {}
        for k, v in content_map_raw.items():
            resolved[str(k)] = _resolve_content_map_value(str(k), v)

        repo_root = _repo_root()

        def _run() -> dict[str, Any]:
            return run_wra(
                repo_root=repo_root,
                workflow_path=str(path),
                content_map=resolved,
                company_endpoint=company_endpoint if isinstance(company_endpoint, str) else None,
            )

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _run)
            ok = bool(result.get("ok"))
            await self.send({"type": "run_complete", "run_id": run_id, "success": ok})
        except Exception as exc:
            await self.send({"type": "run_error", "run_id": run_id, "error": str(exc)})


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 cusear_agent.py YOUR_AGENT_TOKEN")
        print("Optional env: CUSEAR_WS_BASE=wss://api.cusear.autos  CUSEAR_REPO_ROOT=/path/to/repo")
        sys.exit(1)
    token = sys.argv[1].strip()
    print(f"[Agent] cusear™ Agent v{AGENT_VERSION}")
    print(f"[Agent] Local dir: {LOCAL_DIR}")
    asyncio.run(CusearAgent(token).connect())


if __name__ == "__main__":
    main()
