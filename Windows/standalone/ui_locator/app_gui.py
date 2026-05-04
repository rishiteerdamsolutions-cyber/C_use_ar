#!/usr/bin/env python3
"""Desktop UI for testing screenshot element detection and cursor targeting."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import pyautogui
from PIL import Image, ImageDraw, ImageTk

from ui_locator import UIElement, _choose_target, detect_elements


class UILocatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("UI Locator Tester")
        self.root.geometry("1180x760")

        self.image_path: Path | None = None
        self.display_image: Image.Image | None = None
        self.display_photo: ImageTk.PhotoImage | None = None
        self.base_image: Image.Image | None = None
        self.elements: list[UIElement] = []
        self.selected_index: int | None = None

        self._build_layout()
        self._load_default_sample()

    def _build_layout(self) -> None:
        self.root.configure(bg="#1b1f2a")

        top = tk.Frame(self.root, bg="#1b1f2a", padx=10, pady=10)
        top.pack(fill=tk.X)

        self._make_button(top, "Upload Screenshot", self.upload_screenshot).pack(side=tk.LEFT)
        self._make_button(top, "Detect Elements", self.detect).pack(side=tk.LEFT, padx=(8, 0))
        self._make_button(top, "Move Cursor To Selected", self.move_cursor).pack(side=tk.LEFT, padx=(8, 0))
        self._make_button(top, "Dry Run (No Move)", self.dry_run).pack(side=tk.LEFT, padx=(8, 0))

        self.status_var = tk.StringVar(value="Load a screenshot to begin.")
        tk.Label(
            top,
            textvariable=self.status_var,
            bg="#1b1f2a",
            fg="#e5e7eb",
            font=("Arial", 11),
        ).pack(side=tk.LEFT, padx=(16, 0))

        body = tk.Frame(self.root, bg="#1b1f2a", padx=10, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(body, bg="#1b1f2a")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(left, bg="#101218", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        right = tk.Frame(body, width=390, bg="#1b1f2a")
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        tk.Label(
            right,
            text="Detected Elements",
            bg="#1b1f2a",
            fg="#f3f4f6",
            font=("Arial", 12, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        list_frame = tk.Frame(right, bg="#1b1f2a")
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.listbox = tk.Listbox(
            list_frame,
            bg="#0f172a",
            fg="#e5e7eb",
            selectbackground="#2563eb",
            selectforeground="#ffffff",
            activestyle="none",
            font=("Menlo", 11),
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select_listbox)
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.configure(yscrollcommand=scrollbar.set)

        target_box = tk.LabelFrame(
            right,
            text="Search Target",
            bg="#1b1f2a",
            fg="#f3f4f6",
            padx=8,
            pady=8,
        )
        target_box.pack(fill=tk.X, pady=(8, 0))
        self.target_var = tk.StringVar()
        tk.Entry(
            target_box,
            textvariable=self.target_var,
            bg="#0f172a",
            fg="#e5e7eb",
            insertbackground="#e5e7eb",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#334155",
        ).pack(fill=tk.X)
        self._make_button(target_box, "Auto-select By Name", self.select_by_name).pack(
            fill=tk.X, pady=(8, 0)
        )

    def _make_button(self, parent: tk.Widget, text: str, command: callable) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief=tk.FLAT,
            padx=10,
            pady=6,
            cursor="hand2",
            font=("Arial", 11, "bold"),
        )

    def _load_default_sample(self) -> None:
        sample = Path(__file__).resolve().parent / "sample_ui.png"
        if not sample.exists():
            self.status_var.set("Load a screenshot to begin.")
            self._render_image()
            return
        self.image_path = sample
        self.base_image = Image.open(sample).convert("RGB")
        try:
            self.elements = detect_elements(sample)
            self.selected_index = 0 if self.elements else None
            self._refresh_table(select_current=True)
            self.status_var.set(
                f"Loaded sample screenshot with {len(self.elements)} detected elements."
            )
        except Exception:
            self.status_var.set("Loaded default sample screenshot. Click Detect Elements.")
        self._render_image()

    def upload_screenshot(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Screenshot",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.webp"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return

        self.image_path = Path(path)
        self.base_image = Image.open(self.image_path).convert("RGB")
        self.elements = []
        self.selected_index = None
        self._refresh_table()
        self._render_image()
        self.status_var.set(f"Loaded: {self.image_path.name}")

    def detect(self) -> None:
        if not self.image_path:
            messagebox.showwarning("Missing Screenshot", "Please upload a screenshot first.")
            return
        try:
            self.elements = detect_elements(self.image_path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            messagebox.showerror("Detection Error", str(exc))
            return

        self.selected_index = 0 if self.elements else None
        self._refresh_table()
        self._render_image()
        self.status_var.set(f"Detected {len(self.elements)} elements.")

    def select_by_name(self) -> None:
        if not self.elements:
            messagebox.showwarning("No Elements", "Detect elements first.")
            return
        query = self.target_var.get().strip()
        if not query:
            messagebox.showwarning("Missing Name", "Enter button/input name to match.")
            return
        try:
            target = _choose_target(self.elements, query)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            messagebox.showerror("Match Error", str(exc))
            return

        for idx, element in enumerate(self.elements):
            if element is target:
                self.selected_index = idx
                break
        self._refresh_table(select_current=True)
        self._render_image()
        self.status_var.set(f"Auto-selected: {target.name}")

    def on_select_listbox(self, _event: tk.Event) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        self.selected_index = int(selection[0])
        self._render_image()
        element = self.elements[self.selected_index]
        self.status_var.set(
            f"Selected {element.element_type}: {element.name} @ {element.center[0]},{element.center[1]}"
        )

    def move_cursor(self) -> None:
        self._move_selected(dry_run=False)

    def dry_run(self) -> None:
        self._move_selected(dry_run=True)

    def _move_selected(self, dry_run: bool) -> None:
        if self.selected_index is None or self.selected_index >= len(self.elements):
            messagebox.showwarning("No Selection", "Select an element first.")
            return
        if not self.base_image:
            return

        element = self.elements[self.selected_index]
        img_w, img_h = self.base_image.size
        screen = pyautogui.size()
        move_x = int(round(element.center[0] * (screen.width / max(img_w, 1))))
        move_y = int(round(element.center[1] * (screen.height / max(img_h, 1))))

        if not dry_run:
            pyautogui.moveTo(move_x, move_y, duration=0.18)

        mode = "Dry run" if dry_run else "Moved"
        self.status_var.set(
            f"{mode}: {element.name} -> cursor {move_x},{move_y} (screen {screen.width}x{screen.height})"
        )

    def _refresh_table(self, select_current: bool = False) -> None:
        self.listbox.delete(0, tk.END)
        for i, item in enumerate(self.elements):
            row = f"[{i:02d}] {item.element_type:<6}  {item.name}  ({item.center[0]}, {item.center[1]})"
            self.listbox.insert(tk.END, row)
        if select_current and self.selected_index is not None and self.selected_index < len(self.elements):
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(self.selected_index)
            self.listbox.activate(self.selected_index)
            self.listbox.see(self.selected_index)

    def _render_image(self) -> None:
        if not self.base_image:
            self.canvas.delete("all")
            canvas_w = max(self.canvas.winfo_width(), 200)
            canvas_h = max(self.canvas.winfo_height(), 200)
            self.canvas.create_text(
                canvas_w // 2,
                canvas_h // 2 - 18,
                text="No screenshot loaded",
                fill="#d9d9d9",
                font=("Arial", 18, "bold"),
            )
            self.canvas.create_text(
                canvas_w // 2,
                canvas_h // 2 + 16,
                text="Click 'Upload Screenshot' to test your image",
                fill="#aab3c2",
                font=("Arial", 12),
            )
            return

        canvas_w = max(self.canvas.winfo_width(), 200)
        canvas_h = max(self.canvas.winfo_height(), 200)

        img = self.base_image.copy()
        draw = ImageDraw.Draw(img)

        for idx, element in enumerate(self.elements):
            x, y, w, h = element.bbox
            selected = idx == self.selected_index
            color = "#44d17e" if selected else "#5aa0ff"
            draw.rectangle([x, y, x + w, y + h], outline=color, width=3 if selected else 2)
            tag = f"{element.element_type}: {element.name}"
            draw.text((x + 4, max(0, y - 14)), tag, fill=color)

        img.thumbnail((canvas_w - 8, canvas_h - 8))
        self.display_image = img
        self.display_photo = ImageTk.PhotoImage(img)

        self.canvas.delete("all")
        self.canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.display_photo, anchor=tk.CENTER)

    def _on_canvas_resize(self, _event: tk.Event) -> None:
        self._render_image()


def main() -> None:
    root = tk.Tk()
    app = UILocatorApp(root)
    root.update_idletasks()
    app._render_image()  # pylint: disable=protected-access
    root.mainloop()


if __name__ == "__main__":
    main()
