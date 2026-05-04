"""Tests for margin and FX helpers."""

from __future__ import annotations

import pytest

from agency_api.pricing_policy import (
    VariableCostModel,
    gross_margin_ratio,
    inr_to_usd,
    minimum_margin_floor,
    validate_plan_margin,
)


def test_margin_floor_default():
    assert minimum_margin_floor() == pytest.approx(0.90)


def test_validate_plan_margin_blocks_low_price(monkeypatch):
    monkeypatch.setenv("VARIABLE_COST_AI_RUN_INR", "20")
    monkeypatch.setenv("PLAN_EXPECTED_AI_RUNS_MONTH", "100")
    monkeypatch.setenv("PAYMENT_FEE_PERCENT", "0")
    monkeypatch.setenv("PAYMENT_FEE_FIXED_INR_PER_RUN", "0")
    monkeypatch.setenv("INFRA_SHARE_INR_PER_RUN", "0")
    monkeypatch.setenv("MIN_GROSS_MARGIN_FLOOR", "0.90")
    # 100 runs * ~20 = 2000 variable; need revenue > 2000/0.1 = 20000 for 90% margin
    out = validate_plan_margin(plan_price_inr=1000, expected_ai_runs_per_month=100)
    assert out["margin_ok"] is False
    assert out["gross_margin"] is not None
    assert out["gross_margin"] < 0.90


def test_validate_plan_margin_allows_high_price(monkeypatch):
    monkeypatch.setenv("VARIABLE_COST_AI_RUN_INR", "10")
    monkeypatch.setenv("PLAN_EXPECTED_AI_RUNS_MONTH", "10")
    monkeypatch.setenv("PAYMENT_FEE_PERCENT", "0")
    monkeypatch.setenv("INFRA_SHARE_INR_PER_RUN", "0")
    monkeypatch.setenv("MIN_GROSS_MARGIN_FLOOR", "0.90")
    out = validate_plan_margin(plan_price_inr=5000, expected_ai_runs_per_month=10)
    assert out["margin_ok"] is True


def test_inr_to_usd():
    assert inr_to_usd(8300, 83) == pytest.approx(100.0)


def test_gross_margin_ratio_none_on_zero_revenue():
    assert gross_margin_ratio(revenue_inr=0, variable_cost_inr=10) is None
