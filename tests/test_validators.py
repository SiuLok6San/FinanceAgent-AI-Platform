"""Unit tests for the deterministic validation layer (validators.py, facts.py)."""

from datetime import date

import pandas as pd
import pytest

import validators as v
from facts import all_facts, key_data_block


def fred_table(sid, values, dates, units="Percent", frequency="monthly", topics=("macro",)):
    return {
        "title": f"FRED: {sid}",
        "data": pd.DataFrame({"date": pd.to_datetime(dates), "value": values}),
        "provider": "FRED",
        "kind": "timeseries",
        "series_id": sid,
        "frequency": frequency,
        "units": units,
        "last_date": dates[-1],
        "topics": list(topics),
        "source_url": f"https://fred.stlouisfed.org/series/{sid}",
    }


def source(i, sid=None, url=None, snippet=""):
    return {"id": f"S{i}", "title": sid or f"src{i}", "url": url or f"https://fred.stlouisfed.org/series/{sid}",
            "source": "FRED", "snippet": snippet}


MONTHS = [f"2025-{m:02d}-01" for m in range(9, 13)] + [f"2026-{m:02d}-01" for m in range(1, 10)]  # 13 obs
UNRATE = fred_table("UNRATE", [4.1, 4.1, 4.2, 4.2, 4.0, 4.1, 4.1, 4.2, 4.2, 4.1, 4.2, 4.2, 4.3], MONTHS)
HY = fred_table("BAMLH0A0HYM2", [3.10, 3.05, 3.30], ["2026-10-13", "2026-10-14", "2026-10-15"], frequency="daily", topics=("credit",))
IG = fred_table("BAMLC0A0CM", [0.80, 0.82, 0.85], ["2026-10-13", "2026-10-14", "2026-10-15"], frequency="daily", topics=("credit",))
CPI = fred_table("CPIAUCSL", [310.0] + [312.0] * 11 + [319.3], MONTHS, units="Index 1982-1984=100")
SOURCES = [source(1, "UNRATE"), source(2, "BAMLH0A0HYM2"), source(3, "BAMLC0A0CM"), source(4, "CPIAUCSL")]
TABLES = [UNRATE, HY, IG, CPI]


def statuses(report, question="What is happening?", tables=TABLES, sources=SOURCES):
    return {f.text: f.status for f in v.check_numeric_grounding(report, question, tables, sources)}


# --- number extraction -------------------------------------------------------

def test_extracts_values_with_units():
    figs = v.extract_figures("Yields rose 25 bps to 4.12%; P/E 14.2x; cap $3.5 trillion; 0.2 percentage points.")
    got = [(f.text, f.unit, f.value) for f in figs]
    assert got == [
        ("25 bps", "bp", 25.0), ("4.12%", "%", 4.12), ("14.2x", "x", 14.2),
        ("$3.5 trillion", "$", 3.5e12), ("0.2 percentage points", "pp", 0.2),
    ]


@pytest.mark.parametrize("text", [
    "## 1. Executive Summary",
    "See Table 2 and Chart 3.",
    "The 10-year and 2-year yields; 10-K filings; COVID-19.",
    "Over the next 12 months and past 6 weeks.",
    "In 2026 and in Q3 2026 and FY2025.",
    "The S&P 500 and a 60/40 portfolio.",
    "Cited [S12] at https://fred.stlouisfed.org/series/DGS10",
    "The 2s10s curve and DGS10 and T10Y2Y.",
])
def test_labels_are_not_extracted(text):
    assert [f.text for f in v.extract_figures(text) if f.unit != "date"] == []


def test_rounding_tolerance_follows_digits_shown():
    a, b = v.extract_figures("4% and 4.25%")
    assert a.tolerance == pytest.approx(0.5)
    assert b.tolerance == pytest.approx(0.005)


# --- numeric grounding -------------------------------------------------------

def test_verified_with_rounding():
    assert statuses("Unemployment is 4.3% [S1].")["4.3%"] == "verified"
    assert statuses("HY OAS is 3.3% [S2].")["3.3%"] == "verified"   # 3.30 shown as 3.3


def test_basis_points_and_fractions_convert_to_percent():
    s = statuses("HY spreads are 330 bps [S2]. IG spreads at 0.0085 [S3].")
    assert s["330 bps"] == "verified"         # 3.30% = 330 bp
    assert s["0.0085"] == "verified"           # 0.0085 = 0.85%


def test_derived_changes_and_spreads():
    s = statuses("HY spreads widened 25 bps in a day [S2]. HY minus IG is 2.45 percentage points. "
                 "Unemployment is up 0.2 pp over a year [S1]. CPI rose 3.0% year over year [S4].")
    assert s["25 bps"] == "derived"       # 3.30 - 3.05
    assert s["2.45 percentage points"] == "derived"  # 3.30 - 0.85
    assert s["0.2 pp"] == "derived"       # 4.3 - 4.1 (12 months earlier)
    assert s["3.0%"] == "derived"         # 319.3 / 310 - 1


