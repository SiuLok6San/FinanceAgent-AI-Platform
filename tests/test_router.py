"""Tests for the rule-based routers (router.py, router_tier2.py)."""

import csv
from pathlib import Path

import pytest

from router import matches_any, route_question
from router_tier2 import detect_tier2_specialization

QUESTIONS = list(csv.DictReader(open(Path(__file__).resolve().parents[1] / "eval" / "questions.csv")))


# --- whole-word matching: regressions from the old substring matcher -------

@pytest.mark.parametrize(
    "question, wrong_tag",
    [
        ("Why is the corporate sector weakening?", "rates"),       # "rate" in "corporate"
        ("What does the jobs data signal?", "credit"),             # "ig" in "signal"
        ("What shape is the economy in?", "equity"),               # "pe" in "shape"
        ("What do people expect from the economy?", "equity"),     # "pe" in "people"/"expect"
        ("Why is growth slowing?", "credit"),                      # "hy" in "why"
        ("What does the unemployment rate say about the economy?", "rates"),
    ],
)
def test_substrings_inside_other_words_do_not_match(question, wrong_tag):
    assert wrong_tag not in route_question(question)


def test_hyphens_and_plurals_match():
    assert "credit" in route_question("Are high-yield spreads tight?")
    assert "credit" in route_question("Outlook for investment-grade bonds")
    assert "rates" in route_question("Where are Treasuries heading?")
    assert matches_any("credit spreads widened", ["spread"])


def test_exclude_phrases_only_affect_their_tag():
    q = "Are high-yield bonds cheap?"
    assert "credit" in route_question(q)
    assert matches_any(q, ["high yield"])


def test_fallback_is_macro():
    assert route_question("What should I think about?") == ["macro"]
    assert detect_tier2_specialization("What should I think about?") == {"general": "broad_financial_question"}


def test_multiple_tags():
    tags = route_question("How do Fed rate hikes affect bank stocks and credit spreads?")
    assert set(tags) == {"rates", "credit", "equity"}


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Is the yield curve inverted?", {"rates": "yield_curve_inversion"}),
        ("When will the Fed rate cut cycle start?", {"rates": "fed_cut_cycle"}),
        ("Is CPI inflation sticky?", {"macro": "inflation"}),
        ("IG vs HY: which is better value?", {"credit": "ig_vs_hy"}),
        ("How should I hedge my portfolio?", {"portfolio": "hedging_strategies"}),
        ("Is Apple's P/E too high?", {"equity": "valuation_pressure"}),
    ],
)
def test_tier2_examples(question, expected):
    out = detect_tier2_specialization(question)
    for tag, label in expected.items():
        assert out.get(tag) == label


# --- the approved evaluation questions -------------------------------------

@pytest.mark.parametrize("row", QUESTIONS, ids=[r["id"] for r in QUESTIONS])
def test_primary_category_is_routed(row):
    """Each question's own category must be among the router's tags.

    Exact-set accuracy (including secondary tags) is reported in
    eval/RESULTS.md rather than asserted, because some misses are expected.
    """
    assert row["category"] in route_question(row["question"])
