"""
router.py
---------
Tier-1 Router for B17 Finance Multi-Agent

Goal:
- Identify the HIGH-LEVEL category of the user's finance question.
- This determines which agents (Analyst, Researcher) emphasize which topics.

Output:
A simple list of tags, e.g.:
    ["macro", "rates"]
    ["credit"]
    ["portfolio"]
"""

from __future__ import annotations
from typing import List


def route_question(question: str) -> List[str]:
    q = question.lower()
    tags = []

    # --------------------
    # Macro Economics
    # --------------------
    macro_keywords = [
        "inflation", "cpi", "ppi", "gdp", "growth", "economy", "labor",
        "employment", "unemployment", "productivity", "recession",
        "macro", "economic outlook", "macro outlook"
    ]
    if any(k in q for k in macro_keywords):
        tags.append("macro")

    # --------------------
    # Rates / Yield Curve / Fed
    # --------------------
    rates_keywords = [
        "rate", "rates", "fed", "monetary policy", "yield",
        "curve", "yield curve", "bonds", "treasury", "duration",
        "higher for longer", "ffr", "federal reserve"
    ]
    if any(k in q for k in rates_keywords):
        tags.append("rates")

    # --------------------
    # Credit (IG / HY / Spreads / Defaults)
    # --------------------
    credit_keywords = [
        "credit", "spread", "ig", "investment grade", "high yield",
        "hy", "default", "default risk", "credit cycle", "credit spread"
    ]
    if any(k in q for k in credit_keywords):
        tags.append("credit")

    # --------------------
    # Portfolio / Asset Allocation
    # --------------------
    portfolio_keywords = [
        "portfolio", "allocation", "diversification", "var", "sharpe",
        "risk parity", "asset mix", "hedge", "rebalance", "returns",
        "risk budgeting"
    ]
    if any(k in q for k in portfolio_keywords):
        tags.append("portfolio")

    # --------------------
    # Equities / Valuation / Fundamentals
    # --------------------
    equity_keywords = [
        "stock", "equity", "valuation", "eps", "earnings",
        "multiple", "pe", "pb", "cash flow", "fundamental"
    ]
    if any(k in q for k in equity_keywords):
        tags.append("equity")

    # --------------------
    # If nothing matched → fallback macro
    # --------------------
    if not tags:
        tags.append("macro")

    return tags
