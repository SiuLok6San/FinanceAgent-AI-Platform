"""Saved eval outputs must round-trip without losing data the validators need."""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from common import load_questions, table_from_json, table_to_json  # noqa: E402


def test_table_round_trip():
    t = {
        "title": "FRED: UNRATE", "kind": "timeseries", "series_id": "UNRATE", "frequency": "monthly",
        "units": "Percent", "last_date": "2026-09-01", "topics": ["macro"], "source_url": "u",
        "data": pd.DataFrame({"date": pd.to_datetime(["2026-08-01", "2026-09-01"]), "value": [4.2, 4.3]}),
    }
    back = table_from_json(table_to_json(t))
    assert back["data"]["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-08-01", "2026-09-01"]
    assert back["data"]["value"].tolist() == [4.2, 4.3]
    assert {k: back[k] for k in ("series_id", "units", "topics")} == {"series_id": "UNRATE", "units": "Percent", "topics": ["macro"]}


def test_questions_file_is_complete():
    qs = load_questions()
    assert len(qs) == 20
    assert {q["category"] for q in qs} == {"macro", "rates", "credit", "equity", "portfolio"}
    assert all(sum(q["category"] == c for q in qs) == 4 for c in {"macro", "rates", "credit", "equity", "portfolio"})
    assert all(q["expected_tags"][0] == q["category"] or q["category"] in q["expected_tags"] for q in qs)
