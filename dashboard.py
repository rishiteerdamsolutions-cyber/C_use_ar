"""
Training Engine Server — Autonomous Web Agency
Serves the TRAINER.html dashboard and handles teach/run API calls.

Run:
    python dashboard.py
    → open http://localhost:7788 in your browser
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

BASE_DIR      = Path(__file__).parent
WORKFLOWS_DIR = BASE_DIR / "workflows"
PORT          = 7788

WORKFLOWS_DIR.mkdir(exist_ok=True)


# ─── Multipart parser ─────────────────────────────────────────────────────────

def _parse_multipart(body: bytes, boundary: str) -> dict:
    """Simple multipart/form-data parser — returns {field: value or [bytes]}."""
    result: dict[str, Any] = {}
    sep = ("--" + boundary).encode()
    parts = body.split(sep)
    for part in parts[1:]:
        if part.strip() in (b"", b"--", b"--\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_raw, _, data = part.partition(b"\r\n\r\n")
        data = data.rstrip(b"\r\n--")
        headers = header_raw.decode(errors="ignore")
        # Extract field name
        name = ""
        filename = ""
        for tok in headers.split(";"):
            tok = tok.strip()
            if tok.startswith("name="):
                name = tok[5:].strip('"')
            elif tok.startswith("filename="):
                filename = tok[9:].strip('"')
        if not name:
            continue
        if filename:
            result.setdefault(name, []).append({"filename": filename, "data": data})
        else:
            result[name] = data.decode(errors="utf-8")
    return result


# ─── Request handler ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  [{self.client_address[0]}] {fmt % args}")

    # ── CORS + JSON helpers ───────────────────────────────────────────────────

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?")[0]

        # Serve TRAINER.html as root
        if path in ("/", "/index.html"):
            html_file = BASE_DIR / "TRAINER.html"
            if html_file.exists():
                self._send_html(html_file)
            else:
                self._send_json({"error": "TRAINER.html not found"}, 404)
            return

        # Health check
        if path == "/health":
            self._send_json({"status": "ok", "version": "1.0"})
            return

        # List workflows
        if path == "/workflows":
            wfs = []
            for p in sorted(WORKFLOWS_DIR.glob("*.json")):
                try:
                    data = json.loads(p.read_text())
                    wfs.append({
                        "name":        p.stem,
                        "total_steps": data.get("total_steps", 0),
                        "taught_at":   data.get("taught_at", ""),
                        "mode":        data.get("mode", "any"),
                    })
                except Exception:
                    pass
            # Also list encrypted workflows
            for p in sorted(WORKFLOWS_DIR.glob("*.enc")):
                wfs.append({"name": p.stem, "total_steps": "?", "mode": "encrypted"})
            self._send_json({"workflows": wfs})
            return

        # Get single workflow
        if path.startswith("/workflow/"):
            name = path[len("/workflow/"):]
            wf_path = WORKFLOWS_DIR / f"{name}.json"
            if wf_path.exists():
                self._send_json(json.loads(wf_path.read_text()))
            else:
                self._send_json({"error": "not found"}, 404)
            return

        self._send_json({"error": "not found"}, 404)

    # ── DELETE ────────────────────────────────────────────────────────────────

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/workflow/"):
            name = path[len("/workflow/"):]
            deleted = False
            for ext in (".json", ".enc"):
                p = WORKFLOWS_DIR / (name + ext)
                if p.exists():
                    p.unlink()
                    deleted = True
            self._send_json({"deleted": deleted})
            return
        self._send_json({"error": "not found"}, 404)

    # ── POST ──────────────────────────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""
        ct     = self.headers.get("Content-Type", "")

        # ── POST /teach ───────────────────────────────────────────────────────
        if path == "/teach":
            try:
                boundary = ""
                for part in ct.split(";"):
                    part = part.strip()
                    if part.startswith("boundary="):
                        boundary = part[9:].strip('"')
                if not boundary:
                    self._send_json({"error": "No boundary in multipart"}, 400)
                    return

                fields = _parse_multipart(body, boundary)
                wf_name      = fields.get("workflow_name", "untitled").strip()
                instructions = json.loads(fields.get("instructions", "[]"))
                screenshots  = fields.get("screenshots", [])

                if not wf_name:
                    self._send_json({"error": "workflow_name required"}, 400)
                    return
                if not screenshots:
                    self._send_json({"error": "No screenshots uploaded"}, 400)
                    return

                # Save screenshots to temp dir
                tmp_dir = Path(tempfile.mkdtemp())
                saved_paths = []
                for i, sc in enumerate(screenshots):
                    fname = sc.get("filename", f"{i+1}.png")
                    fpath = tmp_dir / fname
                    fpath.write_bytes(sc["data"])
                    saved_paths.append(fpath)

                # Map instructions
                inst_map = {item["step"]: item["description"] for item in instructions}

                # Run teach in background thread (returns immediately with job ID)
                def _do_teach():
                    try:
                        from teach.screenshot_teacher import teach_from_folder
                        result = teach_from_folder(
                            workflow_name=wf_name,
                            screenshot_folder=tmp_dir,
                            instructions=[inst_map.get(i+1, f"Step {i+1}") for i in range(len(saved_paths))],
                        )
                        print(f"  ✓ Taught workflow: {wf_name} ({result.get('total_steps', 0)} steps)")
                    except Exception as e:
                        print(f"  ✗ Teach error: {e}")
                        # Fallback: save a basic workflow JSON without Vision analysis
                        _save_basic_workflow(wf_name, saved_paths, inst_map, tmp_dir)
                    finally:
                        shutil.rmtree(tmp_dir, ignore_errors=True)

                threading.Thread(target=_do_teach, daemon=True).start()

                self._send_json({
                    "status":      "teaching",
                    "workflow_name": wf_name,
                    "total_steps": len(saved_paths),
                    "message":     f"Teaching '{wf_name}' from {len(saved_paths)} screenshots…",
                })

            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # ── POST /run ─────────────────────────────────────────────────────────
        if path == "/run":
            try:
                data         = json.loads(body)
                wf_name      = data.get("workflow_name", "")
                mode         = data.get("mode", "fast")
                variables    = data.get("variables", {})
                dry_run      = data.get("dry_run", False)

                if not wf_name:
                    self._send_json({"error": "workflow_name required"}, 400)
                    return

                # Check workflow exists
                json_path = WORKFLOWS_DIR / f"{wf_name}.json"
                enc_path  = WORKFLOWS_DIR / f"{wf_name}.enc"
                if not json_path.exists() and not enc_path.exists():
                    available = [p.stem for p in WORKFLOWS_DIR.glob("*.json")]
                    available += [p.stem for p in WORKFLOWS_DIR.glob("*.enc")]
                    self._send_json({
                        "error": f"Workflow '{wf_name}' not found. Available: {available}"
                    }, 404)
                    return

                # Run in thread, wait for result
                result_holder: dict = {}
                done_event = threading.Event()

                def _do_run():
                    try:
                        os.environ.setdefault("APP_MODE", "development")
                        if mode == "fast":
                            from teach.runner_v1 import RunnerV1
                            runner = RunnerV1(wf_name, dry_run=dry_run)
                        else:
                            from teach.workflow_runner import WorkflowRunner
                            runner = WorkflowRunner(wf_name, dry_run=dry_run)

                        success = runner.run(variables=variables)
                        result_holder["success"] = success
                        result_holder["steps"] = []  # TODO: collect step results

                    except Exception as e:
                        result_holder["error"] = str(e)
                    finally:
                        done_event.set()

                t = threading.Thread(target=_do_run, daemon=True)
                t.start()

                # Wait up to 5 min
                done_event.wait(timeout=300)

                if "error" in result_holder:
                    self._send_json({"error": result_holder["error"]}, 500)
                else:
                    self._send_json({
                        "success":     result_holder.get("success", False),
                        "workflow":    wf_name,
                        "mode":        mode,
                        "dry_run":     dry_run,
                        "steps":       result_holder.get("steps", []),
                        "live_url":    result_holder.get("live_url", ""),
                        "duration_seconds": result_holder.get("duration", 0),
                    })

            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "unknown endpoint"}, 404)


# ─── Basic workflow fallback (no Claude Vision) ───────────────────────────────

def _save_basic_workflow(name: str, screenshots: list, inst_map: dict, tmp_dir: Path):
    """Save a basic workflow JSON when Claude Vision is unavailable (dev/offline)."""
    import datetime
    steps = []
    for i, sc in enumerate(screenshots):
        steps.append({
            "step":        i + 1,
            "action_type": "click",
            "intent":      inst_map.get(i + 1, f"Step {i+1}"),
            "trained_x":   960,
            "trained_y":   540,
            "confidence":  0.0,
            "screenshot":  sc.name,
            "status":      "needs_vision_analysis",
        })

    wf = {
        "workflow_name": name,
        "total_steps":   len(steps),
        "taught_at":     datetime.datetime.utcnow().isoformat(),
        "mode":          "training_only",
        "steps":         steps,
        "note":          "Basic save — re-teach with ANTHROPIC_API_KEY set for full Vision analysis",
    }
    out = Path(__file__).parent / "workflows" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(wf, indent=2))
    print(f"  ⚠ Saved basic workflow (no Vision): {out}")


# ─── Run ──────────────────────────────────────────────────────────────────────

def run():
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"\n{'━'*50}")
    print(f"  ⚡ Web Agency Trainer — running on port {PORT}")
    print(f"  Open:  http://localhost:{PORT}")
    print(f"  Stop:  Ctrl+C")
    print(f"{'━'*50}\n")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


if __name__ == "__main__":
    run()
