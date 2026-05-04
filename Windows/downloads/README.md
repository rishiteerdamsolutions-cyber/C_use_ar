# Public downloads (`/downloads/`)

Files placed here are served by `dashboard.py` at **`https://YOUR_HOST:PORT/downloads/<filename>`** (same host as the public marketing site and Control Center).

The **live marketing HTML** lives only under **`CUSEAR WEBSITE  UX UI/`**; those pages link here by filename (see `manifest.json`). Vercel copies matching binaries into `public/downloads/` during deploy when present.

## Windows launch (current)

1. Run your desktop build pipeline (PyInstaller via `scripts/build_desktop.sh` or your branded spec).
2. Copy the installer or portable `.exe` into this folder.
3. **Rename or ship as** `cusear-desktop-windows-setup.exe` (see `manifest.json`) so links on `download.html` work without edits.

Alternatively, edit `downloads/manifest.json` to use your real filenames.

## macOS (coming soon)

Add `cusear-desktop-macos.dmg` (or update `manifest.json`) when the DMG is ready.

## Do not commit large binaries

`.gitignore` ignores `*.exe` and `*.dmg` here so CI/repos stay light. Upload artifacts to your host’s `downloads/` directory during deploy.
