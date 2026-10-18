"""v3: fact store, placeholder rendering, exact grounding, free numbers, direction check."""

from datetime import date

import pandas as pd
import pytest

import validators as v
from fact_store import build_fact_store, fact_table_block, fmt_change, history_window
from render import placeholder_ids, render


def fred(sid, values, dates, units="Percent", frequency="daily", topics=("credit",)):
    return {"title": f"FRED: {sid}", "data": pd.DataFrame({"date": pd.to_datetime(dates), "value": values}),
            "kind": "timeseries", "series_id": sid, "frequency": frequency, "units": units,
            "last_date": dates[-1], "topics": list(topics), "source_url": f"https://fred.stlouisfed.org/series/{sid}"}


DAYS = pd.bdate_range("2025-10-01", "2026-10-15").strftime("%Y-%m-%d").tolist()
# HY gets one older point so its history spans the full 5-year window; IG covers one year only
HY = fred("BAMLH0A0HYM2", [3.1] + [round(3.0 + 0.001 * i, 2) for i in range(len(DAYS) - 1)] + [3.23], ["2021-10-15"] + DAYS)
IG = fred("BAMLC0A0CM", [0.9] * (len(DAYS) - 1) + [0.84], DAYS)
MONTHS = [f"2025-{m:02d}-01" for m in range(9, 13)] + [f"2026-{m:02d}-01" for m in range(1, 10)]
CPI = fred("CPIAUCSL", [310.0] + [312.0] * 11 + [319.3], MONTHS, units="Index 1982-1984=100", frequency="monthly", topics=("macro",))
JPM = {"title": "JPM Key Financial Metrics", "kind": "snapshot", "ticker": "JPM", "topics": ["equity"],
       "source_url": "https://finnhub.io/docs/api/company-basic-financials", "last_date": None, "frequency": None,
       "data": pd.DataFrame({"field": ["P/E (TTM)", "Dividend yield, indicated annual (%)"], "value": [13.6868, 2.73544],
                             "unit": ["x", "pct"], "id": ["pe_ttm", "div_yield"]})}
TABLES = [HY, IG, CPI, JPM]
SOURCES = [{"id": f"S{i}", "url": t["source_url"], "title": t["title"], "source": "x", "snippet": ""}
           for i, t in enumerate(TABLES, start=1)]
STORE = build_fact_store(TABLES)


def test_fact_ids_values_and_displays():
    assert STORE["HY_OAS.latest"].display == "3.23%"
    assert STORE["IG_OAS.latest"].display == "0.84%"
    assert STORE["CPI.pct_1y"].display == "+3.0%"            # 319.3 / 310 - 1, calendar 12 months
    assert STORE["HY_OAS_minus_IG_OAS"].display == "+239 bp"
    assert STORE["JPM.pe_ttm"].display == "13.7x" and STORE["JPM.div_yield"].display == "2.74%"
    expected = round(float((HY["data"]["value"] <= 3.23).mean() * 100))
    assert STORE["HY_OAS.pctile_5y"].value == expected and STORE["HY_OAS.pctile_5y"].display.endswith("th percentile")
    assert STORE["CPI.latest_date"].display == "September 2026"
    assert fmt_change(-0.12, "bp", 2) == "−12 bp" and fmt_change(0.2, "pp", 1) == "+0.2 pp"


def test_fact_table_lists_ids_by_source():
    block = fact_table_block(STORE, SOURCES)
    assert "{{HY_OAS.latest}} = 3.23%" in block and "[S1]" in block and "[S1, S2]" in block


def test_render_inserts_values_cites_once_per_line_and_flags_unknowns():
    tpl = ("- HY OAS is {{HY_OAS.latest}}, {{HY_OAS.latest}} again; IG is {{ IG_OAS.latest }}.\n"
           "- Weight {{assume: 10-15%}}; {{NOPE.x}}; P/E {JPM.pe_ttm}; keep {not a fact}.")
    text, refs = render(tpl, STORE, SOURCES)
    assert text.splitlines()[0] == "- HY OAS is 3.23% [S1], 3.23% again; IG is 0.84% [S2]."
    assert text.splitlines()[1] == "- Weight 10-15% (assumption); [unknown fact: NOPE.x]; P/E 13.7x [S4]; keep {not a fact}."
    assert [r.kind for r in refs] == ["fact", "fact", "fact", "assumption", "unknown", "fact"]
    assert placeholder_ids(tpl) == ["HY_OAS.latest", "HY_OAS.latest", "IG_OAS.latest", "NOPE.x", "JPM.pe_ttm"]


