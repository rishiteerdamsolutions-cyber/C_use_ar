#!/usr/bin/env python3
"""Runner-only customer desktop application.

This entrypoint does not start dashboard.py, serve HTTP routes, or load
TRAINER.html. It provides a small native Tkinter window around the exported
bundle, scheduler, logs, and stop control.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, messagebox, ttk


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = _app_root()
os.environ.setdefault("AGENCY_HOME", str(ROOT))
os.environ.setdefault("DESKTOP_APP", "1")
os.environ.setdefault("AGENCY_USER_MODE", "consumer")
os.environ.setdefault("TRAINER_NO_OPEN_BROWSER", "1")


def _load_env_file_literal(path: Path, *, override: bool) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not key or (not override and key in os.environ):
            continue
        val = val.strip()
        if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val[1:-1]
        os.environ[key] = val


try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(ROOT / ".env.local", override=True, interpolate=False)
    load_dotenv(ROOT / ".env", override=False, interpolate=False)
except Exception:
    pass
_load_env_file_literal(ROOT / ".env.local", override=True)
_load_env_file_literal(ROOT / ".env", override=False)


def _validate_license_if_needed() -> None:
    if (os.environ.get("APP_MODE", "production") or "").strip() == "development":
        return
    default_check = "1" if getattr(sys, "frozen", False) else "0"
    raw = (os.environ.get("DESKTOP_LICENSE_CHECK") or default_check).strip().lower()
    if raw in ("0", "false", "no", "off"):
        return
    from security.license import validate_license

    validate_license(ROOT / "license.key")


class CustomerApp:
    def __init__(self) -> None:
        from cusear.customer_runtime import CustomerRuntime

        self.root = Tk()
        self.root.title("cusear Customer Runner")
        self.root.geometry("720x430")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.status_text = StringVar(value="Starting...")
        self.bundle_text = StringVar(value="")
        self.next_run_text = StringVar(value="")
        self.last_log_text = StringVar(value="")
        self.schedule_enabled = BooleanVar(value=False)

        # Build UI first so the window is never blank while we load bundle files.
        self.runtime = None
        self._build_ui()
        self.set_status("Loading bundle…")
        self.runtime = CustomerRuntime(status_cb=self.set_status)
        self.refresh()
        # Important: do NOT auto-start the scheduler. The customer should explicitly enable it.
        # This avoids “automation running without control” on launch.

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 8}
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Customer Automation Runner", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )
        ttk.Label(frame, text="ar bundle").grid(row=1, column=0, sticky="w", **pad)
        ttk.Label(frame, textvariable=self.bundle_text).grid(row=1, column=1, columnspan=2, sticky="w", **pad)

        ttk.Checkbutton(
            frame,
            text="Schedule enabled",
            variable=self.schedule_enabled,
            command=self.toggle_schedule,
        ).grid(row=2, column=0, sticky="w", **pad)
        ttk.Label(frame, text="Next run").grid(row=2, column=1, sticky="e", **pad)
        ttk.Label(frame, textvariable=self.next_run_text).grid(row=2, column=2, sticky="w", **pad)

        ttk.Label(frame, text="Status").grid(row=3, column=0, sticky="nw", **pad)
        ttk.Label(frame, textvariable=self.status_text, wraplength=520).grid(row=3, column=1, columnspan=2, sticky="w", **pad)

        ttk.Label(frame, text="Last log").grid(row=4, column=0, sticky="w", **pad)
        ttk.Label(frame, textvariable=self.last_log_text, wraplength=520).grid(row=4, column=1, columnspan=2, sticky="w", **pad)

        ttk.Button(frame, text="Run now", command=lambda: self.run_now(False)).grid(row=5, column=0, sticky="ew", **pad)
        ttk.Button(frame, text="Dry run", command=lambda: self.run_now(True)).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Button(frame, text="Stop", command=self.stop).grid(row=5, column=2, sticky="ew", **pad)
        ttk.Button(frame, text="Refresh", command=self.refresh).grid(row=6, column=0, sticky="ew", **pad)
        ttk.Button(frame, text="Exit", command=self.close).grid(row=6, column=2, sticky="ew", **pad)
        ttk.Button(frame, text="Storage (create folders)", command=self.create_content_folders).grid(
            row=7, column=0, columnspan=3, sticky="ew", **pad
        )

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

    def set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_text.set(str(text)))

    def refresh(self) -> None:
        if self.runtime is None:
            return
        try:
            status = self.runtime.refresh_status()
            self.bundle_text.set(status.bundle_slug or "(no bundle found)")
            self.schedule_enabled.set(bool(status.schedule_enabled))
            self.next_run_text.set(status.next_run_at or "(not scheduled)")
            self.last_log_text.set(status.last_log_path or "(no runs yet)")
            if status.last_status:
                self.status_text.set(status.last_status)
        except Exception as exc:
            self.status_text.set(f"Error: {exc}")

    def toggle_schedule(self) -> None:
        if self.runtime is None:
            return
        try:
            enabled = bool(self.schedule_enabled.get())
            self.runtime.set_schedule_enabled(enabled)
            if enabled:
                self.runtime.start_scheduler()
                self.set_status("Scheduler started.")
            else:
                # Stop background loop; keeps the app open but prevents any further automatic runs.
                self.runtime.shutdown()
                self.set_status("Scheduler stopped.")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Schedule error", str(exc))

    def run_now(self, dry_run: bool) -> None:
        if self.runtime is None:
            return
        def _worker() -> None:
            try:
                self.runtime.run_now(dry_run=dry_run)
            except Exception as exc:
                self.set_status(f"Run error: {exc}")
            finally:
                self.root.after(0, self.refresh)

        threading.Thread(target=_worker, daemon=True, name="customer-run-now").start()

    def stop(self) -> None:
        if self.runtime is None:
            return
        self.runtime.stop_current_run()

    def create_content_folders(self) -> None:
        try:
            import subprocess
            from cusear.storage_vault import bootstrap_storage_vault

            r = bootstrap_storage_vault(None)
            root_path = str(r.get("root") or "").strip()
            stubs = int(r.get("stub_files_created") or 0)
            total = int(r.get("total_days") or 30)
            msg = (
                "Storage folder is ready.\n\n"
                f"Path: {root_path}\n"
                f"Calendar days: 1–{total}\n"
                f"New empty slot stubs this run: {stubs}\n\n"
                "When you use Storage in the app and pick a plan (Core, Hybrid, AI Budget, or AI Pro), "
                "only that plan’s folders are created under this path. Generated or uploaded files are saved there."
            )
            messagebox.showinfo("Storage", msg)
            if root_path:
                try:
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", root_path])
                    elif sys.platform == "win32":
                        os.startfile(root_path)  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen(["xdg-open", root_path])
                except Exception:
                    pass
        except Exception as exc:
            messagebox.showerror("Storage", str(exc))

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.shutdown()
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main() -> int:
    try:
        _validate_license_if_needed()
        return CustomerApp().run()
    except Exception as exc:
        try:
            messagebox.showerror("cusear Customer Runner", str(exc))
        except Exception:
            print(f"Customer app failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
