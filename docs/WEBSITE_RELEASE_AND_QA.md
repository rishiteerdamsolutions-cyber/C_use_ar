# Website, downloads, Windows release & QA — cusear™

Everything runs from **`dashboard.py`**: marketing pages (`portal/`), **Control Center** (`TRAINER.html` at **`/trainer`**), REST API (`/workflows`, `/run`, …), and **public installers** (`downloads/`).

## URLs (same origin)

| Path | Purpose |
|------|---------|
| `/` | Marketing home (`portal/index.html`) |
| `/pricing.html`, `/products.html`, … | Portal pages |
| **`/trainer`** | Control Center UI |
| **`/downloads/<file>`** | Served from repo `downloads/` (e.g. Windows `.exe`) |
| **`/downloads/manifest.json`** | Names the Windows/Mac filenames for `download.html` |

Set **`TRAINER_NO_OPEN_BROWSER=1`** if you do not want an extra browser tab on server start (`python dashboard.py` now opens **`/trainer`**, not `/`).

## 1 — Build Windows customer desktop

From repo root:

```bash
python3 -m pip install -r requirements.txt
python3 scripts/export_ar_desktop.py \
  --agency-home . \
  --bundle-slug Social_Media_Package \
  --platform-target win \
  --artifact-out ./downloads/Social_Media_Package_cusear_win.zip \
  --work-dir ./build/export-social-media
```

Run this on Windows for a Windows ZIP. It packages **`customer_app.py`** only, then injects the selected bundle and child workflows. For the internal Trainer studio only, use **`bash scripts/build_trainer_app.sh`**; do not ship that build to customers.

Then:

1. Copy the `.exe` into **`downloads/`** (default filename: **`cusear-desktop-windows-setup.exe`**), or edit **`downloads/manifest.json`** to match your artefact name.
2. Restart **`dashboard.py`** and verify:

```bash
curl -I "http://127.0.0.1:7788/downloads/manifest.json"
curl -I "http://127.0.0.1:7788/downloads/cusear-desktop-windows-setup.exe"
```

Large installers are **`gitignored`**; upload them onto the production host’s **`downloads/`** folder on each release.

When macOS is ready: add the DMG/GZ to **`downloads/`**, set **`mac.available`** to **`true`** in **`manifest.json`**, and swap the disabled button on **`portal/download.html`** (automatic if manifest drives the UI).

## 2 — Test one ar™ with five platforms (QA)

Assume one **bundled Automation Routine** (under `bundles/`) linking workflows that ultimately touch **Instagram, Facebook, LinkedIn, X, WhatsApp** (status or Web as designed).

### Prereqs on a Windows QA machine

1. Install from your **`/downloads/...exe`** package.
2. **Google Chrome** + signed-in sessions where the routine expects them.
3. **`AGENCY_HOME`** (if portable) pointing at writable data; **`workflows/`** and **`bundles/`** present for that ar™.
4. Keys in **`.env.local`** per plan tier (Hybrid/AI, vision, WhatsApp notify, …).

### Runbook

1. Start **`dashboard.py`** (or the shipped desktop shim that starts it).
2. Open **`http://127.0.0.1:7788/trainer`** (production: **`https://your-domain/trainer`**).
3. **Automation**: select the bundled workflow tied to your ar™, run **manual** once before enabling the schedule (or dry-run if exposed).
4. Watch **STOP** in the shell header — use it between steps during bring-up.
5. Confirm outbound **WhatsApp** confirmation lines when your tier includes them.
6. Inspect **`logs/workflow_runs/`** JSON audits for failures per platform pass.

Debugging tip: throttle to **four → five** platforms if your bundle allows toggles; then enable the fifth row.

## 3 — Deploy “one website” (production)

Typical VPS / Railway-style layout:

| Step | Action |
|------|--------|
| 1 | Run **`dashboard.py`** behind **HTTPS** (Caddy/nginx TLS to `localhost:7788` or uvicorn-if you wrap later). |
| 2 | Ensure **`portal/`**, **`downloads/`** (manifest + installers), **`TRAINER.html`**, **`workflows/`** (bundled starter), **`bundles/`** exist on disk. |
| 3 | Set **`PORT`** via env / firewall; restrict API with **`X-API-Key`** where possible. |

## 4 — End-user journey

1. Land on **`/`** (marketing).
2. **`Download`** page lists Windows **`/downloads/...`** from **`manifest.json`**.
3. After install → open **`/trainer`** for Control Center.

---

**Fallback:** rename **`portal/index.html`** if you need `/` to fall back to the Trainer-only mode (dashboard serves **`TRAINER.html`** when portal index is missing).
