from __future__ import annotations

import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


VAULT_DIR_NAME = "cusear"

PlanKey = Literal["core", "hybrid", "ai_budget", "ai_pro"]
MediaKey = Literal["text", "image", "video"]
PlatformKey = Literal["instagram", "facebook", "linkedin", "x", "whatsapp"]


PLAN_DIR: dict[PlanKey, str] = {
    "core": "Core",
    "hybrid": "Hybrid",
    "ai_budget": "AI Budget",
    "ai_pro": "AI Pro",
}

# Top-level directory names that belong to the current Storage vault (never deleted by legacy cleanup).
PLAN_TOP_LEVEL_DIR_NAMES: frozenset[str] = frozenset(PLAN_DIR.values())

MEDIA_DIR: dict[MediaKey, str] = {
    "text": "Texts",
    "image": "Images",
    "video": "Videos",
}

MEDIA_EXT: dict[MediaKey, str] = {
    "text": ".txt",
    "image": ".png",
    "video": ".mp4",
}

PLATFORM_DIR: dict[PlatformKey, str] = {
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "x": "X",
    "whatsapp": "WhatsApp",
}

PLAN_SUFFIX: dict[PlanKey, str] = {
    "core": "C",
    "hybrid": "H",
    "ai_budget": "AIB",
    "ai_pro": "AI",
}


def calendar_total_days_env() -> int:
    raw = (os.environ.get("CUSEAR_CALENDAR_TOTAL_DAYS") or "30").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 30
    return max(1, min(n, 366))


def downloads_dir() -> Path:
    """Best-effort user Downloads folder (macOS/Windows/Linux)."""
    home = Path.home()
    cand = home / "Downloads"
    if cand.exists():
        return cand
    up = (os.environ.get("USERPROFILE") or "").strip()
    if up:
        win = Path(up) / "Downloads"
        if win.exists():
            return win
    return cand


def _safe_filename(text: str, *, fallback: str = "asset") -> str:
    s = re.sub(r"[^A-Za-z0-9_. -]+", "", str(text or "").strip()).strip()
    return s or fallback


def vault_root(downloads_base: Path | None = None) -> Path:
    dl = downloads_base if downloads_base is not None else downloads_dir()
    return dl / VAULT_DIR_NAME


def slot_stem(*, plan: PlanKey, day: int) -> str:
    return f"{int(day)}{PLAN_SUFFIX[plan]}"


def slot_path(
    downloads_base: Path | None,
    *,
    plan: PlanKey,
    media: MediaKey,
    day: int,
    platform: PlatformKey | None = None,
) -> Path:
    root = vault_root(downloads_base)
    ext = MEDIA_EXT[media]
    stem = slot_stem(plan=plan, day=day)
    if plan == "ai_pro":
        if not platform:
            raise ValueError("platform required for ai_pro")
        return root / PLAN_DIR[plan] / PLATFORM_DIR[platform] / MEDIA_DIR[media] / f"{stem}{ext}"
    return root / PLAN_DIR[plan] / MEDIA_DIR[media] / f"{stem}{ext}"


def ensure_vault_root(downloads_base: Path | None = None) -> Path:
    """Create ``Downloads/cusear`` only (no plan subfolders)."""
    root = vault_root(downloads_base)
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_plan_vault(
    downloads_base: Path | None,
    plan: PlanKey,
    *,
    platform: PlatformKey | None = None,
    create_stubs: bool = True,
) -> dict[str, Any]:
    """
    Create directories (and optional empty day stubs) for a single plan only.

    For ``ai_pro``, pass ``platform`` to create **only** that platform’s tree; omit
    ``platform`` only when you intend to prepare every platform (rare).
    """
    root = ensure_vault_root(downloads_base)
    total = calendar_total_days_env()
    stubs = 0
    skip_touch = (os.environ.get("CUSEAR_SKIP_30DAY_TOUCH") or "").strip().lower() in ("1", "true", "yes")

    if plan in ("core", "hybrid", "ai_budget"):
        plan_dir = root / PLAN_DIR[plan]
        for media in ("text", "image", "video"):
            (plan_dir / MEDIA_DIR[media]).mkdir(parents=True, exist_ok=True)
        if create_stubs and not skip_touch:
            for day in range(1, total + 1):
                for media in ("text", "image", "video"):
                    p = slot_path(downloads_base, plan=plan, media=media, day=day)  # type: ignore[arg-type]
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        try:
                            p.touch()
                            stubs += 1
                        except OSError:
                            pass
        return {
            "ok": True,
            "root": str(root.resolve()),
            "plan": plan,
            "platform": "",
            "stub_files_created": stubs,
        }

    if plan == "ai_pro":
        if platform and platform in PLATFORM_DIR:
            plats: tuple[PlatformKey, ...] = (platform,)  # type: ignore[assignment]
        else:
            plats = tuple(PLATFORM_DIR.keys())  # type: ignore[assignment]
        for plat in plats:
            ai_base = root / PLAN_DIR["ai_pro"] / PLATFORM_DIR[plat]
            for media in ("text", "image", "video"):
                (ai_base / MEDIA_DIR[media]).mkdir(parents=True, exist_ok=True)
            if create_stubs and not skip_touch:
                for day in range(1, total + 1):
                    for media in ("text", "image", "video"):
                        p = slot_path(downloads_base, plan="ai_pro", platform=plat, media=media, day=day)
                        if not p.exists():
                            try:
                                p.touch()
                                stubs += 1
                            except OSError:
                                pass
        return {
            "ok": True,
            "root": str(root.resolve()),
            "plan": plan,
            "platform": platform or ",".join(plats),
            "stub_files_created": stubs,
        }

    raise ValueError(f"unknown plan: {plan}")


