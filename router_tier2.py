"""
router_tier2.py
---------------
Tier-2 specialization router for B17 Finance Multi-Agent System.

Purpose:
- Provide *more granular* classification after Tier-1
- Enable more accurate planning + analyst agent behavior
- Keep it lightweight and deterministic (no LLM here); the rules live in
  config/routing.toml and use the same whole-word matching as router.py

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

from router import matches_any
from settings import load_config


def detect_tier2_specialization(question: str) -> Dict[str, str]:
    cfg = load_config("routing")
    out: Dict[str, str] = {}

    for tag in cfg["tag_order"]:
        # first matching rule wins for each tag
        for rule in cfg["tier2"].get(tag, []):
            if matches_any(question, rule["keywords"]):
                out[tag] = rule["label"]
                break

    # Default fallback
    return out or dict(cfg["tier2_fallback"])
