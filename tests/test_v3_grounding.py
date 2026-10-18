"""v3: fact store, placeholder rendering, exact grounding, free numbers, direction check."""

from datetime import date

import pandas as pd
import pytest

import validators as v
from fact_store import build_fact_store, fact_table_block, fmt_change
from render import placeholder_ids, render


def fred(sid, values, dates, units="Percent", frequency="daily", topics=("credit",)):
    return {"title": f"FRED: {sid}", "data": pd.DataFrame({"date": pd.to_datetime(dates), "value": values}),
            "kind": "timeseries", "series_id": sid, "frequency": frequency, "units": units,
            "last_date": dates[-1], "topics": list(topics), "source_url": f"https://fred.stlouisfed.org/series/{sid}"}


DAYS = pd.bdate_range("2025-10-01", "2026-10-15").strftime("%Y-%m-%d").tolist()
HY = fred("BAMLH0A0HYM2", [round(3.0 + 0.001 * i, 2) for i in range(len(DAYS) - 1)] + [3.23], DAYS)
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
    assert "2 figure(s) inserted from the fact table, 1 typed directly." in res["summary"]
    fb = v.feedback_for_writer(res)
    assert '"0.84%"' in fb and "write it as the matching {{ID}}" in fb


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
