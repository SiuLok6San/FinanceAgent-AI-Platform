"""
validators.py
-------------
Deterministic validation layer, run on every final report after generation.

Checks (no LLM involved):
1. Numeric grounding  - every number in the report is classified as
   "verified" (matches a fetched value, allowing for rounding and unit
   conversion), "derived" (simple arithmetic on fetched values, e.g. a change
   or a spread) or "unverified".
2. Citation integrity - [S#] markers must point to a fetched source;
   paragraphs that state verified numbers must cite something; a cited
   source should be the one the number came from.
3. Data freshness     - last observation date of every series vs. what its
   frequency allows.
4. No-data honesty    - topics the question is about but for which no data
   was fetched must be disclosed, and must not carry unverified numbers.

Flagged content is never removed: the report gets a one-line summary at the
top and the UI highlights the flagged numbers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from answer_builder import find_dangling_citations, parse_citation_markers
from facts import Fact, all_facts, observation_dates, table_frequency, topics_with_data
from router import matches_any, topics_in
from settings import load_config


def _cfg() -> Dict[str, Any]:
    return load_config("validation")


# ---------------------------------------------------------------------------
# Number and date extraction
# ---------------------------------------------------------------------------

_MAGNITUDE = {
    "thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12, "t": 1e12,
}

_HYPHENS = "-\u2010\u2011\u2012"  # ASCII hyphen, hyphen, non-breaking hyphen, figure dash

_NUMBER_RE = re.compile(r"""
    (?<![\w.,/$])(?<![A-Za-z][-\u2010\u2011\u2012])   # not inside a word, decimal, ratio or code (COVID-19)
    (?P<sign>[-+−–])?
    (?P<dollar>US\$|\$)?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:
        \s?(?P<magw>trillion|billion|million|thousand|tn|bn|mn)\b
      | (?P<magl>[tbmk])(?![\w])
    )?
    (?:
        (?:\s|[-\u2010\u2011])?(?P<unit>%|percent(?:age)?\s+points?|percent|ppts?|pp|bps|bp|basis\s+points?|pts?|points?)(?![\w])
      | (?P<times>x)(?![\w])
    )?
    (?![\w/]|[.,]\d)