def ensure_vault_dirs(downloads_base: Path | None = None) -> None:
    root = vault_root(downloads_base)
    root.mkdir(parents=True, exist_ok=True)
    for plan in ("core", "hybrid", "ai_budget"):
        plan_dir = root / PLAN_DIR[plan]  # type: ignore[index]
        for media in ("text", "image", "video"):
            (plan_dir / MEDIA_DIR[media]).mkdir(parents=True, exist_ok=True)  # type: ignore[index]
    ai_pro_root = root / PLAN_DIR["ai_pro"]
    for plat in PLATFORM_DIR.values():
        for media_dir in MEDIA_DIR.values():
            (ai_pro_root / plat / media_dir).mkdir(parents=True, exist_ok=True)


def ensure_slot_stubs(downloads_base: Path | None = None) -> int:
    if (os.environ.get("CUSEAR_SKIP_30DAY_TOUCH") or "").strip().lower() in ("1", "true", "yes"):
        return 0
    total = calendar_total_days_env()
    n = 0
    for day in range(1, total + 1):
        for plan in ("core", "hybrid", "ai_budget"):
            for media in ("text", "image", "video"):
                p = slot_path(downloads_base, plan=plan, media=media, day=day)  # type: ignore[arg-type]
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists():
                    continue
                try:
                    p.touch()
                    n += 1
                except Exception:
                    pass
        for plat in PLATFORM_DIR.keys():
            for media in ("text", "image", "video"):
                p = slot_path(downloads_base, plan="ai_pro", platform=plat, media=media, day=day)  # type: ignore[arg-type]
                p.parent.mkdir(parents=True, exist_ok=True)
                if p.exists():
                    continue
                try:
                    p.touch()
                    n += 1
                except Exception:
                    pass
    return n


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(path.name, fallback="slot")
    tmp = path.with_name(f".tmp_{secrets.token_hex(8)}_{stem}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    if not path.is_file():
        raise OSError(f"atomic write failed (missing target): {path}")
    sz = path.stat().st_size
    if sz != len(data):
        raise OSError(f"atomic write size mismatch for {path}: expected {len(data)} got {sz}")


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


@dataclass(frozen=True)
class VaultBootstrapResult:
    ok: bool
    root: str
    total_days: int
    stub_files_created: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "root": str(self.root),
            "total_days": int(self.total_days),
            "stub_files_created": int(self.stub_files_created),
            "detail": str(self.detail or ""),
        }


def bootstrap_storage_vault(
    downloads_base: Path | None = None,
    *,
    layout: Literal["minimal", "full"] = "minimal",
) -> dict[str, Any]:
    """
    ``minimal`` (default): only ``Downloads/cusear`` — plan folders are created when
    you pick a plan (see ``ensure_plan_vault``).

    ``full``: legacy layout — all four plans, all AI Pro platforms, stubs, and docs.
    """
    root = ensure_vault_root(downloads_base)
    if layout == "minimal":
        return VaultBootstrapResult(
            ok=True,
            root=str(root.resolve()),
            total_days=calendar_total_days_env(),
            stub_files_created=0,
            detail="minimal_root_only",
        ).as_dict()
    ensure_vault_dirs(downloads_base)
    stubs = ensure_slot_stubs(downloads_base)
    if (os.environ.get("CUSEAR_STORAGE_WRITE_DOCS") or "").strip().lower() in ("1", "true", "yes"):
        _write_content_layout_readme(downloads_base)
    return VaultBootstrapResult(
        ok=True,
        root=str(root.resolve()),
        total_days=calendar_total_days_env(),
        stub_files_created=stubs,
        detail="full_all_plans",
    ).as_dict()


