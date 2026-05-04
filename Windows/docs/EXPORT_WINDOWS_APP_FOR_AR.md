# Step-by-step: export a Windows desktop app for one ar™

This guide matches the split architecture: Trainer exports call **`scripts/export_ar_desktop.py`**, which packages the runner-only **`customer_app.py`** plus one bundle and its child workflows. The Trainer-side **`POST /export-desktop-ar`** job in **`dashboard.py`** and the **Export desktop** modal in **`TRAINER.html`** remain developer-only.

**Important:** A **Windows installer / zip is built on a Windows machine**. You cannot cross-compile the PyInstaller consumer bundle from macOS for Windows (the script checks `sys.platform`).

---

## What you get

A **ZIP** (artifact name like **`<BundleSlug>_cusear_win.zip`** or the path you pass) containing a **one-folder PyInstaller** layout:

- **`<BundleSlug>_cusear/`** (folder name = executable base)
  - **`bundle_slug_cusear.exe`** (GUI, `console=False` in generated spec)
  - **`bundles/<slug>.json`** — the single ar™ bundle you exported
  - **`workflows/*.json`** — every **child** workflow referenced by that bundle
  - **`.env.local`** — sets consumer mode, scheduler, `CUSEAR_DEFAULT_AR_SLUG=<slug>`, etc.
  - **`README_AR_DESKTOP.txt`** — end-user notes (license, Gatekeeper on Mac, etc.)

The ZIP should **not** include **`TRAINER.html`**, **`portal/`**, or **`dashboard.py`**. Customers launch a minimal native runner window from **`customer_app.py`**; they do not see the Trainer.

You still ship a valid **`license.key`** next to the app (see **`security/license.py`**) unless you change that policy.

Optional: **`--embed-keys`** copies **`OPENAI_*` / `ANTHROPIC_*`** from the **exporter's** environment into `.env.local` (keys bill to whoever owns them).

---

## Phase A — Prepare the ar™ on your training machine

### A1. Confirm the bundle JSON

1. Open **`bundles/<YourSlug>.json`** (slug is filename without `.json`).
2. Ensure **`children`** is a non-empty list of workflow **stems** that exist under **`workflows/<name>.json`**.
3. Each child workflow should implement the posting path for the platforms you promise (e.g. Instagram, Facebook, LinkedIn, X, WhatsApp/status) using the step types you rely on (Click, Type, Tab-heavy WRA™ flows, etc.).

### A2. Run and dry-run on Windows

1. On a **Windows** PC, run **`python dashboard.py`** (or your dev desktop).
2. Open **Control Center**: **`http://127.0.0.1:7788/trainer`**.
3. For **each child workflow**, run a **dry run** or **controlled live run** once; fix failures before export.
4. If you use automation schedules, set **next run** / topics in the **Automation** and **ar™** areas as you intend for customers.

### A3. Enable desktop export (API + UI)

Default: export is **allowed** (`TRAINER_ALLOW_DESKTOP_EXPORT` defaults to **on**).

- To **disable** exports (e.g. hosted SaaS): set **`TRAINER_ALLOW_DESKTOP_EXPORT=0`**.
- To **allow** on a build machine: omit that or set **`1`**.

---

## Phase B — Export (choose one path)

### Path 1 — Control Center UI (recommended when `TRAINER.html` is loaded)

1. Go to **Exporter** or use **Desktop build…** where the UI calls **`openDesktopExportInApp()`** (see **`TRAINER.html`**).
2. Open the **Export desktop (single ar™)** modal.
3. Select **bundle slug** and **Windows** as the platform.
4. Choose whether to **embed API keys** (only for trusted internal builds).
5. Start the job; poll until **status = ok**, then **download** the ZIP (browser hits **`/export-desktop-ar/download?id=…`**).

Underlying API:

- **`POST /export-desktop-ar`** JSON body (example):

  ```json
  { "bundle_slug": "Social_Media_Package", "platform": "win", "embed_keys": false }
  ```

  Response: **`{ "job_id": "...", "status": "running" }`**

- **`GET /export-desktop-ar/job?id=<job_id>`** — log, error, artifact path, status.
- **`GET /export-desktop-ar/download?id=<job_id>`** — binary ZIP when **`status`** is **`ok`**.