def test_unverified_number():
    assert statuses("Unemployment will hit 5.7% next year [S1].")["5.7%"] == "unverified"


def test_unit_mismatch_does_not_verify():
    # 4.3 is a percent series; "$4.30" must not match it
    assert statuses("Shares cost $4.30.")["$4.30"] == "unverified"


def test_numbers_from_question_are_ignored():
    figs = v.check_numeric_grounding("A 7.5% stress case [S1].", "What if unemployment hits 7.5%?", TABLES, SOURCES)
    assert figs == []


def test_number_found_only_in_web_snippet_is_noted():
    sources = SOURCES + [source(5, url="https://news/x", snippet="2026-10-15: Defaults rose to 4.8% in September.")]
    fig = v.check_numeric_grounding("Default rates are 4.8% [S5].", "q", TABLES, sources)[0]
    assert fig.status == "unverified" and fig.in_source_text == "S5"


def test_dates_are_checked_against_observations():
    s = statuses("As of September 2026, the rate was 4.3% [S1]; on 2026-10-15 HY OAS was 3.3% [S2]. In March 2024 it was low.")
    assert s["September 2026"] == "verified"
    assert s["2026-10-15"] == "verified"
    assert s["March 2024"] == "unverified"


def test_topic_match_prefers_series_named_in_sentence():
    fig = v.check_numeric_grounding("The HY spread is 3.05% [S2].", "q", TABLES, SOURCES)[0]
    assert fig.matched.startswith("BAMLH0A0HYM2") and fig.topic_match is True


# --- citations -----------------------------------------------------------------

def test_dangling_and_uncited_and_mismatched():
    report = ("- Unemployment is 4.3% [S9].\n"
              "- HY OAS is 3.3%.\n"
              "- IG OAS is 0.85% [S1].\n")
    figs = v.check_numeric_grounding(report, "q", TABLES, SOURCES)
    c = v.check_citations(report, SOURCES, figs)
    assert c["dangling"] == ["S9"]
    assert [u["figures"] for u in c["uncited_paragraphs"]] == [["3.3%"]]
    mism = {m["figures"][0]: m["cited"] for m in c["source_mismatches"]}
    assert mism == {"4.3%": ["S9"], "0.85%": ["S1"]}


def test_citing_either_series_of_a_spread_is_fine():
    report = "HY minus IG is 2.45 percentage points [S3]."
    figs = v.check_numeric_grounding(report, "q", TABLES, SOURCES)
    assert figs[0].source_ids == ("S2", "S3")
    assert v.check_citations(report, SOURCES, figs)["source_mismatches"] == []


def test_figure_also_in_cited_snippet_is_not_a_mismatch():
    sources = SOURCES + [source(5, url="https://news/fed", snippet="Unemployment held at 4.3% in September.")]
    report = "Unemployment is 4.3% [S5]."
    figs = v.check_numeric_grounding(report, "q", TABLES, sources)
    assert figs[0].status == "verified" and figs[0].source_id == "S1"
    assert v.check_citations(report, sources, figs)["source_mismatches"] == []


def test_grouped_citation_markers():
    report = "Unemployment is 4.3% [S1, S2]."
    figs = v.check_numeric_grounding(report, "q", TABLES, SOURCES)
    c = v.check_citations(report, SOURCES, figs)
    assert c["dangling"] == [] and c["uncited_paragraphs"] == [] and c["source_mismatches"] == []


# --- freshness -------------------------------------------------------------------

def test_freshness_counts_from_end_of_period():
    today = date(2026, 10, 17)
    rows = {r["table"]: r for r in v.check_freshness(TABLES, today)}
    assert rows["FRED: UNRATE"]["age_days"] == 17          # September ends 2026-09-30
    assert rows["FRED: UNRATE"]["status"] == "ok"
    assert rows["FRED: BAMLH0A0HYM2"]["status"] == "ok"


def test_stale_series_are_flagged():
    old_monthly = fred_table("UNRATE", [4.0, 4.1, 4.2], ["2026-05-01", "2026-06-01", "2026-07-01"])
    old_daily = fred_table("DGS10", [4.0, 4.1, 4.2], ["2026-10-01", "2026-10-02", "2026-10-05"], frequency="daily")
    rows = v.check_freshness([old_monthly, old_daily], date(2026, 10, 17))
    assert [(r["age_days"], r["status"]) for r in rows] == [(78, "stale"), (12, "stale")]