""", re.I | re.X)

_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           "|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_RE = re.compile(
    rf"\b(?:(?P<iso>(?:19|20)\d{{2}}-\d{{2}}-\d{{2}})"
    rf"|(?P<mon>{_MONTHS})\.?\s+(?:(?P<day>\d{{1,2}}),?\s+)?(?P<year>(?:19|20)\d{{2}})"
    rf"|(?P<day2>\d{{1,2}})\s+(?P<mon2>{_MONTHS})\.?,?\s+(?P<year2>(?:19|20)\d{{2}}))\b",
    re.I,
)
_MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_URL_RE = re.compile(r"https?://\S+|\]\([^)]*\)")


def _unit_kind(m: re.Match) -> str:
    if m.group("times"):
        return "x"
    u = (m.group("unit") or "").lower()
    if not u:
        return "$" if m.group("dollar") else ""
    if u in ("%", "percent"):
        return "%"
    if u.startswith("b"):
        return "bp"
    return "pp"  # percentage points, pp, points, pts


def _mask(text: str, spans: Iterable[Tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def _without_links(text: str) -> str:
    """Blank out citation markers and URLs (same length, so positions still line up)."""
    spans = [(m["start"], m["end"]) for m in parse_citation_markers(text)]
    spans += [m.span() for m in _URL_RE.finditer(text)]
    return _mask(text, spans)


def _without_labels(text: str) -> str:
    """Also blank out dates and label phrases such as "S&P 500"."""
    spans = [m.span() for m in _DATE_RE.finditer(text)]
    for phrase in _cfg()["extraction"]["label_phrases"]:
        spans += [m.span() for m in re.finditer(re.escape(phrase), text, re.I)]
    return _mask(text, spans)


def _is_label(text: str, m: re.Match, cfg: Dict[str, Any]) -> bool:
    start, end = m.span()
    after = text[end:end + 12]
    before = text[max(0, start - 15):start]
    has_unit = bool(m.group("unit") or m.group("times") or m.group("dollar") or m.group("magw") or m.group("magl"))

    # list numbering / section numbers: "1. Executive Summary", "## 2) Risks"
    line_start = text.rfind("\n", 0, start) + 1
    if re.fullmatch(r"[\s#>*\-]*", text[line_start:start]) and re.match(r"[.)](\s|$)", after) and not has_unit:
        return True
    # "Table 2", "Scenario 1"
    words = "|".join(map(re.escape, cfg["label_words"]))
    if re.search(rf"\b(?:{words})\.?\s*$", before, re.I):
        return True
    # compound labels: "10-year", "3-month", "10-K" (any hyphen character)
    if not has_unit and re.match(rf"[{_HYPHENS}][A-Za-z]", after):
        return True
    # time spans: "12 months", "6 weeks"
    units = "|".join(map(re.escape, cfg["time_units"]))
    if not has_unit and re.match(rf"\s?(?:{units})s?\b", after, re.I):
        return True
    # year labels: "in 2026"
    lo, hi = cfg["year_label_range"]
    num = m.group("num")
    if not has_unit and not m.group("sign") and re.fullmatch(r"\d{4}", num) and lo <= int(num) <= hi:
        return True
    return False


@dataclass
class Figure:
    text: str
    value: float          # absolute value as written, magnitude applied (e.g. "$1.2bn" -> 1.2e9)
    unit: str             # "%", "bp", "pp", "x", "$", "" or "date"
    tolerance: float      # rounding tolerance implied by the digits shown
    start: int
    end: int
    status: str = "unverified"     # verified | derived | unverified
    matched: Optional[str] = None  # description of the matching fact
    source_id: Optional[str] = None
    topic_match: Optional[bool] = None
    in_source_text: Optional[str] = None  # S# of a snippet that contains the same number
    context: str = ""


def extract_figures(text: str) -> List[Figure]:
    """Numbers (with units) and dates used as values in text, skipping labels."""
    cfg = _cfg()["extraction"]
    figures: List[Figure] = []
    unlinked = _without_links(text or "")
    for m in _DATE_RE.finditer(unlinked):
        figures.append(Figure(m.group(0), 0.0, "date", 0.0, m.start(), m.end()))
    masked = _without_labels(unlinked)
    for m in _NUMBER_RE.finditer(masked):
        if _is_label(masked, m, cfg):
            continue
        num = m.group("num").replace(",", "")
        decimals = len(num.split(".")[1]) if "." in num else 0
        mag = _MAGNITUDE.get((m.group("magw") or m.group("magl") or "").lower(), 1.0)
        figures.append(Figure(
            text=text[m.start():m.end()],
            value=abs(float(num)) * mag,
            unit=_unit_kind(m),
            tolerance=0.5 * 10 ** -decimals * mag + 1e-9,
            start=m.start(),
            end=m.end(),
        ))
    return sorted(figures, key=lambda f: f.start)


def _parse_date(text: str) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """(ISO date or None, (year, month))."""
    m = _DATE_RE.fullmatch(text.strip())
    if not m:
        return None, None
    if m.group("iso"):
        iso = m.group("iso")
        return iso, (int(iso[:4]), int(iso[5:7]))
    month = _MONTH_NUM[(m.group("mon") or m.group("mon2"))[:3].lower()]
    year = int(m.group("year") or m.group("year2"))
    day = m.group("day") or m.group("day2")
    if day:
        return f"{year:04d}-{month:02d}-{int(day):02d}", (year, month)
    return None, (year, month)


# ---------------------------------------------------------------------------
# 1) Numeric grounding
# ---------------------------------------------------------------------------

def _sentence_bounds(text: str, pos: int) -> Tuple[int, int]:
    starts = [m.end() for m in re.finditer(r"[.!?](?=\s)|\n", text[:pos])]
    a = starts[-1] if starts else 0
    m = re.search(r"[.!?](?=\s)|\n", text[pos:])
    b = pos + m.start() + 1 if m else len(text)
    return a, b


def _candidate_values(fig: Figure, fact: Fact) -> List[Tuple[float, float]]:
    """(report value, tolerance) pairs, converted into the fact's units, that may match it."""
    x, tol = fig.value, fig.tolerance
    if fig.unit == "%":
        if fact.percent:
            return [(x, tol)]
        return [(x / 100, tol / 100)] if abs(fact.value) < 1 else []   # 4.25% vs 0.0425
    if fig.unit == "bp":
        return [(x / 100, tol / 100)] if fact.percent else []          # 25 bp vs 0.25 pp
    if fig.unit in ("x", "$"):
        return [] if fact.percent else [(x, tol)]
    if fig.unit == "pp":
        return [(x, tol)]
    out = [(x, tol)]
    if fig.unit == "" and x < 1 and fact.percent:
        out.append((x * 100, tol * 100))                                # 0.0425 vs 4.25%
    return out