def _validate(tpl):
    text, refs = render(tpl, STORE, SOURCES)
    return v.validate_report(text, "q", TABLES, SOURCES, ["credit"], today=date(2026, 10, 17), refs=refs)


def test_placeholder_figures_are_grounded_exactly_and_typed_numbers_are_issues():
    res = _validate("- HY OAS is {{HY_OAS.latest}}, up {{HY_OAS.chg_1y}} in a year.\n- IG OAS is 0.84%.\n")
    figs = {f["text"]: f for f in res["figures"]}
    assert figs["3.23%"]["origin"] == "fact" and figs["3.23%"]["status"] == "verified"
    assert figs["3.23%"]["fact_id"] == "HY_OAS.latest"
    assert figs["0.84%"]["origin"] == "free" and figs["0.84%"]["status"] == "verified"
    # typed directly (even though correct) and uncited -> 2 open issues in placeholder mode
    assert res["open_issues"] == 2
    assert "2 figure(s) inserted from the fact table, 0 quoted from web sources, 1 typed directly." in res["summary"]
    fb = v.feedback_for_writer(res)
    assert '"0.84%"' in fb and "so write the matching {{ID}}" in fb


def test_assumption_label_on_a_number_from_a_cited_source_is_an_issue():
    news = {"id": "S5", "url": "https://example.com/nyfed", "title": "NY Fed", "source": "web",
            "snippet": "The New York Fed model puts the chance of a recession at 25.2%."}
    tpl = ("- The NY Fed model gives {{assume: 25.2%}} [S5].\n"      # S5's figure, labelled an assumption
           "- A cash buffer of {{assume: 10-15%}} is prudent [S5].\n"  # a real assumption
           "- The NY Fed model gives 25.2%.\n")                         # typed, uncited, in S5's text
    text, refs = render(tpl, STORE, SOURCES + [news])
    res = v.validate_report(text, "q", TABLES, SOURCES + [news], ["credit"], today=date(2026, 10, 17), refs=refs)
    assumed = [f for f in res["figures"] if f["origin"] == "assumption"]
    assert [v.mislabelled_assumption(f) for f in assumed] == ["S5", None, None]
    assert res["open_issues"] == 2  # the mislabelled assumption and the typed number
    assert "1 of those labels on a number from a source the line cites" in res["summary"]
    fb = v.feedback_for_writer(res)
    assert "labelled as an assumption, but it is in the text of [S5]" in fb and "{{S5: 25.2%}}" in fb
    assert "it appears in the text of [S5], so quote it as {{S5: 25.2%}}" in fb


def test_direction_check_on_placeholders():
    ok = _validate("- HY spreads widened {{HY_OAS.chg_1y}} over the year.\n")
    assert ok["directions"] == []
    bad = _validate("- HY spreads narrowed {{HY_OAS.chg_1y}} over the year, but CPI fell {{CPI.pct_1y}}.\n")
    assert [(d["word"], d["says"], d["data"]) for d in bad["directions"]] == [("narrowed", "down", "up"), ("fell", "down", "up")]
    assert "2 change figure(s) described in the wrong direction." in bad["summary"]
    assert "went up; fix the wording" in v.feedback_for_writer(bad)


def test_direction_check_on_free_text_uses_the_matched_change():
    # v1/v2 style: a typed number that matches a change in the data
    res = v.validate_report("CPI fell 3.0% over the past year [S3].", "q", TABLES, SOURCES, ["macro"],
                            today=date(2026, 10, 17))
    assert [(d["word"], d["data"]) for d in res["directions"]] == [("fell", "up")]


