import os
from pathlib import Path

import pytest


def test_bootstrap_storage_vault_creates_expected_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSEAR_CALENDAR_TOTAL_DAYS", "30")
    monkeypatch.delenv("CUSEAR_SKIP_30DAY_TOUCH", raising=False)

    from cusear.storage_vault import bootstrap_storage_vault, slot_path, vault_root

    r = bootstrap_storage_vault(tmp_path, layout="full")
    assert r.get("ok") is True
    root = vault_root(tmp_path)
    assert root.exists() and root.is_dir()

    p1 = slot_path(tmp_path, plan="core", media="text", day=1)
    p2 = slot_path(tmp_path, plan="hybrid", media="image", day=30)
    p3 = slot_path(tmp_path, plan="ai_budget", media="video", day=7)
    p4 = slot_path(tmp_path, plan="ai_pro", platform="instagram", media="text", day=12)
    assert p1.is_file()
    assert p2.is_file()
    assert p3.is_file()
    assert p4.is_file()


def test_bootstrap_storage_vault_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSEAR_CALENDAR_TOTAL_DAYS", "30")
    monkeypatch.delenv("CUSEAR_SKIP_30DAY_TOUCH", raising=False)

    from cusear.storage_vault import bootstrap_storage_vault

    first = bootstrap_storage_vault(tmp_path, layout="full")
    assert first.get("ok") is True
    second = bootstrap_storage_vault(tmp_path, layout="full")
    assert second.get("ok") is True
    assert int(second.get("stub_files_created") or 0) == 0


def test_minimal_bootstrap_then_ensure_hybrid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSEAR_CALENDAR_TOTAL_DAYS", "30")
    monkeypatch.delenv("CUSEAR_SKIP_30DAY_TOUCH", raising=False)

    from cusear.storage_vault import bootstrap_storage_vault, ensure_plan_vault, slot_path, vault_root

    r = bootstrap_storage_vault(tmp_path, layout="minimal")
    assert r.get("ok") is True
    root = vault_root(tmp_path)
    assert root.is_dir()
    assert not (root / "Core").exists()

    ensure_plan_vault(tmp_path, "hybrid")
    assert (root / "Hybrid" / "Texts").is_dir()
    p1 = slot_path(tmp_path, plan="hybrid", media="text", day=1)
    assert p1.is_file()


def test_cleanup_legacy_top_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cusear.storage_vault import (
        bootstrap_storage_vault,
        cleanup_cusear_legacy_top_level,
        list_cusear_legacy_top_level,
        vault_root,
    )

    bootstrap_storage_vault(tmp_path, layout="minimal")
    root = vault_root(tmp_path)
    (root / "Instagram_Post").mkdir()
    (root / "CONTENT_LAYOUT.txt").write_text("old", encoding="utf-8")
    listed = list_cusear_legacy_top_level(tmp_path)
    assert "Instagram_Post" in listed["legacy_dirs"]
    assert "CONTENT_LAYOUT.txt" in listed["legacy_files"]

    out = cleanup_cusear_legacy_top_level(tmp_path)
    assert "Instagram_Post" in out["removed"]
    assert "CONTENT_LAYOUT.txt" in out["removed"]
    assert not (root / "Instagram_Post").exists()
    assert not (root / "CONTENT_LAYOUT.txt").exists()