def _topic_match(sentence: str, fact: Fact, sentence_topics: Set[str], series_keywords: Dict[str, List[str]]) -> bool:
    if fact.series_id and matches_any(sentence, series_keywords.get(fact.series_id, [])):
        return True
    return bool(sentence_topics & set(fact.topics))


def check_numeric_grounding(
    report: str,
    question: str,
    tables: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
) -> List[Figure]:
    facts = all_facts(tables)
    url_to_id = {s["url"]: s["id"] for s in sources if s.get("url")}
    dates = observation_dates(tables)
    for s in sources:  # "2026-10-15: ..." prefixes on news / web snippets
        dates.update(re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", s.get("snippet") or ""))
    months = {(int(d[:4]), int(d[5:7])) for d in dates}
    question_values = {round(f.value, 9) for f in extract_figures(question) if f.unit != "date"}
    snippet_figures = [(s["id"], f) for s in sources for f in extract_figures(s.get("snippet") or "") if f.unit != "date"]
    series_keywords = {s["id"]: s["keywords"] for s in load_config("data_sources")["fred"]["series"]}

    figures = []
    for fig in extract_figures(report):
        a, b = _sentence_bounds(report, fig.start)
        sentence = report[a:b].strip()
        fig.context = sentence[:300]

        if fig.unit == "date":
            iso, ym = _parse_date(fig.text)
            if (iso and iso in dates) or (not iso and ym in months):
                fig.status, fig.matched = "verified", "observation / as-of date in fetched data"
            figures.append(fig)
            continue

        if round(fig.value, 9) in question_values:
            continue  # the number comes from the question itself

        sentence_topics = set(topics_in(sentence))
        best: Optional[Tuple[Tuple[int, int, float], Fact]] = None
        for fact in facts:
            for x, tol in _candidate_values(fig, fact):
                err = abs(x - abs(fact.value))
                if err <= tol:
                    topical = _topic_match(sentence, fact, sentence_topics, series_keywords)
                    # prefer facts about what the sentence discusses, then exact values, then closeness
                    rank = (0 if topical else 1, 0 if fact.kind == "observed" else 1, err / max(tol, 1e-12))
                    if best is None or rank < best[0]:
                        best = (rank, fact)
        if best:
            fact = best[1]
            fig.status = "verified" if fact.kind == "observed" else "derived"
            fig.matched = fact.label
            fig.source_id = url_to_id.get(fact.source_url)
            fig.topic_match = best[0][0] == 0
        else:
            for sid, sf in snippet_figures:
                same_unit = sf.unit == fig.unit or "" in (sf.unit, fig.unit)
                if same_unit and abs(sf.value - fig.value) <= max(sf.tolerance, fig.tolerance):
                    fig.in_source_text = sid
                    break
        figures.append(fig)
    return figures


# ---------------------------------------------------------------------------
# 2) Citation integrity
# ---------------------------------------------------------------------------

def _units(text: str) -> List[Tuple[int, int]]:
    """Spans of bullets / paragraphs (headings are skipped)."""
    spans, start, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        is_bullet = bool(re.match(r"([-*•]|\d+[.)])\s", stripped))
        if not stripped or stripped.startswith("#") or is_bullet:
            if start is not None:
                spans.append((start, pos))
                start = None
            if is_bullet:
                spans.append((pos, pos + len(line)))
        elif start is None:
            start = pos
        pos += len(line)
    if start is not None:
        spans.append((start, pos))
    return spans


def check_citations(report: str, sources: List[Dict[str, Any]], figures: List[Figure]) -> Dict[str, Any]:
    markers = parse_citation_markers(report)
    uncited, mismatched = [], []
    for a, b in _units(report):
        grounded = [f for f in figures if a <= f.start < b and f.status in ("verified", "derived") and f.unit != "date"]
        if not grounded:
            continue
        cited = {i for m in markers if a <= m["start"] < b for i in m["ids"]}
        snippet = report[a:b].strip()[:200]
        if not cited:
            uncited.append({"text": snippet, "figures": [f.text for f in grounded]})
            continue
        wrong = [f.text for f in grounded if f.source_id and f.source_id not in cited]
        if wrong:
            mismatched.append({"text": snippet, "figures": wrong, "cited": sorted(cited)})
    return {
        "markers": len(markers),
        "dangling": find_dangling_citations(report, sources),
        "uncited_paragraphs": uncited,
        "source_mismatches": mismatched,
    }


# ---------------------------------------------------------------------------
# 3) Data freshness
# ---------------------------------------------------------------------------

def _period_end(day: date, frequency: str) -> date:
    if frequency == "monthly":
        return (pd.Timestamp(day) + pd.offsets.MonthEnd(0)).date()
    if frequency == "quarterly":
        return (pd.Timestamp(day) + pd.offsets.QuarterEnd(0)).date()
    return day


def check_freshness(tables: List[Dict[str, Any]], today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    limits = _cfg()["freshness"]["max_age_days"]
    out = []
    for t in tables:
        last = t.get("last_date")
        freq = table_frequency(t)
        if not last:
            continue
        row = {"table": t["title"], "last_date": str(last)[:10], "frequency": freq, "age_days": None,
               "max_age_days": limits.get(freq), "status": "n/a"}
        if freq in limits:
            end = _period_end(date.fromisoformat(str(last)[:10]), freq)
            row["age_days"] = max((today - end).days, 0)
            row["status"] = "stale" if row["age_days"] > limits[freq] else "ok"
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 4) No-data honesty
# ---------------------------------------------------------------------------

def check_no_data(report: str, tags: List[str], tables: List[Dict[str, Any]], figures: List[Figure]) -> Dict[str, Any]:
    routing = load_config("routing")["tier1"]
    covered = topics_with_data(tables)
    missing = [t for t in tags if t not in covered]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", report) if s.strip()]
    patterns = [re.compile(p, re.I) for p in _cfg()["no_data"]["disclosure_patterns"]]

    topics = []
    for topic in missing:
        words = [topic] + routing.get(topic, {}).get("keywords", [])
        disclosed = any(any(p.search(s) for p in patterns) and matches_any(s, words) for s in sentences)
        unverified = [f.text for f in figures
                      if f.status == "unverified" and f.unit != "date" and topic in topics_in(f.context)]
        topics.append({"topic": topic, "disclosed": disclosed, "unverified_figures": unverified})
    issue = any(not t["disclosed"] or t["unverified_figures"] for t in topics)
    return {"topics_without_data": topics, "issue": issue}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_report(
    report: str,
    question: str,
    tables: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    tags: List[str],
    data_status: Optional[List[Dict[str, Any]]] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Run all checks; the result is JSON-serializable and stored with the report."""
    figures = check_numeric_grounding(report, question, tables, sources)
    counts = {k: sum(f.status == k for f in figures) for k in ("verified", "derived", "unverified")}
    result = {
        "counts": {"figures": len(figures), **counts},
        "figures": [asdict(f) for f in figures],
        "citations": check_citations(report, sources, figures),
        "freshness": check_freshness(tables, today),
        "no_data": check_no_data(report, tags, tables, figures),
        "data_status": [s for s in (data_status or []) if not s.get("ok")],
    }
    result["summary"] = summary_line(result)
    return result


def summary_line(v: Dict[str, Any]) -> str:
    c = v["counts"]
    if c["figures"]:
        in_snippets = sum(f["status"] == "unverified" and bool(f["in_source_text"]) for f in v["figures"])
        note = f" ({in_snippets} of them appear in a fetched web/news snippet)" if in_snippets else ""
        parts = [f"{c['verified'] + c['derived']} of {c['figures']} figures verified against source data "
                 f"({c['verified']} exact, {c['derived']} derived); {c['unverified']} flagged{note}."]
    else:
        parts = ["No figures to verify."]
    cit = v["citations"]
    if cit["dangling"]:
        parts.append(f"{len(cit['dangling'])} citation(s) point to no source ({', '.join(cit['dangling'])}).")
    if cit["uncited_paragraphs"]:
        parts.append(f"{len(cit['uncited_paragraphs'])} paragraph(s) state figures without a citation.")
    if cit["source_mismatches"]:
        parts.append(f"{len(cit['source_mismatches'])} paragraph(s) cite a different source than the data.")
    stale = [r["table"] for r in v["freshness"] if r["status"] == "stale"]
    if stale:
        parts.append(f"{len(stale)} stale data series.")
    for t in v["no_data"]["topics_without_data"]:
        if not t["disclosed"]:
            parts.append(f"No data was fetched for {t['topic']}, and the report does not say so.")
        if t["unverified_figures"]:
            parts.append(f"{len(t['unverified_figures'])} unverified figure(s) on {t['topic']}, which has no data.")
    return " ".join(parts)


def annotate_report(report: str, validation: Dict[str, Any]) -> str:
    """Report with the validation summary on top (flagged content is kept as is)."""
    return f"> **Validation:** {validation['summary']}\n\n{report}"
