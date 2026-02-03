"""
router_tier2.py
---------------
Tier-2 specialization router for B17 Finance Multi-Agent System.

Purpose:
- Provide *more granular* classification after Tier-1
- Enable more accurate planning + analyst agent behavior
- Keep it lightweight and deterministic (no LLM here)

Output example:
    {
        "rates": "yield_curve_inversion",
        "macro": "inflation",
        "credit": "ig_vs_hy",
        "equity": "valuation_pressure",
    }
"""

from __future__ import annotations
from typing import Dict


def detect_tier2_specialization(question: str) -> Dict[str, str]:
    q = question.lower()
    out: Dict[str, str] = {}

    # ------------------------------------------------
    # RATES: Yield curve, Fed policy, duration
    # ------------------------------------------------
    if any(k in q for k in ["yield curve", "inversion", "10s2s", "curve steep", "curve flatten"]):
        out["rates"] = "yield_curve_inversion"

    elif any(k in q for k in ["fed cut", "cut cycle", "easing"]):
        out["rates"] = "fed_cut_cycle"

    elif any(k in q for k in ["fed hike", "tightening", "restrictive", "higher for longer"]):
        out["rates"] = "fed_hike_cycle"

    elif any(k in q for k in ["duration", "treasury volatility", "bond volatility"]):
        out["rates"] = "duration_volatility"


    # ------------------------------------------------
    # MACRO: Inflation, recession, growth
    # ------------------------------------------------
    if any(k in q for k in ["inflation", "cpi", "ppi", "core pce", "disinflation"]):
        out["macro"] = "inflation"

    elif any(k in q for k in ["recession", "slowdown", "hard landing", "soft landing"]):
        out["macro"] = "recession_probabilities"

    elif any(k in q for k in ["gdp", "growth", "economic expansion"]):
        out["macro"] = "growth_outlook"

    elif any(k in q for k in ["labor", "employment", "unemployment"]):
        out["macro"] = "labor_market"


    # ------------------------------------------------
    # CREDIT: IG vs HY, default cycle, spreads
    # ------------------------------------------------
    if any(k in q for k in ["ig vs hy", "ig vs. hy", "investment-grade vs high-yield"]):
        out["credit"] = "ig_vs_hy"

    elif any(k in q for k in ["default cycle", "default risk", "downgrade risk"]):
        out["credit"] = "default_cycle"

    elif any(k in q for k in ["spread widening", "spread tightening"]):
        out["credit"] = "spread_dynamics"

    elif any(k in q for k in ["leveraged loan", "clo"]):
        out["credit"] = "leveraged_loans"


    # ------------------------------------------------
    # PORTFOLIO: Allocation, hedging, risk budgeting
    # ------------------------------------------------
    if any(k in q for k in ["risk parity", "risk budgeting"]):
        out["portfolio"] = "risk_parity"

    elif any(k in q for k in ["allocation", "asset mix", "rebalance"]):
        out["portfolio"] = "allocation_framework"

    elif any(k in q for k in ["hedge", "hedging"]):
        out["portfolio"] = "hedging_strategies"

    elif any(k in q for k in ["volatility regime", "vol regime"]):
        out["portfolio"] = "volatility_regime_shifts"


    # ------------------------------------------------
    # EQUITY: Valuation, earnings, sectors
    # ------------------------------------------------
    if any(k in q for k in ["valuation", "multiple", "pe", "pb", "cash flow"]):
        out["equity"] = "valuation_pressure"

    elif any(k in q for k in ["earnings", "eps", "rev growth"]):
        out["equity"] = "earnings_cycle"

    elif any(k in q for k in ["tech sector", "financials", "energy sector"]):
        out["equity"] = "sector_focus"

    elif any(k in q for k in ["mega cap", "small cap", "growth vs value"]):
        out["equity"] = "style_factor_analysis"


    # ------------------------------------------------
    # Default fallback
    # ------------------------------------------------
    if not out:
        out["general"] = "broad_financial_question"

    return out
