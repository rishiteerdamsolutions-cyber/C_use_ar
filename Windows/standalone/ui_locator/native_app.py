#!/usr/bin/env python3
"""
Kinkar: train/replay steps (anchor + optional label match + click/type/enter).

Also:
- **Live detector** in the GUI: capture screen and list detected buttons/inputs.
- **Headless validate** for CUSEAR (no window): ``python native_app.py validate --label "…"``
  → JSON on stdout, exit 0 if a strong on-screen label match exists, else 1.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import pyautogui
from PIL import Image, ImageDraw

from ui_locator import (
    UIElement,
    _choose_target,
    _scale_center,
    capture_screen_bgr,
    detect_elements_bgr,
)

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
TRAINING_FILE = APP_DIR / "kinkar_training_map.json"

DOCK_WIDTH = 360
PREVIEW_MAX_HEIGHT = 170

pyautogui.PAUSE = 0.03


def normalize_step_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def find_training_entry(
    data: dict[str, dict[str, object]], query: str
) -> tuple[str, dict[str, object]] | None:
    """Match step name case-insensitively and with collapsed whitespace."""
    q = normalize_step_key(query)
    if not q:
        return None
    if query.strip().lower() in data:
        return query.strip().lower(), data[query.strip().lower()]
    if q in data:
        return q, data[q]
    for k, v in data.items():
        if normalize_step_key(k) == q:
            return k, v
    return None


def load_training_map(path: Path | None = None) -> dict[str, dict[str, object]]:
    p = path or TRAINING_FILE
    if not p.exists():
        return {}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    clean: dict[str, dict[str, object]] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        k = key.strip().lower()
        if not k:
            continue
        rec = coerce_training_value(value)
        tn = str(rec.get("target_name", "")).strip()
        if not tn and isinstance(value, str) and value.strip():
            rec["target_name"] = value.strip()
            tn = value.strip()
        if tn:
            clean[k] = rec
    return clean


def save_training_map(data: dict[str, dict[str, object]], path: Path | None = None) -> None:
    p = path or TRAINING_FILE
    p.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def coerce_training_value(value: object) -> dict[str, object]:
    """Normalize JSON training entry: legacy string or full object."""
    defaults: dict[str, object] = {
        "target_name": "",
        "click": False,
        "type_text": "",
        "press_enter": False,
        "open_url": "",
        "anchor_abs_x": None,
        "anchor_abs_y": None,
        "anchor_rel_x": None,
        "anchor_rel_y": None,
        "anchor_screen_w": None,
        "anchor_screen_h": None,
        "find_label": "",
    }
    if isinstance(value, str):
        defaults["target_name"] = value.strip()
        return defaults
    if isinstance(value, dict):
        out = dict(defaults)
        tn = value.get("target_name", value.get("target", value.get("name")))
        if isinstance(tn, str):
            out["target_name"] = tn.strip()
        out["click"] = bool(value.get("click", False))
        tt = value.get("type_text", value.get("type", ""))
        out["type_text"] = tt.strip() if isinstance(tt, str) else str(tt)
        out["press_enter"] = bool(value.get("press_enter", value.get("enter", False)))
        ou = value.get("open_url", "")
        out["open_url"] = ou.strip() if isinstance(ou, str) else ""
        fl = value.get("find_label", value.get("match_query", ""))
        out["find_label"] = fl.strip() if isinstance(fl, str) else ""
        for key in ("anchor_rel_x", "anchor_rel_y"):
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                out[key] = float(raw)
        for key in ("anchor_screen_w", "anchor_screen_h"):
            raw = value.get(key)
            if isinstance(raw, int) and raw > 0:
                out[key] = raw
        for key in ("anchor_abs_x", "anchor_abs_y"):
            raw = value.get(key)
            if isinstance(raw, int) and raw >= 0:
                out[key] = raw
        return out
    return defaults


def _mac_always_show_tool_window(widget: QWidget) -> None:
    if sys.platform != "darwin":
        return
    attr = getattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow", None)
    if attr is not None:
        widget.setAttribute(attr, True)


def _win_set_topmost(widget: QWidget, on: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        swp = 0x0001 | 0x0002 | 0x0040
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0, swp
        )
    except Exception:
        pass


def _write_text_typed(text: str, *, interval: float = 0.02) -> None:
    if not text:
        return
    writer = getattr(pyautogui, "write", None)
    if callable(writer):
        try:
            writer(text, interval=interval)
        except TypeError:
            writer(text)
    else:
        pyautogui.typewrite(text, interval=interval)


def resolve_anchor_xy(entry: dict[str, object]) -> tuple[int, int]:
    raw_ax = entry.get("anchor_rel_x")
    raw_ay = entry.get("anchor_rel_y")
    raw_abs_x = entry.get("anchor_abs_x")
    raw_abs_y = entry.get("anchor_abs_y")
    raw_sw = entry.get("anchor_screen_w")
    raw_sh = entry.get("anchor_screen_h")
    if not isinstance(raw_ax, (int, float)) or not isinstance(raw_ay, (int, float)):
        raise ValueError("Step has no anchor (retrain this step).")
    screen = pyautogui.size()
    if (
        isinstance(raw_abs_x, int)
        and isinstance(raw_abs_y, int)
        and isinstance(raw_sw, int)
        and isinstance(raw_sh, int)
        and raw_sw == int(screen.width)
        and raw_sh == int(screen.height)
    ):
        x, y = raw_abs_x, raw_abs_y
    else:
        x = int(round(float(raw_ax) * max(int(screen.width), 1)))
        y = int(round(float(raw_ay) * max(int(screen.height), 1)))
    x = min(max(x, 0), max(int(screen.width) - 1, 0))
    y = min(max(y, 0), max(int(screen.height) - 1, 0))
    return x, y


def resolve_xy_for_step(entry: dict[str, object]) -> tuple[int, int, str]:
    """
    Prefer finding the control by visible label on the live screen (handles layout moves).
    If ``find_label`` is empty or matching fails, fall back to saved anchor coordinates.
    """
    label = str(entry.get("find_label") or entry.get("match_query", "")).strip()
    if not label:
        x, y = resolve_anchor_xy(entry)
        return x, y, "anchor"
    try:
        image = capture_screen_bgr()
        img_h, img_w = image.shape[:2]
        elements = detect_elements_bgr(image)
        if not elements:
            raise ValueError("no elements")
        chosen = _choose_target(elements, label)
        screen = pyautogui.size()
        x, y = _scale_center(
            chosen.center,
            (img_w, img_h),
            (int(screen.width), int(screen.height)),
        )
        x = min(max(x, 0), max(int(screen.width) - 1, 0))
        y = min(max(y, 0), max(int(screen.height) - 1, 0))
        return x, y, "label"
    except Exception:
        x, y = resolve_anchor_xy(entry)
        return x, y, "anchor"


def _activate_chrome_macos() -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def open_browser_url(url: str) -> None:
    url = (url or "").strip()
    if not url:
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", url], start_new_session=True)
        time.sleep(0.6)
        _activate_chrome_macos()
    elif sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=True)
    else:
        subprocess.Popen(["xdg-open", url], start_new_session=True)


def run_step_automation(
    entry: dict[str, object],
    *,
    type_override: str | None = None,
    url_override: str | None = None,
    open_browser: bool = True,
    wait_after_url_s: float = 3.0,
) -> tuple[list[str], int, int, str]:
    """
    Move to target (by find_label on live screen when set, else saved anchor), then click/type/enter.
    ``type_override`` replaces trained type_text when non-None (empty string clears typing).
    If ``open_browser`` is False, no URL is opened (not even the step's saved URL).
    """
    url = ""
    if open_browser:
        url = (url_override if url_override is not None else str(entry.get("open_url", ""))).strip()
    if url:
        open_browser_url(url)
        time.sleep(max(0.0, wait_after_url_s))

    x, y, pos_source = resolve_xy_for_step(entry)
    do_click = bool(entry.get("click", False))
    trained_type = str(entry.get("type_text", "")).strip()
    if type_override is not None:
        type_text = type_override.strip()
    else:
        type_text = trained_type
    press_enter = bool(entry.get("press_enter", False))

    pyautogui.moveTo(x, y, duration=0.22)
    time.sleep(0.1)
    log: list[str] = []
    if do_click:
        pyautogui.click(x, y)
        log.append("click")
        time.sleep(0.2)
    if type_text:
        _write_text_typed(type_text)
        log.append(f"type({len(type_text)} chars)")
        time.sleep(0.08)
    if press_enter:
        pyautogui.press("enter")
        log.append("enter")
    return log, x, y, pos_source


def pil_to_qimage(image: Image.Image) -> QImage:
    image = image.convert("RGBA")
    data = image.tobytes("raw", "RGBA")
    return QImage(data, image.width, image.height, QImage.Format_RGBA8888)


def draw_elements_on_image(
    base: Image.Image,
    elements: list[UIElement],
    selected: int | None,
) -> Image.Image:
    out = base.copy().convert("RGBA")
    draw = ImageDraw.Draw(out)
    for idx, el in enumerate(elements):
        x, y, w, h = el.bbox
        sel = idx == selected
        color = (34, 197, 94, 255) if sel else (59, 130, 246, 255)
        width = 3 if sel else 2
        for i in range(width):
            draw.rectangle([x - i, y - i, x + w + i, y + h + i], outline=color)
        draw.text((x + 4, max(0, y - 16)), f"{el.element_type}: {el.name}", fill=color)
    return out


def validate_label_headless(expect_label: str) -> dict[str, object]:
    """
    No GUI. Capture live screen, detect UI, see if ``expect_label`` matches strongly.
    Intended for CUSEAR to call before a critical click/type step.
    """
    label = (expect_label or "").strip()
    if not label:
        return {"ok": False, "error": "empty_label", "elements_found": 0}
    try:
        image = capture_screen_bgr()
        img_h, img_w = image.shape[:2]
        elements = detect_elements_bgr(image)
        if not elements:
            return {
                "ok": False,
                "error": "no_elements_detected",
                "elements_found": 0,
                "image_size": {"w": img_w, "h": img_h},
            }
        chosen = _choose_target(elements, label)
        screen = pyautogui.size()
        mx, my = _scale_center(
            chosen.center,
            (img_w, img_h),
            (int(screen.width), int(screen.height)),
        )
        return {
            "ok": True,
            "matched_name": chosen.name,
            "element_type": chosen.element_type,
            "center_screen": {"x": mx, "y": my},
            "bbox_image": list(chosen.bbox),
            "elements_found": len(elements),
            "selected": asdict(chosen),
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "elements_found": len(elements)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "elements_found": 0}


def cmd_validate(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Kinkar headless: confirm a label exists on the live screen.")
    p.add_argument("--label", required=True, help="Text to match (button label, field caption, etc.)")
    args = p.parse_args(argv)
    payload = validate_label_headless(args.label)
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("ok") else 1


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Kinkar")
        self.setFixedWidth(DOCK_WIDTH)
        self.setMinimumWidth(DOCK_WIDTH)
        self.setMaximumWidth(DOCK_WIDTH)
        self.resize(DOCK_WIDTH, 720)

        self._positioned = False
        self._training: dict[str, dict[str, object]] = load_training_map()

        central = QWidget()
        self.setCentralWidget(central)
        col = QVBoxLayout(central)
        col.setSpacing(8)

        title = QLabel("<b>Kinkar Trainer</b>")
        title.setAlignment(Qt.AlignCenter)
        col.addWidget(title)

        self.chk_top = QCheckBox("Keep window on top (can block other windows — turn off if annoying)")
        self.chk_top.setChecked(False)
        self.chk_top.toggled.connect(self._on_top_toggled)
        col.addWidget(self.chk_top)

        hint = QLabel(
            "Turn <b>on top</b> off while you train or use other windows. "
            "If you set <b>Find by label</b>, RUN looks for that text on the live screen first (layout can move); "
            "if it cannot match, it uses your saved cursor position."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        col.addWidget(hint)

        self._bgr: np.ndarray | None = None
        self._pil_image: Image.Image | None = None
        self._elements: list[UIElement] = []
        self._selected: int | None = None

        col.addWidget(QLabel("<b>Live screen — detected UI</b>"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(PREVIEW_MAX_HEIGHT)
        self.scroll.setMinimumHeight(110)
        self.scroll.setStyleSheet("background: #101218; border: 1px solid #334155;")
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumHeight(90)
        self.scroll.setWidget(self.image_label)
        col.addWidget(self.scroll)
        self.list_w = QListWidget()
        self.list_w.setMinimumHeight(100)
        self.list_w.currentRowChanged.connect(self._on_row)
        col.addWidget(self.list_w)

        def full_btn(text: str, slot, *, tall: int = 36) -> QPushButton:
            b = QPushButton(text)
            b.clicked.connect(slot)
            b.setMinimumHeight(tall)
            return b

        col.addWidget(full_btn("Capture + detect now", self._capture_detect))

        cusear_note = QLabel(
            "<b>CUSEAR:</b> headless check (no window):<br>"
            "<code>python native_app.py validate --label \"Repository name\"</code><br>"
            "Exit 0 = strong match on live screen; 1 = no match."
        )
        cusear_note.setWordWrap(True)
        cusear_note.setStyleSheet("color: #94a3b8; font-size: 10px;")
        col.addWidget(cusear_note)

        self.status = QLabel("Ready. Use Capture + detect to preview what Kinkar sees.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #e5e7eb; font-size: 11px;")
        col.addWidget(self.status)

        col.addWidget(QLabel("<b>Step name</b> (same as when you trained)"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("e.g. new post")
        col.addWidget(self.search)

        col.addWidget(QLabel("<b>Find by label</b> (text on the button or field)"))
        self.find_label = QLineEdit()
        self.find_label.setPlaceholderText("e.g. Post, Create, What’s on your mind? — leave empty for position only")
        col.addWidget(self.find_label)

        col.addWidget(QLabel("<b>When training — after move</b>"))
        self.chk_click = QCheckBox("Click")
        self.chk_click.setChecked(False)
        col.addWidget(self.chk_click)
        col.addWidget(QLabel("<b>Type</b> (saved in the step; optional)"))
        self.type_replay = QLineEdit()
        self.type_replay.setPlaceholderText("Optional default text for every run")
        col.addWidget(self.type_replay)
        self.chk_enter = QCheckBox("Enter")
        self.chk_enter.setChecked(False)
        col.addWidget(self.chk_enter)

        col.addWidget(full_btn("Train step (3s)", self._train_from_cursor_live))

        col.addWidget(QLabel("<b>When running</b>"))
        self.chk_open_first = QCheckBox("Open page in browser first")
        self.chk_open_first.setChecked(True)
        col.addWidget(self.chk_open_first)
        col.addWidget(QLabel("<b>Page URL</b>"))
        self.url_run = QLineEdit()
        self.url_run.setPlaceholderText("https://www.facebook.com")
        self.url_run.setText("https://www.facebook.com")
        col.addWidget(self.url_run)
        col.addWidget(QLabel("<b>Message to type this run</b>"))
        self.msg_run = QLineEdit()
        self.msg_run.setPlaceholderText("Leave blank to use saved Type above")
        col.addWidget(self.msg_run)

        run_btn = full_btn("RUN", self._run_step, tall=48)
        run_btn.setObjectName("kinkar_run")
        col.addWidget(run_btn)
        col.addWidget(full_btn("Bring window to front", lambda: self.raise_()))

        self.setStyleSheet(
            "QMainWindow, QWidget { background: #1b1f2a; color: #e5e7eb; }"
            "QPushButton { background: #2563eb; color: white; padding: 6px; border: none; border-radius: 4px; font-size: 12px; }"
            "QPushButton:hover { background: #1d4ed8; }"
            "QPushButton#kinkar_run { background: #16a34a; color: white; padding: 10px; border: none; "
            "border-radius: 6px; font-size: 15px; font-weight: bold; min-height: 48px; }"
            "QPushButton#kinkar_run:hover { background: #15803d; }"
            "QLineEdit { background: #0f172a; border: 1px solid #334155; padding: 6px; color: #e5e7eb; }"
            "QCheckBox { font-size: 12px; }"
            "QListWidget { background: #0f172a; border: 1px solid #334155; font-size: 11px; }"
        )
        self._apply_overlay_window_flags(self.chk_top.isChecked(), defer_show=True)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        _win_set_topmost(self, self.chk_top.isChecked())
        if not self._positioned:
            self._positioned = True
            self._dock_to_right()

    def _dock_to_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        margin = 8
        h = min(max(self.height(), 480), geo.height() - 2 * margin)
        self.resize(DOCK_WIDTH, h)
        x = geo.x() + geo.width() - self.width() - margin
        y = geo.y() + margin
        self.move(x, y)

    def _apply_overlay_window_flags(self, stay_on_top: bool, *, defer_show: bool = False) -> None:
        flags = Qt.WindowType.Window | Qt.WindowType.Tool
        if stay_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        _mac_always_show_tool_window(self)
        if defer_show:
            return
        _win_set_topmost(self, stay_on_top)
        self.show()

    def _on_top_toggled(self, on: bool) -> None:
        self._apply_overlay_window_flags(on, defer_show=False)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            st = self.windowState()
            bad = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
            if st & bad:
                self.setWindowState(Qt.WindowState.WindowNoState)
                self.resize(DOCK_WIDTH, min(self.height(), 900))
                self._dock_to_right()
        super().changeEvent(event)

    def _capture_detect(self) -> None:
        self.status.setText("Capturing live screen…")
        QApplication.processEvents()
        try:
            self._bgr = capture_screen_bgr()
            self._pil_image = Image.fromarray(cv2.cvtColor(self._bgr, cv2.COLOR_BGR2RGB))
            self._elements = detect_elements_bgr(self._bgr)
            self._selected = 0 if self._elements else None
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Detection error", str(exc))
            self._bgr = None
            self._pil_image = None
            self._elements = []
            self._selected = None
            self._refresh_list()
            self._refresh_preview()
            return
        self._refresh_list()
        self._refresh_preview()
        self.status.setText(f"Detected {len(self._elements)} element(s). Select a row to highlight.")

    def _refresh_list(self) -> None:
        self.list_w.blockSignals(True)
        self.list_w.clear()
        for i, el in enumerate(self._elements):
            self.list_w.addItem(QListWidgetItem(f"[{i}] {el.element_type}  {el.name}"))
        if self._selected is not None and 0 <= self._selected < self.list_w.count():
            self.list_w.setCurrentRow(self._selected)
        self.list_w.blockSignals(False)

    def _on_row(self, row: int) -> None:
        if row < 0 or row >= len(self._elements):
            return
        self._selected = row
        self._refresh_preview()
        el = self._elements[row]
        self.status.setText(f"Selected: {el.element_type} “{el.name}”")

    def _refresh_preview(self) -> None:
        if not self._pil_image:
            self.image_label.setText("No capture yet — tap Capture + detect.")
            self.image_label.setPixmap(QPixmap())
            return
        drawn = draw_elements_on_image(self._pil_image, self._elements, self._selected)
        qimg = pil_to_qimage(drawn)
        pix = QPixmap.fromImage(qimg)
        vw = max(self.scroll.viewport().width() - 4, 80)
        vh = max(self.scroll.viewport().height() - 4, 80)
        if pix.width() > vw or pix.height() > vh:
            pix = pix.scaled(vw, vh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(pix)
        self.image_label.setText("")

    def _run_step(self) -> None:
        query = self.search.text().strip()
        if not query:
            QMessageBox.warning(self, "Empty", "Enter the step name.")
            return
        data = load_training_map()
        found = find_training_entry(data, query)
        if found is None:
            QMessageBox.warning(
                self,
                "Not trained",
                f"No step matches “{query}”. Train it first (exact name is not required; extra spaces are OK).",
            )
            return
        canonical_key, entry = found

        open_first = self.chk_open_first.isChecked()
        url = self.url_run.text().strip() if open_first else ""
        if open_first and not url:
            url = "https://www.facebook.com"

        msg = self.msg_run.text().strip()
        type_override: str | None = msg if msg else None

        self.showMinimized()
        QApplication.processEvents()
        time.sleep(0.28)
        try:
            log, x, y, pos_src = run_step_automation(
                entry,
                type_override=type_override,
                url_override=url,
                open_browser=open_first,
                wait_after_url_s=3.0,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Run error", str(exc))
            log, x, y, pos_src = [], 0, 0, "error"
        finally:
            self.showNormal()
            self.raise_()

        tail = " → ".join(log) if log else "move only"
        via = "label match" if pos_src == "label" else "saved position"
        self.status.setText(f"Ran “{canonical_key}” ({via}): {tail} @ ({x},{y})")

    def _train_from_cursor_live(self) -> None:
        alias = self.search.text().strip()
        if not alias:
            QMessageBox.warning(self, "Empty", "Enter the step name first.")
            return

        self.status.setText("Training in 3s — move mouse to the exact target (in the other app).")
        QApplication.processEvents()
        for seconds in (3, 2, 1):
            self.status.setText(f"Training '{alias}': place cursor on target… {seconds}s")
            QApplication.processEvents()
            time.sleep(1)

        cursor = pyautogui.position()
        screen = pyautogui.size()

        key = normalize_step_key(alias)
        self._training[key] = {
            "target_name": alias.strip(),
            "find_label": self.find_label.text().strip(),
            "click": self.chk_click.isChecked(),
            "type_text": self.type_replay.text().strip(),
            "press_enter": self.chk_enter.isChecked(),
            "open_url": self.url_run.text().strip(),
            "anchor_abs_x": int(cursor.x),
            "anchor_abs_y": int(cursor.y),
            "anchor_rel_x": float(cursor.x) / max(float(screen.width), 1.0),
            "anchor_rel_y": float(cursor.y) / max(float(screen.height), 1.0),
            "anchor_screen_w": int(screen.width),
            "anchor_screen_h": int(screen.height),
        }
        save_training_map(self._training)
        bits = []
        if self.chk_click.isChecked():
            bits.append("click")
        if self.type_replay.text().strip():
            bits.append("type")
        if self.chk_enter.isChecked():
            bits.append("enter")
        extra = (" + " + ", ".join(bits)) if bits else ""
        self.status.setText(f"Saved “{alias}” (key: {key}) @ ({int(cursor.x)},{int(cursor.y)}){extra}")

    def resizeEvent(self, event) -> None:  # noqa: N802
        if self.width() != DOCK_WIDTH:
            self.resize(DOCK_WIDTH, self.height())
        super().resizeEvent(event)
        self._refresh_preview()


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "validate":
        raise SystemExit(cmd_validate(sys.argv[2:]))
    raise SystemExit(main())