def test_frequency_is_inferred_when_missing():
    t = dict(UNRATE, frequency=None)
    assert v.check_freshness([t], date(2026, 10, 17))[0]["frequency"] == "monthly"


# --- no-data honesty ---------------------------------------------------------------

def test_missing_topic_without_disclosure_is_an_issue():
    report = "The 10-year Treasury yield is 4.5%. Unemployment is 4.3% [S1]."
    figs = v.check_numeric_grounding(report, "q", [UNRATE], SOURCES[:1])
    nd = v.check_no_data(report, ["macro", "rates"], [UNRATE], figs)
    assert nd["issue"] is True
    rates = nd["topics_without_data"][0]
    assert rates == {"topic": "rates", "disclosed": False, "unverified_figures": ["4.5%"]}


def test_disclosed_missing_topic_is_fine():
    report = "No rates data was fetched, so the Treasury view is qualitative. Unemployment is 4.3% [S1]."
    figs = v.check_numeric_grounding(report, "q", [UNRATE], SOURCES[:1])
    nd = v.check_no_data(report, ["macro", "rates"], [UNRATE], figs)
    assert nd["issue"] is False and nd["topics_without_data"][0]["disclosed"] is True


# --- end to end ------------------------------------------------------------------

def test_validate_report_summary_and_annotation():
    report = "- Unemployment is 4.3% [S1].\n- It will reach 5.7% [S1].\n"
    res = v.validate_report(report, "q", TABLES, SOURCES, ["macro"], today=date(2026, 10, 17))
    assert res["counts"] == {"figures": 2, "verified": 1, "derived": 0, "unverified": 1}
    assert res["summary"].startswith("1 of 2 figures verified against source data (1 exact, 0 derived); 1 flagged.")
    assert v.annotate_report(report, res).startswith("> **Validation:** 1 of 2 figures")


def test_key_data_block_cites_sources_and_shows_changes():
    block = key_data_block([UNRATE, HY, IG], SOURCES)
    assert "[S1] FRED: UNRATE" in block
    assert "latest 4.3 (2026-09-01)" in block
    assert "BAMLH0A0HYM2 minus BAMLC0A0CM: +2.45" in block
    assert any(f.kind == "derived" for f in all_facts([UNRATE]))


# --- formats seen in real gpt-oss output (Oct 2026 eval run) ------------------------

def test_fullwidth_citation_brackets():
    report = "Headline CPI rose 3.0% year over year 【S4】【S9】. Unemployment is 4.3%【S1†L3-L5】."
    figs = v.check_numeric_grounding(report, "q", TABLES, SOURCES)
    c = v.check_citations(report, SOURCES, figs)
    assert c["markers"] == 3
    assert c["dangling"] == ["S9"]
    assert c["uncited_paragraphs"] == []


def test_non_breaking_hyphens_and_spaces():
    figs = v.extract_figures("**U.S. Inflation – 12‑Month Trend** – a 0.5‑pp decline to 3.7 % on 18 Oct 2026")
    got = [(f.text, f.unit) for f in figs]
    assert ("12", "") not in got                      # "12‑Month" is a label
    assert ("0.5‑pp", "pp") in got               # unit after a non-breaking hyphen
    assert ("3.7 %", "%") in got
    assert ("18 Oct 2026", "date") in got and ("18", "") not in got


def test_year_over_year_uses_calendar_months_when_a_month_is_missing():
    # October 2025 was never published; 12 observations back would be August 2025 (13 months).
    dates = ["2025-08-01", "2025-09-01", "2025-11-01", "2025-12-01"] + [f"2026-{m:02d}-01" for m in range(1, 10)]
    values = [300.0, 303.0] + [305.0] * 10 + [312.09]
    cpi = fred_table("CPIAUCSL", values, dates, units="Index 1982-1984=100")
    labels = {f.label: f.value for f in all_facts([cpi]) if "% change" in f.label}
    assert labels["CPIAUCSL % change 2025-09-01 to 2026-09-01"] == pytest.approx(3.0)
    assert not any("2025-08-01 to 2026-09-01" in k for k in labels)
    assert statuses("CPI rose 3.0% year over year [S1].", tables=[cpi], sources=[source(1, "CPIAUCSL")])["3.0%"] == "derived"


def test_snippet_match_allows_missing_unit_and_is_summarised():
    sources = SOURCES + [source(5, url="https://news/x", snippet="Core CPI was 2.39% in September.")]
    res = v.validate_report("Core CPI is running at 2.39 [S5].", "q", TABLES, sources, ["macro"], today=date(2026, 10, 17))
    assert res["figures"][0]["in_source_text"] == "S5"
    assert "1 flagged (1 of them appear in a fetched web/news snippet)" in res["summary"]