### Path 2 — Command line (CI or scripted)

On **Windows**, from the **repo root**:

```text
python scripts/export_ar_desktop.py ^
  --agency-home C:\path\to\agency\root ^
  --bundle-slug Your_Bundle_Slug ^
  --platform-target win ^
  --artifact-out C:\out\Your_Bundle_export.zip ^
  --work-dir C:\tmp\pyi_work
```

Optional: add **`--embed-keys`** to fold current env keys into `.env.local`.

**`--agency-home`** must contain **`bundles/`** and **`workflows/`** with all child JSONs.

Script requirements (see script): **`python -m pip install 'pyinstaller>=6.0'`** on that machine.

The script prints a **JSON line** with **`ok`**, **`artifact`**, **`log`**, **`error`** — the dashboard worker parses the last JSON object from stdout.

### Path 3 — Plain PyInstaller (trainer / dev, not locked to one bundle)

For the **internal Trainer** desktop folder (not the single-AR customer bundle), use:

```bash
bash scripts/build_desktop.sh
```

That uses **`packaging/trainer_app.spec`** and includes **`TRAINER.html`** + **`dashboard.py`**. It is **not** the same as **`export_ar_desktop.py`** (which packages **`customer_app.py`** and injects **one bundle**, **child workflows**, and **`.env.local`** into the dist folder).

---

## Phase C — Post-export checklist

1. **Unzip** on a clean Windows VM → run **`.exe`**.
2. Confirm folder contains **`bundles/`**, **`workflows/`**, **`.env.local`**, and that **`CUSEAR_DEFAULT_AR_SLUG`** matches your bundle.
3. Add **`license.key`** if your build enforces **`DESKTOP_LICENSE_CHECK=1`** (default in generated `.env.local`).
4. First launch: the native customer runner should show only schedule/status/run/stop controls. No Trainer, Step Builder, Rekky, WRA Studio, or marketing site should appear.
5. Run one **manual** automation cycle across all promised platforms before you ship ZIPs to users.

---

## Phase D — Turn the ZIP into a public “installer” link (website)

Retail flow is often:

1. Unzip internally → optionally wrap **`setup.exe`** with **Inno Setup** / **WiX** (not in-repo today — export produces **ZIP of one-folder**).
2. Place the artefact consumers download under **`downloads/`** on your server(s).
3. Update **`downloads/manifest.json`** → **`windows.file`** must match your published filename (for example **`cusear-desktop-windows-setup.exe`** if you built an installer and renamed accordingly).

Hosting the **same origin** as the live site + Control Center is documented in **`docs/WEBSITE_RELEASE_AND_QA.md`**.

---

## Troubleshooting (quick)

| Symptom | What to check |
|--------|----------------|
| **`403 desktop export disabled`** | Set **`TRAINER_ALLOW_DESKTOP_EXPORT=1`** (or unset **`0`**). |
| **`Cannot build for Windows — run Trainer on Windows`** | Export job must execute on **`win32`**; use a Windows CI runner or dev PC. |
| **`Missing workflow JSON for: …`** | Every **`children`** name must have **`workflows/<name>.json`**. |
| **PyInstaller fail** | Install **`pyinstaller>=6`**, antivirus exclusions for `pyi_*` work dirs, sufficient disk space. |
| **`create-dmg` / Mac** | Windows path skips DMG — Mac builds use **`--platform-target mac`** on Darwin only. |

---

## Related files

| File | Role |
|------|------|
| **`scripts/export_ar_desktop.py`** | Builds runner-only customer spec, PyInstaller, copies bundle/workflows, writes `.env.local`, zips folder. |
| **`customer_app.py`** | Native runner-only customer entrypoint; no Trainer server or website routes. |
| **`trainer_app.py`** | Internal studio entrypoint. |
| **`dashboard.py`** | Trainer-side **`POST /export-desktop-ar`**, job store, and optional website/download host. |
| **`TRAINER.html`** | Developer-only **`openDesktopExportInApp`** modal wiring. |

---

*Aligned with repo behaviour at authoring time; verify flags and paths against your deployed branch.*