def test_wrong_written_sign_is_a_direction_error():
    res = v.validate_report("IG spreads moved -0.06 pp in a day [S2].", "q", TABLES, SOURCES, ["credit"],
                            today=date(2026, 10, 17))
    assert res["directions"] == [] or res["directions"][0]["data"] == "down"


def test_unknown_fact_is_an_open_issue():
    res = _validate("- Spread is {{HY_OAS.nope}}.\n")
    assert res["unknown_facts"] == ["HY_OAS.nope"] and res["open_issues"] >= 1
    assert "{{HY_OAS.nope}} is not in the FACT TABLE" in v.feedback_for_writer(res)


def test_web_quotes_must_appear_in_the_named_source():
    sources = SOURCES + [{"id": "S5", "url": "https://news/x", "title": "n", "source": "web",
                          "snippet": "Core PCE inflation was 2.9% in August."}]
    text, refs = render("- Core PCE is {{S5: 2.9%}} and {{S5: 3.4%}}.\n", STORE, sources)
    assert text == "- Core PCE is 2.9% [S5] and 3.4%.\n"
    res = v.validate_report(text, "q", TABLES, sources, ["macro"], today=date(2026, 10, 17), refs=refs)
    figs = {f["text"]: f for f in res["figures"]}
    assert figs["2.9%"]["origin"] == "quote" and figs["2.9%"]["in_source_text"] == "S5"
    assert figs["3.4%"]["matched"] == "quoted from S5 (not found in its text)"
    assert res["open_issues"] == 1
    assert "quoted from a source whose text does not contain it" in v.feedback_for_writer(res)


def test_duplicate_units_and_signs_around_placeholders_are_dropped():
    text, refs = render("- IG is {{IG_OAS.latest}} %, HY moved +{{HY_OAS.chg_1y}} bp, at the "
                        "{{HY_OAS.pctile_5y}} percentile; P/E {{JPM.pe_ttm}}x; keep 5 bp.", STORE, SOURCES)
    assert text == (f"- IG is 0.84% [S2], HY moved {STORE['HY_OAS.chg_1y'].display} [S1], at the "
                    f"{STORE['HY_OAS.pctile_5y'].display}; P/E 13.7x [S4]; keep 5 bp.")
    assert [text[r.start:r.end] for r in refs][0] == "0.84%"


def test_spreads_are_larger_minus_smaller():
    assert "HY_OAS_minus_IG_OAS" in STORE and "IG_OAS_minus_HY_OAS" not in STORE


def test_writer_citation_right_after_a_placeholder_is_not_doubled():
    text, _ = render("- IG is {{IG_OAS.latest}} [S2]; spread {{HY_OAS_minus_IG_OAS}} [S1,S2].", STORE, SOURCES)
    assert text == "- IG is 0.84% [S2]; spread +239 bp [S1,S2]."


def test_spreads_only_within_the_same_group():
    ust = fred("DGS10", [4.0] * (len(DAYS) - 1) + [5.10], DAYS, topics=("rates",))
    ff = fred("FEDFUNDS", [3.75] * 13, MONTHS, frequency="monthly", topics=("rates",))
    store = build_fact_store([HY, IG, ust, ff])
    assert "HY_OAS_minus_IG_OAS" in store and "UST10Y_minus_FEDFUNDS" in store
    assert not any(k in store for k in ("UST10Y_minus_HY_OAS", "FEDFUNDS_minus_IG_OAS", "HY_OAS_minus_FEDFUNDS"))


def test_direction_check_stops_at_commas():
    res = _validate("- HY OAS moved {{HY_OAS.chg_1y}}, narrowing the gap to IG.\n")
    assert res["directions"] == []


def test_label_phrases_allow_any_whitespace():
    assert [f.text for f in v.extract_figures("Relative to the S&P\u00a0500 and the S&P  500 index")] == []


