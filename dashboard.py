"""
Agent Dashboard — Autonomous Web Agency Agent v1.0
Browser-based UI: upload screenshots, teach workflows, run them.

Run:
    python dashboard.py
    → opens http://localhost:7788
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import webbrowser
from pathlib import Path
from typing import Any

from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse

BASE_DIR          = Path(__file__).parent
SCREENSHOTS_INPUT = BASE_DIR / "teach" / "screenshots_input"
WORKFLOWS_DIR     = BASE_DIR / "workflows"
PORT              = 7788


# ─── HTML dashboard ───────────────────────────────────────────────────────────
def _html_page(workflows: list[str], screenshots: list[str], message: str = "") -> str:
    wf_options = "".join(f'<option value="{w}">{w}</option>' for w in workflows)
    wf_cards = ""
    for w in workflows:
        try:
            data = json.loads((WORKFLOWS_DIR / f"{w}.json").read_text())
            steps = data.get("total_steps", 0)
            taught = data.get("taught_at", "")[:10]
            wf_cards += f"""
            <div class="card wf-card">
              <div class="wf-name">📋 {w}</div>
              <div class="wf-meta">{steps} steps · taught {taught}</div>
              <div class="wf-actions">
                <button onclick="runWF('{w}','smart')" class="btn btn-ai">▶ Run (AI mode)</button>
                <button onclick="runWF('{w}','fast')"  class="btn btn-fast">⚡ Run (Fast mode)</button>
                <button onclick="showSteps('{w}')"     class="btn btn-info">👁 Preview</button>
              </div>
            </div>"""
        except Exception:
            pass

    ss_thumbs = ""
    for s in screenshots:
        ss_thumbs += f'<div class="thumb-wrap"><div class="thumb-name">{s}</div></div>'

    msg_html = f'<div class="msg">{message}</div>' if message else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonomous Web Agency Agent</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',sans-serif;background:#0f0f0f;color:#e8e8e8;min-height:100vh}}
  header{{background:#1a1a1a;border-bottom:1px solid #c9a96e33;padding:18px 32px;display:flex;align-items:center;gap:16px}}
  header h1{{font-size:1.3rem;color:#c9a96e;font-weight:700}}
  header span{{color:#888;font-size:.85rem}}
  .badge{{background:#c9a96e22;color:#c9a96e;border:1px solid #c9a96e44;border-radius:20px;padding:3px 12px;font-size:.75rem}}
  main{{max-width:1100px;margin:0 auto;padding:32px 24px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:32px}}
  @media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
  .card{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:24px}}
  .card h2{{color:#c9a96e;font-size:1rem;margin-bottom:16px;font-weight:600}}
  .wf-card{{display:flex;flex-direction:column;gap:10px;padding:18px}}
  .wf-name{{font-weight:600;font-size:.95rem;color:#fff}}
  .wf-meta{{color:#888;font-size:.8rem}}
  .wf-actions{{display:flex;gap:8px;flex-wrap:wrap}}
  .btn{{border:none;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}}
  .btn-primary{{background:#c9a96e;color:#000}}
  .btn-primary:hover{{background:#e0c080}}
  .btn-ai{{background:#2d4a8a;color:#fff}}
  .btn-ai:hover{{background:#3a5faa}}
  .btn-fast{{background:#1a5c2a;color:#fff}}
  .btn-fast:hover{{background:#22752f}}
  .btn-info{{background:#333;color:#ccc}}
  .btn-info:hover{{background:#444}}
  .btn-danger{{background:#5c1a1a;color:#faa}}
  input[type=text],select{{width:100%;background:#111;border:1px solid #333;color:#e8e8e8;border-radius:6px;padding:9px 12px;font-size:.9rem;margin-bottom:10px}}
  input[type=text]:focus,select:focus{{outline:none;border-color:#c9a96e66}}
  .drop-zone{{border:2px dashed #333;border-radius:10px;padding:32px;text-align:center;color:#666;transition:.2s;cursor:pointer;margin-bottom:12px}}
  .drop-zone:hover,.drop-zone.over{{border-color:#c9a96e;color:#c9a96e;background:#c9a96e08}}
  .thumbs{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}}
  .thumb-wrap{{background:#222;border:1px solid #333;border-radius:6px;padding:8px 12px;font-size:.78rem;color:#aaa}}
  .msg{{background:#1a3a1a;border:1px solid #2a6a2a;border-radius:8px;padding:12px 16px;color:#7fc97f;margin-bottom:20px;font-size:.88rem}}
  .msg.error{{background:#3a1a1a;border-color:#6a2a2a;color:#cf7070}}
  .section-title{{color:#888;font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}}
  .divider{{border:none;border-top:1px solid #222;margin:28px 0}}
  .step-list{{list-style:none;padding:0}}
  .step-list li{{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid #222;font-size:.85rem}}
  .step-num{{color:#c9a96e;font-weight:700;min-width:24px}}
  .step-type{{background:#222;border-radius:4px;padding:2px 8px;color:#888;font-size:.75rem;align-self:flex-start;margin-top:2px}}
  #modal{{display:none;position:fixed;inset:0;background:#000a;z-index:99;align-items:center;justify-content:center}}
  #modal.show{{display:flex}}
  #modal-box{{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:28px;max-width:600px;width:90%;max-height:80vh;overflow-y:auto}}
  #modal-box h3{{color:#c9a96e;margin-bottom:16px}}
  .close-btn{{float:right;background:none;border:none;color:#888;font-size:1.4rem;cursor:pointer}}
  .log-box{{background:#111;border-radius:6px;padding:14px;font-family:monospace;font-size:.8rem;color:#7fc97f;min-height:80px;max-height:220px;overflow-y:auto;margin-top:8px;white-space:pre-wrap}}
  .mode-pill{{display:inline-block;border-radius:20px;padding:3px 10px;font-size:.73rem;font-weight:700}}
  .pill-ai{{background:#2d4a8a;color:#9bb8ff}}
  .pill-fast{{background:#1a5c2a;color:#7fc97f}}
</style>
</head>
<body>

<header>
  <div>
    <h1>⚡ Autonomous Web Agency Agent</h1>
    <span>Karimnagar, Telangana · v1.0</span>
  </div>
  <span class="badge">🟢 Dashboard</span>
</header>

<main>
{msg_html}

<!-- Two modes explanation -->
<div class="card" style="margin-bottom:24px;background:#111;border-color:#c9a96e22">
  <div style="display:flex;gap:32px;flex-wrap:wrap">
    <div>
      <span class="mode-pill pill-fast">⚡ V1 FAST MODE</span>
      <p style="margin-top:8px;font-size:.83rem;color:#888">Training Only · Zero API calls · Uses saved pixel coords<br>Best for: same UI every run, high volume, offline</p>
    </div>
    <div>
      <span class="mode-pill pill-ai">🤖 V2 AI MODE</span>
      <p style="margin-top:8px;font-size:.83rem;color:#888">Training + Claude Vision · Re-checks every step live<br>Best for: UI that changes, new deployments, resilience</p>
    </div>
  </div>
</div>

<div class="grid">

  <!-- LEFT: Teach New Workflow -->
  <div class="card">
    <h2>🎓 Teach New Workflow</h2>
    <p class="section-title">1. Upload screenshots (name: 1.png, 2.png, 3.png…)</p>

    <div class="drop-zone" id="dropZone" onclick="document.getElementById('fileInput').click()">
      📂 Drop screenshots here or click to browse<br>
      <small style="color:#555">Name them 1.png, 2.png, 3.png in order</small>
    </div>
    <input type="file" id="fileInput" multiple accept="image/*" style="display:none" onchange="uploadFiles(this.files)">

    <div class="thumbs" id="thumbs">
      {ss_thumbs}
    </div>

    <hr class="divider">
    <p class="section-title">2. Name & describe each step</p>

    <form id="teachForm" onsubmit="submitTeach(event)">
      <input type="text" id="wfName" placeholder="Workflow name  e.g.  deploy_to_vercel" required>
      <div id="stepFields"></div>
      <button type="button" class="btn btn-info" onclick="loadStepFields()" style="margin-bottom:10px;width:100%">
        🔄 Load step fields from uploaded screenshots
      </button>
      <button type="submit" class="btn btn-primary" style="width:100%">
        🚀 Teach This Workflow (Claude analyses each screenshot)
      </button>
    </form>

    <div class="log-box" id="teachLog" style="display:none"></div>
  </div>

  <!-- RIGHT: Run Workflow -->
  <div class="card">
    <h2>▶ Run a Workflow</h2>
    <p class="section-title">Saved workflows</p>

    {wf_cards if wf_cards else '<p style="color:#555;font-size:.85rem">No workflows yet — teach one on the left.</p>'}

    <hr class="divider">
    <p class="section-title">Quick run</p>
    <select id="wfSelect">
      <option value="">— choose workflow —</option>
      {wf_options}
    </select>
    <input type="text" id="varInput" placeholder="Variables  e.g.  CLIENT_NAME=Priya Salon">
    <div style="display:flex;gap:8px">
      <button onclick="quickRun('smart')" class="btn btn-ai" style="flex:1">🤖 Run AI Mode</button>
      <button onclick="quickRun('fast')"  class="btn btn-fast" style="flex:1">⚡ Run Fast Mode</button>
    </div>
    <div class="log-box" id="runLog" style="display:none;margin-top:12px"></div>
  </div>

</div>
</main>

<!-- Step preview modal -->
<div id="modal">
  <div id="modal-box">
    <button class="close-btn" onclick="closeModal()">✕</button>
    <h3 id="modal-title">Workflow Steps</h3>
    <ul class="step-list" id="modal-steps"></ul>
  </div>
</div>

<script>
// ── Drag & drop upload ────────────────────────────────────────────────
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => {{ e.preventDefault(); dz.classList.add('over'); }});
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => {{
  e.preventDefault(); dz.classList.remove('over');
  uploadFiles(e.dataTransfer.files);
}});

async function uploadFiles(files) {{
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const r = await fetch('/upload', {{method:'POST', body:fd}});
  const d = await r.json();
  location.reload();
}}

// ── Load step description fields ──────────────────────────────────────
async function loadStepFields() {{
  const r = await fetch('/screenshots');
  const {{screenshots}} = await r.json();
  const container = document.getElementById('stepFields');
  container.innerHTML = '';
  screenshots.forEach((s, i) => {{
    container.innerHTML += `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
        <span style="color:#c9a96e;min-width:40px;font-size:.8rem">${{s}}</span>
        <input type="text" name="step_${{i+1}}" placeholder="What should the agent do here?" required style="margin:0">
      </div>`;
  }});
}}

// ── Submit teach form ─────────────────────────────────────────────────
async function submitTeach(e) {{
  e.preventDefault();
  const name = document.getElementById('wfName').value.trim().replace(/\\s+/g,'_');
  const fields = document.querySelectorAll('#stepFields input');
  const steps = [];
  const r0 = await fetch('/screenshots');
  const {{screenshots}} = await r0.json();
  fields.forEach((f, i) => {{
    steps.push({{screenshot: screenshots[i], instruction: f.value}});
  }});
  if (!steps.length) {{ alert('Load step fields first!'); return; }}

  const log = document.getElementById('teachLog');
  log.style.display = 'block';
  log.textContent = 'Teaching workflow — Claude is analysing each screenshot...\\n';

  const r = await fetch('/teach', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{name, steps}})
  }});
  const d = await r.json();
  log.textContent += d.message || JSON.stringify(d);
  if (d.ok) setTimeout(() => location.reload(), 1500);
}}

// ── Run workflow ──────────────────────────────────────────────────────
async function runWF(name, mode) {{
  const log = document.getElementById('runLog');
  log.style.display = 'block';
  log.textContent = `Starting ${{name}} in ${{mode}} mode...\\n`;
  const r = await fetch('/run', {{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{name, mode, variables:{{}}}})
  }});
  const d = await r.json();
  log.textContent += d.message;
}}

async function quickRun(mode) {{
  const name = document.getElementById('wfSelect').value;
  if (!name) {{ alert('Choose a workflow'); return; }}
  const varStr = document.getElementById('varInput').value;
  const variables = {{}};
  varStr.split(',').forEach(pair => {{
    const [k,v] = pair.split('=');
    if (k && v) variables[`{{{{${{k.trim()}}}}}}`] = v.trim();
  }});
  const log = document.getElementById('runLog');
  log.style.display = 'block';
  log.textContent = `Starting ${{name}} (${{mode}})...\\n`;
  const r = await fetch('/run', {{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{name, mode, variables}})
  }});
  const d = await r.json();
  log.textContent += d.message;
}}

// ── Preview steps ─────────────────────────────────────────────────────
async function showSteps(name) {{
  const r = await fetch('/workflow/' + name);
  const d = await r.json();
  document.getElementById('modal-title').textContent = name + ' — ' + d.total_steps + ' steps';
  const ul = document.getElementById('modal-steps');
  ul.innerHTML = '';
  (d.steps || []).forEach(s => {{
    const act = s.action || {{}};
    ul.innerHTML += `<li>
      <span class="step-num">${{s.step}}</span>
      <div>
        <div>${{s.instruction}}</div>
        <span class="step-type">${{act.action_type || '?'}}</span>
        ${{act.trained_x ? `<small style="color:#555"> coords(${{act.trained_x}},${{act.trained_y}})</small>` : ''}}
      </div>
    </li>`;
  }});
  document.getElementById('modal').classList.add('show');
}}

function closeModal() {{ document.getElementById('modal').classList.remove('show'); }}
document.getElementById('modal').addEventListener('click', e => {{
  if (e.target === document.getElementById('modal')) closeModal();
}});
</script>
</body>
</html>"""


