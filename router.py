"""
router.py
---------
Tier-1 Router for B17 Finance Multi-Agent

Goal:
- Identify the HIGH-LEVEL category of the user's finance question.
- This determines which agents (Analyst, Researcher) emphasize which topics.

This is a rule-based (keyword) router, not a learned classifier. The
keyword lists live in config/routing.toml.

Output:
A simple list of tags, e.g.:
    ["macro", "rates"]
    ["credit"]
    ["portfolio"]
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, List, Tuple

from settings import load_config


def normalize_text(text: str) -> str:
    """Lower-case and treat hyphens as spaces ("high-yield" -> "high yield")."""
    return re.sub(r"\s+", " ", (text or "").lower().replace("-", " ")).strip()


@lru_cache(maxsize=None)
def _pattern(keywords: Tuple[str, ...]) -> re.Pattern:
    """Whole-word pattern for any keyword, allowing a plural "s"/"es"."""
    alts = sorted((re.escape(normalize_text(k)) for k in keywords), key=len, reverse=True)
    return re.compile(r"(?<![\w/])(?:" + "|".join(alts) + r")(?:s|es)?(?![\w/])")


def matches_any(text: str, keywords: Iterable[str], exclude_phrases: Iterable[str] = ()) -> bool:
    """True if any keyword appears as a whole word in text (after removing excluded phrases)."""
    keywords = tuple(keywords)
    if not keywords:
        return False
    q = normalize_text(text)
    exclude_phrases = tuple(exclude_phrases)
    if exclude_phrases:
        q = _pattern(exclude_phrases).sub(" ", q)
    return bool(_pattern(keywords).search(q))


def route_question(question: str) -> List[str]:
    cfg = load_config("routing")
    tags = [
        tag for tag in cfg["tag_order"]
        if matches_any(question, cfg["tier1"][tag]["keywords"], cfg["tier1"][tag]["exclude_phrases"])
    ]

    # If nothing matched → fallback (macro)
    return tags or list(cfg["fallback_tags"])