def test_comparison_check_units_and_order():
    good = _validate("- HY OAS {{HY_OAS.latest}} is above IG OAS {{IG_OAS.latest}}.\n")
    assert good["comparisons"] == []
    wrong_order = _validate("- IG OAS {{IG_OAS.latest}} is above HY OAS {{HY_OAS.latest}}.\n")
    assert [(c["kind"], c["word"]) for c in wrong_order["comparisons"]] == [("order", "above")]
    # real case from a v3 dev draft: a P/E multiple compared with an index level
    units = _validate("- JPM's P/E of {{JPM.pe_ttm}} is modest relative to CPI at {{CPI.latest}}.\n")
    assert [(c["kind"], c["a"], c["b"]) for c in units["comparisons"]] == [("units", "JPM.pe_ttm", "CPI.latest")]
    assert units["open_issues"] == 1
    assert "compares with \"relative to\"" in v.feedback_for_writer(units)
    assert "1 comparison(s) between facts that do not hold." in units["summary"]


def test_comparison_needs_facts_on_both_sides_in_one_sentence():
    res = _validate("- HY OAS is {{HY_OAS.latest}}. That is above average.\n- IG is {{IG_OAS.latest}}.\n")
    assert res["comparisons"] == []


def test_comparison_check_ignores_time_phrases_and_other_clauses():
    # real false positives from v3 dev drafts
    res = _validate("- IG widened {{IG_OAS.chg_1y}} over the past year and HY by {{HY_OAS.chg_1y}}.\n"
                    "- Cut risk if P/E exceeds {{JPM.pe_ttm}} or HY OAS rises above {{HY_OAS.latest}}.\n")
    assert res["comparisons"] == []


# --- wording: history position and current trend ---------------------------------------

def test_history_wording_is_checked_against_the_percentile():
    pct = STORE["HY_OAS.pctile_5y"].value            # 87th in this fixture
    assert pct > 67
    bad = _validate("- HY spreads at {{HY_OAS.latest}} are near their five-year lows.\n")
    assert [(w["says"], w["series"]) for w in bad["wording"]["history"]] == [("low", "HY_OAS")]
    assert "sits at the" in v.feedback_for_writer(bad) and bad["open_issues"] == 1
    ok = _validate("- High-yield spreads are elevated relative to their history.\n")
    assert ok["wording"]["history"] == []


def test_short_history_is_labelled_by_its_span_and_not_judged():
    assert "IG_OAS.pctile_1y" in STORE and not any(k.startswith("IG_OAS.pctile_5y") for k in STORE)
    assert "IG_OAS.min_1y" in STORE and history_window(pd.Series(pd.to_datetime(["2024-04-01", "2026-10-01"])), "monthly") == "30m"
    # IG sits at the bottom of its one-year range, but one year cannot settle "historically wide"
    res = _validate("- IG spreads at {{IG_OAS.latest}} are historically wide.\n")
    assert res["wording"]["history"] == []


def test_history_wording_skips_negations_and_scenarios():
    res = _validate("- HY spreads are not historically tight.\n"
                    "- If HY spreads return to five-year lows, cut risk.\n")
    assert res["wording"]["history"] == []


def test_trend_wording_without_a_number_is_checked():
    # the check that catches "tightening" in a dev draft where IG had widened
    assert STORE["IG_OAS.chg_1m"].value < 0                # fixture: IG fell to 0.84 on the last day
    up = _validate("- Investment-grade spreads are widening.\n")
    assert [(w["word"], w["series"]) for w in up["wording"]["trend"]] == [("widening", "IG_OAS")]
    tight = _validate("- Investment-grade spreads are tightening.\n")
    assert tight["wording"]["trend"] == []                  # tightening = down for spreads, matches the data
    year = _validate("- Over the past year high-yield spreads are rising.\n")
    assert year["wording"]["trend"] == []                   # the year-ago comparison is up


def test_trend_wording_defers_to_the_direction_check_when_a_change_is_quoted():
    res = _validate("- HY spreads are falling, {{HY_OAS.chg_1y}} this year.\n")
    assert res["wording"]["trend"] == []


def test_lines_are_labelled_data_or_interpretation():
    res = _validate("- HY OAS is {{HY_OAS.latest}} today.\n- Spreads reflect investor appetite for risk.\n# Heading\n")
    assert [l["kind"] for l in res["lines"]] == ["data", "interpretation"]
