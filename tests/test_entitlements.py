"""Entitlement inference and enforcement flag."""

from __future__ import annotations

import os

import pytest

from agency_api import entitlements


def test_infer_requires_ai_from_smart_mode():
    wf = {"steps": [{"action_type": "click", "step": 1}]}
    assert entitlements.infer_requires_ai(wf, "smart") is True
    assert entitlements.infer_requires_ai(wf, "fast") is False


def test_infer_requires_ai_from_ai_type_step():
    wf = {"steps": [{"action_type": "ai_type", "step": 1}]}
    assert entitlements.infer_requires_ai(wf, "fast") is True


def test_enforcement_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENTITLEMENTS_ENFORCEMENT", raising=False)
    assert entitlements.enforcement_enabled() is False