def test_ties_prefer_the_latest_observation():
    t = fred_table("BAMLC0A0CM", [0.84, 0.90, 0.84], ["2026-10-13", "2026-10-14", "2026-10-15"], frequency="daily", topics=("credit",))
    fig = v.check_numeric_grounding("IG OAS is 0.84% [S1].", "q", [t], [source(1, "BAMLC0A0CM")])[0]
    assert fig.matched == "BAMLC0A0CM on 2026-10-15"


def _metrics_table():
    return {"title": "JPM Key Financial Metrics", "kind": "snapshot", "topics": ["equity"], "source_url": "https://finnhub/metric",
            "last_date": None, "frequency": None,
            "data": pd.DataFrame({"field": ["P/E (TTM)", "EPS growth TTM YoY (%)", "ROE TTM (%)"], "value": [13.6868, 17.88, 17.91]})}


def test_field_named_in_sentence_wins_when_values_round_the_same():
    # Real case (Q13, Oct 2026): EPS growth 17.88 and ROE 17.91 both round to 17.9
    src = [{"id": "S1", "title": "m", "url": "https://finnhub/metric", "source": "Finnhub", "snippet": ""}]
    fig = v.check_numeric_grounding("Earnings growth of 17.9% YoY [S1].", "q", [_metrics_table()], src)[0]
    assert fig.matched == "JPM Key Financial Metrics: EPS growth TTM YoY (%)"


def test_multiplication_sign_is_a_multiple():
    figs = v.extract_figures("P/E of 13.69× and 15x")
    assert [(f.text, f.unit) for f in figs] == [("13.69×", "x"), ("15x", "x")]



# --- assumptions and feedback for the writer ------------------------------------------

def test_labelled_assumptions_are_counted_and_not_no_data_issues():
    report = "- Hold 10% (assumption) in Treasuries.\n- Rates could fall to 2.1% next year.\n"
    res = v.validate_report(report, "q", [UNRATE], SOURCES[:1], ["macro", "rates"], today=date(2026, 10, 17))
    figs = {f["text"]: f for f in res["figures"]}
    assert figs["10%"]["assumption"] is True and figs["2.1%"]["assumption"] is False
    rates = res["no_data"]["topics_without_data"][0]
    assert rates["unverified_figures"] == ["2.1%"]
    assert "1 labelled as assumptions" in res["summary"]


def test_feedback_lists_fixable_items_with_the_right_source():
    sources = SOURCES + [source(5, url="https://news/x", snippet="Core CPI was 2.39% in September.")]
    report = ("- Unemployment is 4.3%.\n"
              "- Core CPI is 2.39%.\n"
              "- Spreads will hit 5.5% [S9].\n")
    res = v.validate_report(report, "q", TABLES, sources, ["macro", "rates"], today=date(2026, 10, 17))
    fb = v.feedback_for_writer(res)
    assert 'Add a citation to: "- Unemployment is 4.3%."' in fb and "come from [S1]" in fb
    assert '"2.39%"' in fb and "appears in source [S5]" in fb
    assert '"5.5%"' in fb and 'mark it "(assumption)"' in fb
    assert "Citations S9 do not exist; the only sources are S1, S2, S3, S4, S5." in fb
    assert "No data was fetched for rates; add one sentence saying so." in fb
    assert res["open_issues"] == 5  # 2.39% + 5.5% + uncited paragraph + dangling S9 + undisclosed rates


def test_cited_snippet_figure_is_not_an_open_issue():
    sources = SOURCES + [source(5, url="https://news/x", snippet="Core CPI was 2.39% in September.")]
    res = v.validate_report("- Core CPI is 2.39% [S5].\n", "q", TABLES, sources, ["macro"], today=date(2026, 10, 17))
    assert res["counts"]["unverified"] == 1 and res["open_issues"] == 0
    assert v.feedback_for_writer(res) == ""


def test_iso_dates_with_non_breaking_hyphens():
    # Real case (Q11, Oct 2026): "3.23 % (2026‑10‑15)" written with U+2011
    report = "HY OAS is 3.3% (2026\u201110\u201115) [S2]."
    figs = v.check_numeric_grounding(report, "q", TABLES, SOURCES)
    assert [(f.text, f.unit, f.status) for f in figs] == [("3.3%", "%", "verified"), ("2026\u201110\u201115", "date", "verified")]


def test_parenthesised_citations():
    # Real case (Oct 2026 UI run): "... tight relative to historical levels (S4, S8)"
    from answer_builder import parse_citation_markers
    assert [m["ids"] for m in parse_citation_markers("levels (S4, S8) and (S3) but not (see S2 above)")] == [["S4", "S8"], ["S3"]]
