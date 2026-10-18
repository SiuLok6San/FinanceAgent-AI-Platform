"""
facts.py
--------
Turns the Researcher's data tables into a flat list of numeric facts.

- observed: every number in a fetched table (a FRED observation, a Finnhub metric)
- derived:  simple arithmetic on fetched data (change vs. an earlier
            observation, % change, annualized quarterly growth, spread
            between the latest values of two series)

The same facts feed two things: the KEY DATA block that the Analyst,
Reasoner and Writer see (so the LLM works from real numbers), and the
numeric-grounding check in validators.py (so every number in the report can
be traced back to the data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from settings import load_config


@dataclass
class Fact:
    value: float
    kind: str                       # "observed" | "derived"
    label: str                      # human-readable description
    percent: bool                   # value is in percent / percentage points
    source_url: str = ""
    series_id: Optional[str] = None
    date: Optional[str] = None
    topics: Tuple[str, ...] = field(default_factory=tuple)


def _cfg() -> Dict[str, Any]:
    return load_config("validation")


def is_percent_units(units: Optional[str]) -> bool:
    return bool(units) and units in _cfg()["percent_units"]


def _is_percent_label(label: str) -> bool:
    low = label.lower()
    return "(%)" in low or "percent" in low or low.strip() == "dp"


def infer_frequency(dates: pd.Series) -> Optional[str]:
    """Guess daily/weekly/monthly/quarterly from the median gap between observations."""
    if len(dates) < 3:
        return None
    gap = pd.Series(sorted(pd.to_datetime(dates))).diff().dt.days.median()
    if gap <= 4:
        return "daily"
    if gap <= 10:
        return "weekly"
    if gap <= 45:
        return "monthly"
    if gap <= 120:
        return "quarterly"
    return None


def series_frame(table: Dict[str, Any]) -> pd.DataFrame:
    """date/value frame for a time-series table, oldest first."""
    df = table["data"]
    out = pd.DataFrame({"date": pd.to_datetime(df["date"]), "value": pd.to_numeric(df["value"], errors="coerce")})
    return out.dropna().sort_values("date").reset_index(drop=True)


def table_frequency(table: Dict[str, Any]) -> Optional[str]:
    if table.get("frequency"):
        return table["frequency"]
    if table.get("kind") == "timeseries":
        return infer_frequency(table["data"]["date"])
    return None


def _timeseries(tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [t for t in tables if t.get("kind") == "timeseries" and not t["data"].empty]


def _numeric(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def observed_facts(tables: List[Dict[str, Any]]) -> List[Fact]:
    facts: List[Fact] = []
    for t in tables:
        topics = tuple(t.get("topics") or ())
        url = t.get("source_url", "")
        df = t["data"]
        if t.get("kind") == "timeseries":
            pct = is_percent_units(t.get("units"))
            sid = t.get("series_id") or t["title"]
            for row in series_frame(t).itertuples():
                day = row.date.strftime("%Y-%m-%d")
                facts.append(Fact(row.value, "observed", f"{sid} on {day}", pct, url, t.get("series_id"), day, topics))
        elif {"field", "value"} <= set(df.columns):
            for row in df.itertuples():
                v = _numeric(row.value)
                if v is not None:
                    facts.append(Fact(v, "observed", f"{t['title']}: {row.field}", _is_percent_label(str(row.field)), url, None, t.get("last_date"), topics))
        else:  # wide snapshot table, e.g. recommendation trends
            key_col = next((c for c in ("period", "date") if c in df.columns), None)
            for _, row in df.iterrows():
                for col in df.columns:
                    v = _numeric(row[col])
                    if v is None or col == key_col:
                        continue
                    where = f" ({row[key_col]})" if key_col else ""
                    facts.append(Fact(v, "observed", f"{t['title']}: {col}{where}", _is_percent_label(str(col)), url, None, t.get("last_date"), topics))
    return facts


def derived_facts(tables: List[Dict[str, Any]]) -> List[Fact]:
    lags_cfg = _cfg()["derived"]["lags"]
    facts: List[Fact] = []
    latest: List[Tuple[Dict[str, Any], float, str]] = []

    for t in _timeseries(tables):
        s = series_frame(t)
        if s.empty:
            continue
        sid = t.get("series_id") or t["title"]
        pct = is_percent_units(t.get("units"))
        topics = tuple(t.get("topics") or ())
        url = t.get("source_url", "")
        last_v, last_d = s["value"].iloc[-1], s["date"].iloc[-1].strftime("%Y-%m-%d")
        latest.append((t, last_v, last_d))

        freq = table_frequency(t)
        for lag in lags_cfg.get(freq, []):
            if len(s) <= lag:
                break
            prev_v, prev_d = s["value"].iloc[-1 - lag], s["date"].iloc[-1 - lag].strftime("%Y-%m-%d")
            facts.append(Fact(last_v - prev_v, "derived", f"{sid} change {prev_d} to {last_d}", pct, url, t.get("series_id"), last_d, topics))
            if prev_v:
                facts.append(Fact((last_v / prev_v - 1) * 100, "derived", f"{sid} % change {prev_d} to {last_d}", True, url, t.get("series_id"), last_d, topics))
            if freq == "quarterly" and lag == 1 and prev_v > 0 and last_v > 0:
                facts.append(Fact(((last_v / prev_v) ** 4 - 1) * 100, "derived", f"{sid} annualized growth {prev_d} to {last_d}", True, url, t.get("series_id"), last_d, topics))

    # spreads between the latest values of two percent series (e.g. 10Y - 2Y, HY - IG)
    pct_latest = [x for x in latest if is_percent_units(x[0].get("units"))]
    for (a, va, da), (b, vb, db) in combinations(pct_latest, 2):
        ida, idb = a.get("series_id") or a["title"], b.get("series_id") or b["title"]
        topics = tuple(sorted(set(a.get("topics") or ()) | set(b.get("topics") or ())))
        facts.append(Fact(va - vb, "derived", f"{ida} minus {idb} (latest: {da}, {db})", True, a.get("source_url", ""), None, max(da, db), topics))
    return facts


def all_facts(tables: List[Dict[str, Any]]) -> List[Fact]:
    return observed_facts(tables) + derived_facts(tables)


def observation_dates(tables: List[Dict[str, Any]]) -> Set[str]:
    """Every ISO date that appears as an observation or as-of date in the tables."""
    dates: Set[str] = set()
    for t in tables:
        if t.get("last_date"):
            dates.add(str(t["last_date"])[:10])
        df = t["data"]
        for col in ("date", "period"):
            if col in df.columns:
                dates.update(pd.to_datetime(df[col], errors="coerce").dropna().dt.strftime("%Y-%m-%d"))
    return dates


# ---------------------------------------------------------------------------
# KEY DATA block for the LLM agents
# ---------------------------------------------------------------------------

def source_ids_by_url(sources: List[Dict[str, Any]]) -> Dict[str, str]:
    return {s["url"]: s["id"] for s in sources if s.get("url")}


def fmt_number(v: float, signed: bool = False) -> str:
    sign = "+" if signed and v > 0 else ""
    a = abs(v)
    for size, word in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if a >= size:
            return f"{sign}{v / size:.2f} {word}"
    if a >= 1000:
        return f"{sign}{v:,.1f}"
    return f"{sign}{v:.4g}"


def _cite(url: str, ids: Dict[str, str]) -> str:
    return f"[{ids[url]}] " if url in ids else ""


def key_data_block(tables: List[Dict[str, Any]], sources: List[Dict[str, Any]]) -> str:
    """Compact, citable summary of the fetched data for the LLM prompts."""
    if not tables:
        return "KEY DATA: (none fetched)"
    ids = source_ids_by_url(sources)
    lines = ["KEY DATA (fetched by the Researcher; quote these numbers and cite the [S#] shown):"]

    for t in tables:
        df = t["data"]
        cite = _cite(t.get("source_url", ""), ids)
        if t.get("kind") == "timeseries":
            s = series_frame(t)
            if s.empty:
                continue
            freq = table_frequency(t) or "unknown frequency"
            pct = is_percent_units(t.get("units"))
            lines.append(f"{cite}{t['title']} — units: {t.get('units') or 'n/a'}, {freq}")
            last_v, last_d = s["value"].iloc[-1], s["date"].iloc[-1].strftime("%Y-%m-%d")
            parts = [f"latest {fmt_number(last_v)} ({last_d})"]
            for lag in _cfg()["derived"]["lags"].get(freq, [])[:6]:
                if len(s) <= lag:
                    break
                pv, pd_ = s["value"].iloc[-1 - lag], s["date"].iloc[-1 - lag].strftime("%Y-%m-%d")
                change = f"change {fmt_number(last_v - pv, signed=True)}" + ("" if pct else f", {fmt_number((last_v / pv - 1) * 100, signed=True)}%" if pv else "")
                parts.append(f"{pd_}: {fmt_number(pv)} ({change})")
            lines.append("  " + "; ".join(parts))
        elif {"field", "value"} <= set(df.columns):
            items = [f"{r.field}: {fmt_number(v) if (v := _numeric(r.value)) is not None else r.value}" for r in df.itertuples()]
            lines.append(f"{cite}{t['title']}: " + "; ".join(items))
        elif "headline" in df.columns:
            heads = [f"{_cite(r.url, ids)}{r.headline} ({r.date})" for r in df.itertuples()]
            lines.append(f"{t['title']}: " + " | ".join(heads))
        else:
            rows = []
            for _, r in df.head(2).iterrows():
                rows.append(", ".join(f"{c} {fmt_number(v) if (v := _numeric(r[c])) is not None else r[c]}" for c in df.columns))
            lines.append(f"{cite}{t['title']}: " + " | ".join(rows))

    spreads = [f for f in derived_facts(tables) if " minus " in f.label]
    if spreads:
        lines.append("Spreads between latest values (percentage points): " + "; ".join(
            f"{f.label.split(' (latest')[0]}: {fmt_number(f.value, signed=True)}" for f in spreads))
    return "\n".join(lines)


def topics_with_data(tables: List[Dict[str, Any]]) -> Set[str]:
    return {topic for t in tables if not t["data"].empty for topic in (t.get("topics") or ())}
