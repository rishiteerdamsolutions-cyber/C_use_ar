#!/usr/bin/env python3
"""
Label Pointer — live screen only: type a visible label, move the cursor to the best matching control.
No training file. Uses the same detection + OCR as ui_locator.py.

Small floating panel so you can keep it open beside a browser or app in normal (non-fullscreen) layout.
"""

from __future__ import annotations

import sys
import time

import pyautogui
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui_locator import go_to_target_on_live_screen

PANEL_WIDTH = 300


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


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Label Pointer")
        self.setFixedWidth(PANEL_WIDTH)
        self.setMinimumWidth(PANEL_WIDTH)
        self.setMaximumWidth(PANEL_WIDTH)
        self.resize(PANEL_WIDTH, 280)
        self._positioned = False

        central = QWidget()
        self.setCentralWidget(central)
        col = QVBoxLayout(central)
        col.setSpacing(10)

        self.chk_float = QCheckBox("Float above other windows (keep open beside Chrome, etc.)")
        self.chk_float.setChecked(True)
        self.chk_float.toggled.connect(self._on_float_toggled)
        col.addWidget(self.chk_float)

        col.addWidget(QLabel("<b>Label or name on screen</b>"))
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("e.g. Post, Log in, Email, Submit")
        self.edit.returnPressed.connect(self._go)
        col.addWidget(self.edit)

        self.chk_dry = QCheckBox("Dry run (find match but do not move mouse)")
        col.addWidget(self.chk_dry)

        go = QPushButton("Move cursor to label (live screen)")
        go.setMinimumHeight(40)
        go.clicked.connect(self._go)
        col.addWidget(go)

        self.hint = QLabel(
            "Keep your <b>browser or app visible</b> (normal or tiled window — not macOS full-screen Space). "
            "<b>Go</b> briefly hides this panel so detection only sees other windows — not the text you typed here."
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        col.addWidget(self.hint)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #e5e7eb; font-size: 11px;")
        col.addWidget(self.status)

        self.setStyleSheet(
            "QMainWindow, QWidget { background: #1b1f2a; color: #e5e7eb; }"
            "QPushButton { background: #2563eb; color: white; padding: 8px; border: none; border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: #1d4ed8; }"
            "QLineEdit { background: #0f172a; border: 1px solid #334155; padding: 8px; color: #e5e7eb; }"
            "QCheckBox { font-size: 12px; }"
        )
        self._apply_window_flags(self.chk_float.isChecked(), defer_show=True)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        _win_set_topmost(self, self.chk_float.isChecked())
        if not self._positioned:
            self._positioned = True
            self._dock_right()

    def _dock_right(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        margin = 8
        h = min(max(self.height(), 200), geo.height() - 2 * margin)
        self.resize(PANEL_WIDTH, h)
        x = geo.x() + geo.width() - self.width() - margin
        y = geo.y() + margin
        self.move(x, y)

    def _apply_window_flags(self, floating: bool, *, defer_show: bool = False) -> None:
        if floating:
            flags = Qt.WindowType.Window | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        else:
            flags = Qt.WindowType.Window
        self.setWindowFlags(flags)
        if sys.platform == "darwin":
            attr = getattr(Qt.WidgetAttribute, "WA_MacAlwaysShowToolWindow", None)
            if attr is not None:
                self.setAttribute(attr, floating)
        if defer_show:
            return
        _win_set_topmost(self, floating)
        self.show()

    def _on_float_toggled(self, on: bool) -> None:
        self._apply_window_flags(on, defer_show=False)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if event.type() == QEvent.Type.WindowStateChange:
            st = self.windowState()
            bad = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
            if st & bad:
                self.setWindowState(Qt.WindowState.WindowNoState)
                self.resize(PANEL_WIDTH, min(self.height(), 400))
                self._dock_right()
        super().changeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        if self.width() != PANEL_WIDTH:
            self.resize(PANEL_WIDTH, self.height())
        super().resizeEvent(event)

    def _go(self) -> None:
        label = self.edit.text().strip()
        if not label:
            QMessageBox.warning(self, "Empty", "Enter a label or name to find.")
            return
        # Screenshot must not include this window — otherwise OCR matches the field / hints here.
        was_visible = self.isVisible()
        self.hide()
        QApplication.processEvents()
        time.sleep(0.22)
        try:
            result = go_to_target_on_live_screen(
                label,
                dry_run=self.chk_dry.isChecked(),
                duration=0.22,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "No match", str(exc))
            self.status.setText("")
            result = None
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", str(exc))
            self.status.setText("")
            result = None
        finally:
            if was_visible:
                self.show()
                self.raise_()
                QApplication.processEvents()
                time.sleep(0.05)
                _win_set_topmost(self, self.chk_float.isChecked())

        if result is None:
            return

        pos = result["cursor_position"]
        sel = result["selected"]
        name = sel.get("name", "?")
        mode = "Would move to" if self.chk_dry.isChecked() else "Moved to"
        self.status.setText(
            f"{mode} ({pos['x']}, {pos['y']}) — matched: “{name}” ({sel.get('element_type', '?')})"
        )


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
