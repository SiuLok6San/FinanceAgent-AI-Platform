"""
fact_store.py
-------------
Named facts for the agents: every number the Researcher fetched or derived
gets a stable ID, a unit, a date, a source and a fixed display format.

    HY_OAS.latest        3.23%          ICE BofA US High Yield OAS, latest (2026-10-15)   [S1]
    HY_OAS.chg_1m        +12 bp         change since 2026-09-15                           [S1]
    HY_OAS.pctile_5y     8th percentile where the latest value sits in its 5y range       [S1]
    HY_OAS_minus_IG_OAS  +239 bp        spread between two series                         [S1, S2]
    JPM.pe_ttm           13.7x          P/E (TTM) from Finnhub                            [S4]

The Analyst, Reasoner and Writer see this table and write {{ID}} instead of
numbers; render.py inserts the value and its citation. So every number in a
report comes from the data by construction, and the validator only has to
check the words around it (e.g. "rose" next to a negative change).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from facts import lagged, series_frame, table_frequency
from settings import load_config

# lag (in observations or periods) -> suffix used in fact IDs
LAG_NAMES = {
    "daily": {1: "1d", 5: "1w", 21: "1m", 63: "3m", 126: "6m", 252: "1y"},
    "weekly": {1: "1w", 4: "1m", 13: "3m", 26: "6m", 52: "1y"},
    "monthly": {1: "1m", 3: "3m", 6: "6m", 12: "1y"},
    "quarterly": {1: "1q", 2: "2q", 4: "1y"},
}

# kinds whose sign means "went up / went down" (used by the direction check)
CHANGE_KINDS = {"change", "pct_change"}


@dataclass
class FactEntry:
    id: str
    value: Any                     # float, or str for text / date facts
    kind: str                      # level | change | pct_change | stat | percentile | spread | date | text
    unit: str                      # percent | bp | pp | pct | pts | index | usd | bn_usd | x | ratio | count | date | text
    display: str                   # exactly what is written into the report
    label: str                     # what the number is
    date: Optional[str] = None
    source_urls: Tuple[str, ...] = ()
    series_id: Optional[str] = None
    topics: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_change(self) -> bool:
        return self.kind in CHANGE_KINDS


# ---------------------------------------------------------------------------
# Display formats
# ---------------------------------------------------------------------------

def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _decimals(values: pd.Series) -> int:
    """Decimals the series is published with (e.g. 4.3 -> 1, 3.23 -> 2), at most 3."""
    best = 0
    for v in values.tail(30):
        text = f"{round(float(v), 4):.4f}".rstrip("0").rstrip(".")
        if "." in text:
            best = max(best, len(text.split(".")[1]))
    return min(best, 3)


def _money(v: float) -> str:
    a = abs(v)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= size:
            return f"${v / size:,.2f}{suffix}"
    return f"${v:,.2f}"


def fmt_level(v: float, level_format: str, dec: int) -> str:
    if level_format == "percent":
        return f"{v:.{dec}f}%"
    if level_format == "usd":
        return f"${v:,.2f}"
    if level_format == "bn_usd":
        return f"${v / 1000:,.2f} trillion"
    return f"{v:,.{dec}f}"


def fmt_change(v: float, unit: str, dec: int) -> str:
    sign = "+" if v > 0 else "−" if v < 0 else ""
    a = abs(v)
    if unit == "bp":
        return f"{sign}{a * 100:.0f} bp"
    if unit == "pp":
        return f"{sign}{a:.{max(dec, 1)}f} pp"
    if unit == "pts":
        return f"{sign}{a:,.2f} pts"
    return f"{sign}{a:.1f}%"  # pct


def fmt_snapshot(v: Any, unit: str) -> str:
    if unit in ("text", "date") or not isinstance(v, (int, float)):
        return str(v)
    if unit == "usd":
        return _money(v)
    if unit == "pct":
        return f"{v:.2f}%"
    if unit == "x":
        return f"{v:.1f}x"
    if unit == "ratio":
        return f"{v:.2f}"
    if unit == "count":
        return f"{v / 1e9:.2f}B" if abs(v) >= 1e9 else f"{v / 1e6:.2f}M" if abs(v) >= 1e6 else f"{v:,.0f}"
    return f"{v:g}"


def _date_label(day: pd.Timestamp, frequency: Optional[str]) -> str:
    if frequency == "monthly":
        return day.strftime("%B %Y")
    if frequency == "quarterly":
        return f"{day.year} Q{(day.month - 1) // 3 + 1}"
    return day.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Building the store
# ---------------------------------------------------------------------------

def _series_meta(table: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config("data_sources")
    by_id = {s["id"]: s for s in cfg["fred"]["series"]}
    meta = dict(by_id.get(table.get("series_id"), {}))
    pct_units = table.get("units") in load_config("validation")["percent_units"]
    meta.setdefault("alias", table.get("series_id") or table["title"])
    meta.setdefault("change_unit", "pp" if pct_units else "pct")
    meta.setdefault("level_format", "percent" if pct_units else "index")
    meta.setdefault("title", table["title"])
    return meta


def _series_facts(t: Dict[str, Any]) -> List[FactEntry]:
    s = series_frame(t)
    if s.empty:
        return []
    meta = _series_meta(t)
    alias, cu, lf = meta["alias"], meta["change_unit"], meta["level_format"]
    freq = table_frequency(t)
    dec = _decimals(s["value"])
    urls = (t.get("source_url", ""),)
    topics = tuple(t.get("topics") or ())
    title = meta["title"]
    v, day = float(s["value"].iloc[-1]), s["date"].iloc[-1]
    when = _date_label(day, freq)

    def entry(suffix, value, kind, unit, display, label, date=None):
        return FactEntry(f"{alias}.{suffix}", value, kind, unit, display, label, date, urls, t.get("series_id"), topics)

    out = [
        entry("latest", v, "level", lf, fmt_level(v, lf, dec), f"{title}, latest ({when})", day.strftime("%Y-%m-%d")),
        entry("latest_date", day.strftime("%Y-%m-%d"), "date", "date", when, f"{title}: date of the latest value"),
    ]
    for lag, name in LAG_NAMES.get(freq, {}).items():
        prev = lagged(s, freq, lag)
        if prev is None:
            continue
        pv, pday = float(prev[0]), prev[1]
        since = _date_label(pday, freq)
        on = "in" if freq in ("monthly", "quarterly") else "on"
        out.append(entry(f"prev_{name}", pv, "level", lf, fmt_level(pv, lf, dec), f"{title} {on} {since}",
                         pday.strftime("%Y-%m-%d")))
        if cu in ("bp", "pp", "pts"):
            out.append(entry(f"chg_{name}", v - pv, "change", cu, fmt_change(v - pv, cu, dec),
                             f"{title}: change since {since}", day.strftime("%Y-%m-%d")))
        if cu in ("pct", "pts") and pv:
            pct = (v / pv - 1) * 100
            out.append(entry(f"pct_{name}", pct, "pct_change", "pct", fmt_change(pct, "pct", dec),
                             f"{title}: % change since {since}", day.strftime("%Y-%m-%d")))
        if freq == "quarterly" and lag == 1 and pv > 0 and v > 0:
            ann = ((v / pv) ** 4 - 1) * 100
            out.append(entry("ann_growth_1q", ann, "pct_change", "pct", fmt_change(ann, "pct", dec),
                             f"{title}: annualized growth over the latest quarter", day.strftime("%Y-%m-%d")))

    # where the latest value sits in the fetched history
    window = load_config("data_sources")["fred"]["history_label"].get(freq, f"{len(s)}obs")
    lo, hi = s["value"].idxmin(), s["value"].idxmax()
    pctile = round(float((s["value"] <= v).mean() * 100))
    out += [
        entry(f"min_{window}", float(s["value"][lo]), "stat", lf, fmt_level(float(s["value"][lo]), lf, dec),
              f"{title}: lowest value in the last {window} ({_date_label(s['date'][lo], freq)})",
              s["date"][lo].strftime("%Y-%m-%d")),
        entry(f"max_{window}", float(s["value"][hi]), "stat", lf, fmt_level(float(s["value"][hi]), lf, dec),
              f"{title}: highest value in the last {window} ({_date_label(s['date'][hi], freq)})",
              s["date"][hi].strftime("%Y-%m-%d")),
        entry(f"avg_{window}", float(s["value"].mean()), "stat", lf, fmt_level(float(s["value"].mean()), lf, dec),
              f"{title}: average over the last {window}"),
        entry(f"pctile_{window}", pctile, "percentile", "percentile", f"{_ordinal(pctile)} percentile",
              f"{title}: where the latest value sits in its {window} range (0 = lowest, 100 = highest)"),
    ]
    return out


def _spread_facts(tables: List[Dict[str, Any]]) -> List[FactEntry]:
    """Latest value of one percent series minus another (e.g. HY OAS minus IG OAS)."""
    pcts = []
    for t in tables:
        if t.get("kind") != "timeseries" or t["data"].empty:
            continue
        meta = _series_meta(t)
        if meta["level_format"] == "percent":
            s = series_frame(t)
            pcts.append((t, meta, float(s["value"].iloc[-1]), _decimals(s["value"])))
    out = []
    for (ta, ma, va, da), (tb, mb, vb, db) in combinations(pcts, 2):
        unit = "bp" if ma["change_unit"] == "bp" and mb["change_unit"] == "bp" else "pp"
        diff = va - vb
        out.append(FactEntry(
            f"{ma['alias']}_minus_{mb['alias']}", diff, "spread", unit, fmt_change(diff, unit, max(da, db)),
            f"{ma['title']} minus {mb['title']} (latest values)", None,
            (ta.get("source_url", ""), tb.get("source_url", "")), None,
            tuple(sorted(set(ta.get("topics") or ()) | set(tb.get("topics") or ()))),
        ))
    return out


def _snapshot_facts(t: Dict[str, Any]) -> List[FactEntry]:
    df, ticker = t["data"], t.get("ticker")
    if not ticker or df.empty:
        return []
    urls = (t.get("source_url", ""),)
    topics = tuple(t.get("topics") or ())
    out = []
    if {"field", "value", "unit", "id"} <= set(df.columns):
        for row in df.itertuples():
            kind = "text" if row.unit in ("text", "date") else "level"
            out.append(FactEntry(f"{ticker}.{row.id}", row.value, kind, row.unit, fmt_snapshot(row.value, row.unit),
                                 f"{ticker} {row.field}", t.get("last_date"), urls, None, topics))
        if t["title"].endswith("Quote") and t.get("last_date"):
            out.append(FactEntry(f"{ticker}.quote_date", t["last_date"], "date", "date", t["last_date"],
                                 f"{ticker} quote date", None, urls, None, topics))
    elif "period" in df.columns:  # recommendation trends: latest period only
        ids = load_config("data_sources")["finnhub"]["recommendation_ids"]
        row = df.iloc[0]
        for col, fid in ids.items():
            if col in df.columns:
                out.append(FactEntry(f"{ticker}.{fid}", int(row[col]), "level", "count", f"{int(row[col])}",
                                     f"{ticker} analysts rating {col} ({row['period']})", str(row["period"]), urls,
                                     None, topics))
        out.append(FactEntry(f"{ticker}.rec_period", str(row["period"]), "date", "date", str(row["period"]),
                             f"{ticker} recommendation trends: latest period", None, urls, None, topics))
    return out


def build_fact_store(tables: List[Dict[str, Any]]) -> Dict[str, FactEntry]:
    entries: List[FactEntry] = []
    for t in tables:
        if t.get("kind") == "timeseries":
            entries += _series_facts(t)
        else:
            entries += _snapshot_facts(t)
    entries += _spread_facts(tables)
    return {e.id: e for e in entries}


# ---------------------------------------------------------------------------
# Prompt block
# ---------------------------------------------------------------------------

def fact_table_block(store: Dict[str, FactEntry], sources: List[Dict[str, Any]]) -> str:
    """The FACT TABLE the LLM agents see: ID, current value and meaning, grouped by source."""
    if not store:
        return "FACT TABLE: (no data was fetched)"
    ids = {s["url"]: s["id"] for s in sources if s.get("url")}
    lines = ["FACT TABLE — write every number as {{ID}}; the value and the [S#] citation are inserted for you."]
    groups: Dict[Tuple[str, ...], List[FactEntry]] = {}
    for e in store.values():
        groups.setdefault(e.source_urls, []).append(e)
    for urls, entries in groups.items():
        cite = ", ".join(ids[u] for u in urls if u in ids) or "no source id"
        lines.append(f"[{cite}]")
        for e in entries:
            lines.append(f"  {{{{{e.id}}}}} = {e.display} — {e.label}")
    return "\n".join(lines)
