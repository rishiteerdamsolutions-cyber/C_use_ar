#!/usr/bin/env python3
"""
Build a consumer desktop bundle for one AR (bundle): PyInstaller + that bundle's JSON +
child workflows + .env.local (optional embedded API keys).

Run from repo root. Host OS must match target (no cross-compile).

  python3 scripts/export_ar_desktop.py \\
    --agency-home /path/to/repo \\
    --bundle-slug Social_Media_Package \\
    --platform-target mac \\
    --embed-keys \\
    --artifact-out /tmp/out.dmg \\
    --work-dir /tmp/pyi_work
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


def _slug(s: str) -> str:
    x = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "").strip()).strip("._-")
    return x or "ar"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _exe_base_name(bundle_slug: str) -> str:
    base = f"{_slug(bundle_slug)}_cusear"
    if len(base) > 52:
        base = base[:52].rstrip("._-")
    return base or "ar_cusear"


def _write_spec(build_dir: Path, repo: Path, exe_name: str) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    env_ex = repo / ".env.example"
    root_repr = repr(str(repo.resolve()))
    datas = []
    if env_ex.is_file():
        datas.append((str(env_ex), "."))
    datas_py = ",\n        ".join(f"(r{repr(a)}, r{repr(b)})" for a, b in datas)
    spec = f'''# -*- mode: python ; coding: utf-8 -*-
# Auto-generated for AR desktop export — do not edit by hand.
import sys
from pathlib import Path

block_cipher = None
ROOT = Path({root_repr})

a = Analysis(
    [str(ROOT / "customer_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        {datas_py}
    ],
    hiddenimports=[
        "dotenv",
        "mss",
        "mss.tools",
        "PIL",
        "PIL.ImageGrab",
        "anthropic",
        "openai",
        "pyautogui",
        "pyperclip",
        "pygetwindow",
        "keyboard",
        "tkinter",
        "tkinter.ttk",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=["dashboard", "webview"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

if sys.platform == "win32":
    a.hiddenimports += ["win32api", "win32con", "win32gui"]
elif sys.platform == "darwin":
    a.hiddenimports += ["AppKit", "Quartz"]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={repr(exe_name)},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name={repr(exe_name)},
)
'''
    p = build_dir / "export_ar_desktop.spec"
    p.write_text(spec, encoding="utf-8")
    return p


def _append_log(lines: list[str], msg: str) -> None:
    lines.append(msg)


def run_export(
    *,
    agency_home: Path,
    bundle_slug: str,
    platform_target: str,
    embed_api_keys: bool,
    artifact_out: Path,
    work_dir: Path,
) -> dict[str, str | bool]:
    """
    platform_target: 'mac' | 'win' — must match sys.platform family.
    Returns dict: ok, log, artifact (path str), error (optional).
    """
    log_lines: list[str] = []
    repo = repo_root()
    ah = agency_home.expanduser().resolve()
    slug = _slug(bundle_slug)
    bpath = ah / "bundles" / f"{slug}.json"
    if not bpath.is_file():
        msg = f"Bundle not found: {bpath}"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    try:
        bundle = json.loads(bpath.read_text(encoding="utf-8"))
    except Exception as exc:
        msg = f"Invalid bundle JSON: {exc}"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    children = [str(x or "").strip() for x in (bundle.get("children") or []) if str(x or "").strip()]
    if not children:
        msg = "Bundle has no child workflows."
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    missing: list[str] = []
    for ch in children:
        wfp = ah / "workflows" / f"{ch}.json"
        if not wfp.is_file():
            missing.append(ch)
    if missing:
        msg = f"Missing workflow JSON for: {', '.join(missing)}"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    host = sys.platform
    if platform_target == "mac" and host != "darwin":
        msg = "macOS .dmg builds must run on macOS (Trainer on a Mac)."
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}
    if platform_target == "win" and not host.startswith("win"):
        msg = "Windows builds must run on Windows (Trainer on Windows)."
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    try:
        ver = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:
        msg = f"PyInstaller check failed: {exc}. Install with: {sys.executable} -m pip install 'pyinstaller>=6.0'"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}
    if ver.returncode != 0:
        tail = (ver.stderr or ver.stdout or "").strip()[-500:]
        msg = (
            "PyInstaller is not available for this Python. Run:\n"
            f"  {sys.executable} -m pip install 'pyinstaller>=6.0'\n"
            + (f"\n({tail})" if tail else "")
        )
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    exe_name = _exe_base_name(slug)
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_path = _write_spec(work_dir, repo, exe_name)
    distpath = work_dir / "dist"
    workpath = work_dir / "pyi_work"
    shutil.rmtree(distpath, ignore_errors=True)
    shutil.rmtree(workpath, ignore_errors=True)

    # Windowed vs console is set in the .spec (EXE(..., console=False)).
    # PyInstaller 6+ rejects --windowed / --console when a .spec file is passed.
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        f"--distpath={distpath}",
        f"--workpath={workpath}",
        str(spec_path),
    ]
    _append_log(log_lines, "Running: " + " ".join(cmd))
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        msg = "PyInstaller timed out after 900s"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}
    if proc.stdout:
        _append_log(log_lines, proc.stdout[-8000:])
    if proc.stderr:
        _append_log(log_lines, "[stderr]\n" + proc.stderr[-4000:])
    if proc.returncode != 0:
        msg = f"PyInstaller failed (exit {proc.returncode})"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    out_folder = distpath / exe_name
    if not out_folder.is_dir():
        msg = f"Expected build folder missing: {out_folder}"
        _append_log(log_lines, msg)
        return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}

    # Ship one bundle + workflows beside the one-folder executable (customer_app.py AGENCY_HOME).
    bd = out_folder / "bundles"
    wd = out_folder / "workflows"
    bd.mkdir(parents=True, exist_ok=True)
    wd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bpath, bd / f"{slug}.json")
    for ch in children:
        shutil.copy2(ah / "workflows" / f"{ch}.json", wd / f"{ch}.json")
    media_src = ah / "media_library"
    if media_src.is_dir():
        media_dst = out_folder / "media_library"
        shutil.rmtree(media_dst, ignore_errors=True)
        shutil.copytree(media_src, media_dst)
        _append_log(log_lines, f"Copied media_library for upload steps: {media_dst}")

    env_lines = [
        "# Generated by Trainer — AR-only consumer desktop",
        "DESKTOP_APP=1",
        "AGENCY_USER_MODE=consumer",
        "# Same as other frozen builds: validate license.key on startup (expiry + optional LICENSE_SERVER_URL).",
        "DESKTOP_LICENSE_CHECK=1",
        f"CUSEAR_DEFAULT_AR_SLUG={slug}",
        "TRAINER_SCHEDULER_ENABLED=1",
        "TRAINER_NO_OPEN_BROWSER=1",
        "",
        "# End user: launch the customer app to enable/disable the schedule, run now, or stop.",
    ]
    if embed_api_keys:
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "TRAINER_VISION_PROVIDER",
            "OPENAI_BASE_URL",
        ):
            val = (os.environ.get(key) or "").strip()
            if val:
                esc = val.replace("\\", "\\\\").replace("\n", " ")
                env_lines.append(f"{key}={esc}")
        env_lines.insert(6, "# API keys embedded from exporter session — usage bills to key owner.")
    else:
        env_lines.extend(
            [
                "# Add your provider keys (or distributor may ship a pre-filled .env.local):",
                "# OPENAI_API_KEY=...",
            ]
        )
    (out_folder / ".env.local").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    gatekeeper_mac = ""
    if platform_target == "mac":
        gatekeeper_mac = textwrap.dedent(
            """

            macOS — “cannot verify / malware” (Gatekeeper)
            -----------------------------------------------
            This build is not Apple-notarized (normal for local PyInstaller exports).

            • In Finder: **Control-click** (or right-click) the **{exe_name}** program → **Open** → **Open**.
            • Or: **System Settings** → **Privacy & Security** → after the first launch attempt, choose **Open Anyway** for this app.
            • If it still blocks after copying from a download/DMG, clear quarantine in Terminal (replace with your real path):
                xattr -dr com.apple.quarantine "/path/to/{exe_name}"

            For public distribution you would sign with an Apple Developer ID certificate and notarize with Apple (separate process).
            """
        ).format(exe_name=exe_name)

    readme = textwrap.dedent(
        f"""
        cusear™ — desktop package ({exe_name})
        ======================================

        • Place **license.key** in this same folder as the app (next to the executable). Without a valid license the app will not start.
        • Optional: set **LICENSE_SERVER_URL** in .env.local to your POST /validate endpoint for online renewal/revocation checks (see security/license.py).
        • Open this folder and run "{exe_name}" ({'macOS' if platform_target == 'mac' else 'Windows'} — PyInstaller one-folder layout).
        • This build includes one ar routine only (slug: {slug}).
        • The customer app is runner-only: no Trainer, Step Builder, Rekky, WRA Studio, or website routes are included.
        • Schedule: use the native customer window to enable/disable the shipped schedule, run now, dry run, or stop.
        • API usage: keys in .env.local count against that key's provider limits.
        {gatekeeper_mac}
        """
    ).strip()
    (out_folder / "README_AR_DESKTOP.txt").write_text(readme + "\n", encoding="utf-8")

    artifact_out.parent.mkdir(parents=True, exist_ok=True)
    artifact_out = artifact_out.resolve()

    if platform_target == "mac":
        dmg_tool = shutil.which("create-dmg")
        if dmg_tool:
            if artifact_out.suffix.lower() != ".dmg":
                artifact_out = artifact_out.with_suffix(".dmg")
            if artifact_out.exists():
                artifact_out.unlink()
            cmd_dmg = [dmg_tool, "--volname", exe_name[:32], str(artifact_out), str(out_folder)]
            _append_log(log_lines, "Running: " + " ".join(cmd_dmg))
            try:
                subprocess.run(cmd_dmg, cwd=str(repo), capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                msg = "create-dmg timed out"
                _append_log(log_lines, msg)
                return {"ok": False, "log": "\n".join(log_lines), "artifact": "", "error": msg}
            if artifact_out.is_file():
                _append_log(log_lines, f"DMG ready: {artifact_out}")
                return {"ok": True, "log": "\n".join(log_lines), "artifact": str(artifact_out), "error": ""}
            _append_log(log_lines, "create-dmg did not produce file; falling back to zip")
        ap = artifact_out
        zip_base = str(ap.parent / (ap.stem + "_mac"))
        zpath = shutil.make_archive(zip_base, "zip", root_dir=str(distpath), base_dir=exe_name)
        _append_log(log_lines, f"Zip (no DMG tool): {zpath}")
        return {"ok": True, "log": "\n".join(log_lines), "artifact": str(Path(zpath)), "error": ""}

    # Windows → zip the folder (Inno can be added later)
    an = artifact_out.name
    if an.lower().endswith(".zip"):
        zip_base = str(artifact_out.parent / an[:-4])
    else:
        zip_base = str(artifact_out.with_suffix(""))
    if not zip_base.lower().endswith("_win"):
        zip_base = zip_base + "_win"
    zpath = shutil.make_archive(zip_base, "zip", root_dir=str(distpath), base_dir=exe_name)
    _append_log(log_lines, f"Windows zip ready: {zpath}")
    return {"ok": True, "log": "\n".join(log_lines), "artifact": str(Path(zpath)), "error": ""}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agency-home", type=Path, required=True)
    ap.add_argument("--bundle-slug", required=True)
    ap.add_argument("--platform-target", choices=("mac", "win"), required=True)
    ap.add_argument("--embed-keys", action="store_true")
    ap.add_argument("--artifact-out", type=Path, required=True)
    ap.add_argument("--work-dir", type=Path, required=True)
    args = ap.parse_args()
    res = run_export(
        agency_home=args.agency_home,
        bundle_slug=args.bundle_slug,
        platform_target=args.platform_target,
        embed_api_keys=bool(args.embed_keys),
        artifact_out=args.artifact_out,
        work_dir=args.work_dir,
    )
    print(json.dumps(res), flush=True)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
