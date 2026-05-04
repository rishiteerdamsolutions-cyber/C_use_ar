"""
Storage vault layout under ``Downloads/cusear/`` (global, plan-based).

Plans:

- Core:       ``Core/Texts|Images|Videos`` with ``1C.* … 30C.*``
- Hybrid:     ``Hybrid/Texts|Images|Videos`` with ``1H.* … 30H.*``
- AI Budget:  ``AI Budget/Texts|Images|Videos`` with ``1AIB.* … 30AIB.*``
- AI Pro:     ``AI Pro/<Platform>/Texts|Images|Videos`` with ``1AI.* … 30AI.*``

This module provides compatibility helpers for runtime calendar resolution and writing
slot media, but all concrete paths are defined in ``cusear.storage_vault``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cusear.storage_vault import (
    PLATFORM_DIR,
    PlanKey,
    PlatformKey,
    atomic_write_bytes,
    bootstrap_storage_vault,
    calendar_total_days_env,
    downloads_dir,
    ensure_plan_vault,
    slot_path,
    slot_stem,
    vault_root,
)

# Runtime var prefixes for resolved slot paths (populate via apply_calendar_runtime_tokens).
_CALENDAR_LAYER_VAR_PREFIX: dict[str, str] = {
    "core": "CALENDAR_CORE",
    "hybrid": "CALENDAR_HYBRID",
    "ai": "CALENDAR_AI",
}


def ensure_cusear_platform_tree(base_dir: Path | None = None) -> dict[str, dict[str, Path]]:
    """
    Paths used by platform-specific AI export.

    Returns: platform_key → {"text","image","video"} → Path under ``Downloads/cusear/AI Pro/<Platform>/…``.
    """
    root = vault_root(base_dir)
    out: dict[str, dict[str, Path]] = {}
    for plat_key, plat_name in PLATFORM_DIR.items():
        base = root / "AI Pro" / plat_name
        out[plat_key] = {
            "text": base / "Texts",
            "image": base / "Images",
            "video": base / "Videos",
        }
        for p in out[plat_key].values():
            p.mkdir(parents=True, exist_ok=True)
    return out


def bootstrap_cusear_content_folders(
    *,
    workflows_dir: Path,
    bundles_dir: Path,
    base_downloads: Path | None = None,
) -> dict[str, Any]:
    """
    Backwards-compatible name for initializing the global STORAGE vault.

    The prior implementation created per-workflow trees under ``Downloads/cusear/<flow>/<workflow>/``.
    The new system creates one global vault under ``Downloads/cusear/``.
    """
    _ = workflows_dir, bundles_dir  # retained for compatibility; no longer used
    d = bootstrap_storage_vault(base_downloads)
    return {
        "ok": bool(d.get("ok")),
        "roots_created": 1 if d.get("ok") else 0,
        "sample_root": str(d.get("root") or ""),
        "thirty_day_stub_files": int(d.get("stub_files_created") or 0),
        "total_days": int(d.get("total_days") or 30),
    }


def calendar_cycles_wrap_env() -> bool:
    raw = (os.environ.get("CUSEAR_CALENDAR_CYCLE") or "wrap").strip().lower()
    if raw in ("clamp", "stop", "0", "false", "no", "norepeat"):
        return False
    return True


def compute_calendar_day_index(runtime_vars: dict[str, str]) -> int:
    """
    Calendar slot for today's upload files (n in nC / nH / nAI).

    Prefers CURRENT_AUTOMATION_RUN (scheduled run # — run 5 → day 5) when positive.
    Else CURRENT_CAMPAIGN_DAY when positive. Otherwise 1.
    Maps into 1 … calendar_total_days_env() via wrap or clamp.
    """
    ar = str(runtime_vars.get("CURRENT_AUTOMATION_RUN") or "").strip()
    cd = str(runtime_vars.get("CURRENT_CAMPAIGN_DAY") or "").strip()
    idx = 1
    if ar.isdigit() and int(ar) > 0:
        idx = int(ar)
    elif cd.isdigit() and int(cd) > 0:
        idx = int(cd)

    total = calendar_total_days_env()
    if idx < 1:
        idx = 1
    if calendar_cycles_wrap_env():
        idx = ((idx - 1) % total) + 1
    else:
        idx = min(idx, total)
    return idx


def _calendar_plan_for_layer(layer_key: str, runtime_vars: dict[str, str]) -> tuple[PlanKey, PlatformKey | None]:
    lk = str(layer_key or "").strip().lower()
    if lk == "core":
        return "core", None
    if lk == "hybrid":
        return "hybrid", None
    # ai
    variant = str(runtime_vars.get("CALENDAR_AI_VARIANT") or runtime_vars.get("AI_VARIANT") or "budget").strip().lower()
    if variant in ("pro", "ai_pro", "aipro"):
        plat_raw = str(runtime_vars.get("CALENDAR_AI_PLATFORM") or runtime_vars.get("STORAGE_PLATFORM") or "").strip().lower()
        plat: PlatformKey | None = None
        if plat_raw in PLATFORM_DIR:
            plat = plat_raw  # type: ignore[assignment]
        return "ai_pro", plat
    return "ai_budget", None


def resolve_layer_day_paths(
    downloads_base: Path,
    *,
    layer_key: str,
    day: int,
    runtime_vars: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    rv = runtime_vars or {}
    plan, plat = _calendar_plan_for_layer(layer_key, rv)
    if plan == "ai_pro" and not plat:
        return {"image": None, "video": None, "text": None}
    try:
        p_text = slot_path(downloads_base, plan=plan, platform=plat, media="text", day=day)
        p_img = slot_path(downloads_base, plan=plan, platform=plat, media="image", day=day)
        p_vid = slot_path(downloads_base, plan=plan, platform=plat, media="video", day=day)
    except Exception:
        return {"image": None, "video": None, "text": None}
    return {
        "text": p_text if p_text.is_file() else None,
        "image": p_img if p_img.is_file() else None,
        "video": p_vid if p_vid.is_file() else None,
    }


def _path_str(p: Path | None) -> str:
    if not p:
        return ""
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def apply_calendar_runtime_tokens(
    runtime_vars: dict[str, str],
    *,
    downloads_base: Path,
    workflow_stem: str,
    workflow_label: str = "",
) -> int:
    """
    Set CURRENT_CALENDAR_DAY and CALENDAR_<CORE|HYBRID|AI>_{IMAGE|VIDEO|TEXT}_PATH in runtime_vars.

    Returns the computed calendar day (1-based).
    """
    rv = runtime_vars
    day = compute_calendar_day_index(rv)
    rv["CURRENT_CALENDAR_DAY"] = str(day)

    for layer_key in ("core", "hybrid", "ai"):
        pref = _CALENDAR_LAYER_VAR_PREFIX.get(layer_key, "CALENDAR_CORE")
        triple = resolve_layer_day_paths(downloads_base, layer_key=layer_key, day=day, runtime_vars=rv)
        rv[f"{pref}_IMAGE_PATH"] = _path_str(triple.get("image"))
        rv[f"{pref}_VIDEO_PATH"] = _path_str(triple.get("video"))
        rv[f"{pref}_TEXT_PATH"] = _path_str(triple.get("text"))
        plan, _ = _calendar_plan_for_layer(layer_key, rv)
        rv[f"{pref}_STEM"] = slot_stem(plan=plan, day=day)

    return day


def select_calendar_asset_for_upload(
    runtime_vars: dict[str, str],
    *,
    layer: str,
    pick: str,
) -> tuple[str, str, str, str]:
    """
    Resolve media_path, media_kind ('image'|'video'|'text'|''), caption_text, caption_path
    from runtime vars populated by apply_calendar_runtime_tokens.
    """
    mk = str(layer or "").strip().lower()
    if mk not in _CALENDAR_LAYER_VAR_PREFIX:
        return "", "", "", ""
    pref = _CALENDAR_LAYER_VAR_PREFIX[mk]
    img = str(runtime_vars.get(f"{pref}_IMAGE_PATH") or "").strip()
    vid = str(runtime_vars.get(f"{pref}_VIDEO_PATH") or "").strip()
    txt_path = str(runtime_vars.get(f"{pref}_TEXT_PATH") or "").strip()

    caption_text = ""
    if txt_path:
        try:
            caption_text = Path(txt_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            caption_text = ""

    pk = str(pick or "").strip().lower()
    if pk not in ("auto", "image", "video", "text"):
        pk = "auto"

    if pk == "text":
        return "", "text", caption_text, txt_path
    if pk == "image":
        return img, "image" if img else "", caption_text if img else "", txt_path if img else ""
    if pk == "video":
        return vid, "video" if vid else "", caption_text if vid else "", txt_path if vid else ""

    # auto — prefer image, then video, then text-only
    if img:
        return img, "image", caption_text, txt_path
    if vid:
        return vid, "video", caption_text, txt_path
    return "", "text", caption_text, txt_path


def _sniff_binary_media_kind(data: bytes) -> str:
    """Return 'image', 'video', or '' from leading bytes."""
    if not data or len(data) < 8:
        return ""
    if data[:3] == b"\xff\xd8\xff":
        return "image"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video"
    return ""


def write_calendar_slot_media(
    downloads_base: Path,
    *,
    flow_label: str,
    workflow_key: str,
    workflow_display: str,
    layer_key: str,
    day: int,
    slot_kind: str,
    data: bytes,
    original_filename: str = "",
    content_type: str = "",
) -> dict[str, str]:
    """
    Write one file under ``…/Downloads/cusear/<flow>/<workflow>/{Core|Hybrid|AI}/``

    Filename is ``{day}{C|H|AI}.{ext}`` (.png/.jpg/.webp, .mp4/.mov, or .txt).

    ``slot_kind``: auto | image | video | text
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 1:
        raise ValueError("empty file")

    lk = str(layer_key or "").strip().lower()
    if lk not in ("core", "hybrid", "ai"):
        raise ValueError("calendar_layer must be core, hybrid, or ai")

    total = calendar_total_days_env()
    di = int(day)
    if di < 1 or di > total:
        raise ValueError(f"day must be 1..{total}")

    sk = str(slot_kind or "auto").strip().lower()
    if sk not in ("auto", "image", "video", "text"):
        sk = "auto"

    if sk == "text":
        rk = "text"
    elif sk == "image":
        rk = "image"
    elif sk == "video":
        rk = "video"
    else:
        rk = _sniff_binary_media_kind(data)
        fnl = (original_filename or "").lower()
        if not rk:
            if fnl.endswith(".txt"):
                rk = "text"
            elif any(fnl.endswith(x) for x in (".mp4", ".mov", ".webm", ".mkv")):
                rk = "video"
            elif any(fnl.endswith(x) for x in (".png", ".jpg", ".jpeg", ".webp", ".gif")):
                rk = "image"
            else:
                ct = (content_type or "").lower()
                if "video" in ct:
                    rk = "video"
                elif ct.startswith("text/"):
                    rk = "text"
                elif "image" in ct:
                    rk = "image"
        if not rk:
            raise ValueError("could not detect file type; choose Image, Video, or Caption (.txt)")

    plan, plat = _calendar_plan_for_layer(lk, {"CALENDAR_AI_VARIANT": "budget"} if lk != "ai" else {})
    # For write APIs, allow AI Pro if caller stuffed CALENDAR_AI_VARIANT/PLATFORM into the inbound layer variables.
    # The dashboard endpoint passes runtime vars separately; this function remains a simple compatibility writer.
    if lk == "ai":
        plan = "ai_budget"
        plat = None
    if plan == "ai_pro" and plat:
        ensure_plan_vault(downloads_base, "ai_pro", platform=plat)
    elif plan in ("core", "hybrid", "ai_budget"):
        ensure_plan_vault(downloads_base, plan)  # type: ignore[arg-type]
    stem = slot_stem(plan=plan, day=di)
    media_key = rk if rk in ("text", "image", "video") else "text"
    try:
        dest = slot_path(downloads_base, plan=plan, platform=plat, media=media_key, day=di)
    except Exception:
        raise ValueError("could not resolve destination slot path")
    atomic_write_bytes(dest, bytes(data))

    return {
        "path": str(dest.resolve()),
        "stem": stem,
        "ext": dest.suffix.lstrip("."),
        "kind": rk,
        "plan": str(plan),
        "layer": lk,
        "platform": str(plat or ""),
    }
