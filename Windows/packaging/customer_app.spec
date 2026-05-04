# -*- mode: python ; coding: utf-8 -*-
# Customer runner build: excludes Trainer HTML, dashboard server, and portal files.
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent.parent
datas = []
env_example = ROOT / ".env.example"
if env_example.is_file():
    datas.append((str(env_example), "."))

a = Analysis(
    [str(ROOT / "customer_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "cusear.media_folders",
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
    hooksconfig={},
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
    name="CusearCustomerApp",
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
    name="CusearCustomerApp",
)
