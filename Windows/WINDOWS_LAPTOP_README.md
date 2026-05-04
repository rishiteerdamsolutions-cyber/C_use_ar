# Windows laptop — build single ar™ desktop export

This folder is a **copy** of the agency repo (minus secrets, build artifacts, `.git`, large `media_library/`, and duplicate `Windows test/`). Your originals on the Mac are unchanged.

## 1. Install Python

Use **Python 3.10+** from [python.org](https://www.python.org/downloads/windows/). Check “Add python.exe to PATH”.

## 2. Virtual environment and dependencies

Open **Command Prompt** or **PowerShell**, `cd` into this folder, then:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 3. Optional: media for upload workflows

This copy **did not** include `media_library/` (often large). If your workflows use library images/videos, copy your Mac `media_library` folder here (next to `bundles/` and `workflows/`) before exporting.

## 4. Export the Windows customer app

Replace `Your_Bundle_Slug` with the bundle filename **without** `.json` under `bundles\`:

```bat
python scripts\export_ar_desktop.py ^
  --agency-home . ^
  --bundle-slug Your_Bundle_Slug ^
  --platform-target win ^
  --artifact-out dist\Your_Bundle_export.zip ^
  --work-dir build\pyi_work
```

Optional: add `--embed-keys` only for trusted internal builds (embeds API keys from the current environment into `.env.local`).

## 5. After export

- Artifact: a ZIP with the one-folder PyInstaller app.
- Ship **`license.key`** beside the app if `DESKTOP_LICENSE_CHECK=1` (set in generated `.env.local`).

## 6. Syncing updates from the Mac later

Re-run the same `rsync` (or your zip) from the Mac to refresh this folder, or use `git clone` / `git pull` on the laptop if you use a remote.

See also: `docs/EXPORT_WINDOWS_APP_FOR_AR.md` in this tree.