# ─── HTTP request handler ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access log

    def _send(self, body: str | bytes, content_type: str = "text/html", status: int = 200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200):
        self._send(json.dumps(data), "application/json", status)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
            SCREENSHOTS_INPUT.mkdir(parents=True, exist_ok=True)
            workflows = [p.stem for p in sorted(WORKFLOWS_DIR.glob("*.json"))]
            screenshots = sorted(
                [p.name for p in SCREENSHOTS_INPUT.iterdir()
                 if p.suffix.lower() in (".png",".jpg",".jpeg",".webp")],
                key=lambda n: int(Path(n).stem) if Path(n).stem.isdigit() else 999
            )
            self._send(_html_page(workflows, screenshots))

        elif path == "/screenshots":
            screenshots = sorted(
                [p.name for p in SCREENSHOTS_INPUT.iterdir()
                 if p.suffix.lower() in (".png",".jpg",".jpeg",".webp")],
                key=lambda n: int(Path(n).stem) if Path(n).stem.isdigit() else 999
            )
            self._json({"screenshots": screenshots})

        elif path.startswith("/workflow/"):
            name = path.split("/workflow/")[1]
            wf_path = WORKFLOWS_DIR / f"{name}.json"
            if wf_path.exists():
                self._send(wf_path.read_bytes(), "application/json")
            else:
                self._json({"error": "not found"}, 404)

        else:
            self._send("<h1>404</h1>", status=404)

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        path = self.path

        # ── Upload screenshots ────────────────────────────────────────────────
        if path == "/upload":
            content_type = self.headers.get("Content-Type", "")
            body = self._read_body()
            SCREENSHOTS_INPUT.mkdir(parents=True, exist_ok=True)

            if "multipart/form-data" in content_type:
                boundary = content_type.split("boundary=")[1].strip().encode()
                parts = body.split(b"--" + boundary)
                saved = 0
                for part in parts:
                    if b"filename=" not in part:
                        continue
                    header_end = part.find(b"\r\n\r\n")
                    if header_end < 0:
                        continue
                    header = part[:header_end].decode(errors="ignore")
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    # Extract filename
                    for h in header.split("\r\n"):
                        if "filename=" in h:
                            fname = h.split('filename="')[1].split('"')[0]
                            dest = SCREENSHOTS_INPUT / fname
                            dest.write_bytes(file_data)
                            saved += 1
                            break
                self._json({"ok": True, "saved": saved})
            else:
                self._json({"ok": False, "error": "expected multipart"}, 400)

        # ── Teach workflow ────────────────────────────────────────────────────
        elif path == "/teach":
            body = json.loads(self._read_body())
            name  = body.get("name", "workflow")
            steps = body.get("steps", [])

            # Resolve screenshot paths
            resolved_steps = []
            for s in steps:
                img_name = s.get("screenshot", "")
                img_path = SCREENSHOTS_INPUT / img_name
                resolved_steps.append({
                    "screenshot":  str(img_path) if img_path.exists() else img_name,
                    "instruction": s.get("instruction", ""),
                })

            try:
                # Run in thread so we don't block the UI
                def _do_teach():
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR))
                    from teach.screenshot_teacher import teach_from_screenshots
                    teach_from_screenshots(name, resolved_steps, screenshot_dir=SCREENSHOTS_INPUT)

                t = threading.Thread(target=_do_teach, daemon=True)
                t.start()
                self._json({"ok": True, "message": f"Teaching '{name}' started — {len(steps)} steps being analysed by Claude Vision. Check terminal for progress."})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 500)

        # ── Run workflow ──────────────────────────────────────────────────────
        elif path == "/run":
            body      = json.loads(self._read_body())
            name      = body.get("name", "")
            mode      = body.get("mode", "smart")
            variables = body.get("variables", {})

            try:
                def _do_run():
                    import sys as _sys
                    _sys.path.insert(0, str(BASE_DIR))
                    if mode == "fast":
                        from teach.runner_v1 import RunnerV1
                        runner = RunnerV1(name, dry_run=False)
                    else:
                        from teach.workflow_runner import WorkflowRunner
                        runner = WorkflowRunner(name, dry_run=False)
                    runner.run(variables=variables)

                t = threading.Thread(target=_do_run, daemon=True)
                t.start()
                mode_label = "⚡ Fast (Training-Only)" if mode == "fast" else "🤖 AI Mode (Claude Vision)"
                self._json({"ok": True, "message": f"Workflow '{name}' started in {mode_label}.\nAgent is now executing on your desktop.\nCheck the terminal for live step logs."})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, 500)

        else:
            self._json({"error": "unknown endpoint"}, 404)


# ─── Launch ───────────────────────────────────────────────────────────────────
def run_dashboard(port: int = PORT, open_browser: bool = True) -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_INPUT.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"

    print(f"""
╔══════════════════════════════════════════════════╗
║   🌐  Agent Dashboard running                    ║
║   Open:  {url:<38}  ║
║   Stop:  Ctrl+C                                  ║
╚══════════════════════════════════════════════════╝
""")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    run_dashboard()