def _write_content_layout_readme(downloads_base: Path | None = None) -> Path:
    root = vault_root(downloads_base)
    out = root / "CONTENT_LAYOUT.txt"
    total = calendar_total_days_env()
    body = (
        "cusear™ — STORAGE vault (plans × slots)\n"
        "======================================\n\n"
        f"Slots: days 1–{total}. Files are created once as empty stubs; uploading or generating replaces content.\n\n"
        "PLANS\n"
        "-----\n"
        "Core/\n"
        "  Texts/  1C.txt … 30C.txt\n"
        "  Images/ 1C.png … 30C.png\n"
        "  Videos/ 1C.mp4 … 30C.mp4\n\n"
        "Hybrid/\n"
        "  Texts/  1H.txt … 30H.txt\n"
        "  Images/ 1H.png … 30H.png\n"
        "  Videos/ 1H.mp4 … 30H.mp4\n\n"
        "AI Budget/\n"
        "  Texts/  1AIB.txt … 30AIB.txt\n"
        "  Images/ 1AIB.png … 30AIB.png\n"
        "  Videos/ 1AIB.mp4 … 30AIB.mp4\n\n"
        "AI Pro/\n"
        "  <Platform>/Texts/  1AI.txt … 30AI.txt\n"
        "  <Platform>/Images/ 1AI.png … 30AI.png\n"
        "  <Platform>/Videos/ 1AI.mp4 … 30AI.mp4\n\n"
        "Platforms: Facebook, LinkedIn, Instagram, X, WhatsApp.\n"
    )
    try:
        out.write_text(body, encoding="utf-8")
    except OSError:
        pass
    return out


def list_cusear_legacy_top_level(downloads_base: Path | None = None) -> dict[str, Any]:
    """
    Names under ``Downloads/cusear`` that are **not** part of the current plan vault
    (e.g. old per-workflow folders such as ``Instagram_Post``).
    """
    root = vault_root(downloads_base)
    if not root.is_dir():
        return {"ok": True, "root": str(root.resolve()), "legacy_dirs": [], "legacy_files": []}
    legacy_dirs: list[str] = []
    legacy_files: list[str] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir() and p.name not in PLAN_TOP_LEVEL_DIR_NAMES:
            legacy_dirs.append(p.name)
        elif p.is_file() and p.name.lower() == "content_layout.txt":
            legacy_files.append(p.name)
    return {
        "ok": True,
        "root": str(root.resolve()),
        "legacy_dirs": legacy_dirs,
        "legacy_files": legacy_files,
    }


def cleanup_cusear_legacy_top_level(
    downloads_base: Path | None = None,
    *,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """
    Remove legacy top-level entries: any **directory** whose name is not one of the four plan
    folders, plus ``CONTENT_LAYOUT.txt`` if present.

    If ``names`` is set, only those entries are removed (each must be a legacy name, not a plan dir).
    """
    root = vault_root(downloads_base)
    removed: list[str] = []
    errors: list[str] = []
    if not root.is_dir():
        return {"ok": True, "root": str(root.resolve()), "removed": [], "errors": []}

    targets: list[Path] = []
    if names:
        for raw in names:
            nm = str(raw or "").strip()
            if not nm or nm in PLAN_TOP_LEVEL_DIR_NAMES:
                continue
            p = root / nm
            if p.exists():
                targets.append(p)
    else:
        for p in root.iterdir():
            if p.is_dir() and p.name not in PLAN_TOP_LEVEL_DIR_NAMES:
                targets.append(p)
            elif p.is_file() and p.name.lower() == "content_layout.txt":
                targets.append(p)

    for p in sorted(targets, key=lambda x: str(x)):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.is_file():
                p.unlink()
            else:
                continue
            removed.append(p.name)
        except OSError as exc:
            errors.append(f"{p.name}: {exc}")
    return {
        "ok": not errors,
        "root": str(root.resolve()),
        "removed": removed,
        "errors": errors,
    }

