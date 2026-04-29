"""
Pricing policy — gross margin vs variable costs (INR) and FX helpers.
cusear™ Platform · API Layer

Margin basis (locked): variable costs = AI COGS estimate + payment fees + per-run infra share.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class VariableCostModel:
    """Estimated variable cost per AI-heavy month (INR), before payment processing."""

    ai_cost_per_run_inr: float
    expected_ai_runs_per_month: int
    payment_fee_percent: float
    payment_fee_fixed_inr_per_run: float
    infra_share_inr_per_run: float

    @classmethod
    def from_env(cls) -> "VariableCostModel":
        return cls(
            ai_cost_per_run_inr=env_float("VARIABLE_COST_AI_RUN_INR", 15.0),
            expected_ai_runs_per_month=env_int("PLAN_EXPECTED_AI_RUNS_MONTH", 600),
            payment_fee_percent=env_float("PAYMENT_FEE_PERCENT", 2.5),
            payment_fee_fixed_inr_per_run=env_float("PAYMENT_FEE_FIXED_INR_PER_RUN", 0.0),
            infra_share_inr_per_run=env_float("INFRA_SHARE_INR_PER_RUN", 0.5),
        )


def variable_cost_per_ai_run_inr(model: VariableCostModel | None = None) -> float:
    m = model or VariableCostModel.from_env()
    base = m.ai_cost_per_run_inr + m.infra_share_inr_per_run
    fee = base * (m.payment_fee_percent / 100.0) + m.payment_fee_fixed_inr_per_run
    return base + fee


def monthly_variable_cost_inr(
    *,
    expected_ai_runs: int,
    model: VariableCostModel | None = None,
) -> float:
    m = model or VariableCostModel.from_env()
    per = variable_cost_per_ai_run_inr(m)
    return max(0.0, float(expected_ai_runs)) * per


def gross_margin_ratio(*, revenue_inr: float, variable_cost_inr: float) -> float | None:
    if revenue_inr <= 0:
        return None
    return (revenue_inr - variable_cost_inr) / revenue_inr


def minimum_margin_floor() -> float:
    """Default 0.90 — block publish/save below this projected margin."""
    return max(0.0, min(1.0, env_float("MIN_GROSS_MARGIN_FLOOR", 0.90)))


def margin_warning_threshold() -> float:
    return max(0.0, min(1.0, env_float("MARGIN_WARN_BELOW", 0.93)))


def validate_plan_margin(
    *,
    plan_price_inr: float,
    expected_ai_runs_per_month: int,
    model: VariableCostModel | None = None,
) -> dict[str, Any]:
    """
    Returns margin analysis for a subscription plan priced in INR.

    projected_variable_cost uses expected AI runs × per-run variable cost.
    Non-AI unlimited usage is not modeled here (zero marginal API cost assumption).
    """
    m = model or VariableCostModel.from_env()
    vc = monthly_variable_cost_inr(expected_ai_runs=expected_ai_runs_per_month, model=m)
    margin = gross_margin_ratio(revenue_inr=plan_price_inr, variable_cost_inr=vc)
    floor = minimum_margin_floor()
    warn = margin_warning_threshold()
    ok = margin is None or margin >= floor
    warn_flag = margin is not None and floor <= margin < warn
    return {
        "plan_price_inr": plan_price_inr,
        "expected_ai_runs_per_month": expected_ai_runs_per_month,
        "variable_cost_inr_month": round(vc, 4),
        "gross_margin": None if margin is None else round(margin, 6),
        "margin_ok": ok,
        "margin_floor": floor,
        "margin_warning": warn_flag,
        "warn_threshold": warn,
        "cost_model": {
            "ai_cost_per_run_inr": m.ai_cost_per_run_inr,
            "payment_fee_percent": m.payment_fee_percent,
            "payment_fee_fixed_inr_per_run": m.payment_fee_fixed_inr_per_run,
            "infra_share_inr_per_run": m.infra_share_inr_per_run,
            "variable_cost_per_ai_run_inr": round(variable_cost_per_ai_run_inr(m), 6),
        },
    }


def inr_to_usd(amount_inr: float, fx_inr_per_usd: float) -> float | None:
    if fx_inr_per_usd <= 0:
        return None
    return amount_inr / fx_inr_per_usd


def usd_to_inr(amount_usd: float, fx_inr_per_usd: float) -> float | None:
    if fx_inr_per_usd <= 0:
        return None
    return amount_usd * fx_inr_per_usd
