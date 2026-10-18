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
      | (?P<times>[x\u00d7])(?![\w])
    )?
    (?![\w/]|[.,]\d)
""", re.I | re.X)

_MONTHS = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           "|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_DATE_RE = re.compile(
    rf"\b(?:(?P<iso>(?:19|20)\d{{2}}[-\u2010\u2011\u2012]\d{{2}}[-\u2010\u2011\u2012]\d{{2}})"
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
    source_ids: Tuple[str, ...] = ()  # every source the matched fact comes from (two for a spread)
    topic_match: Optional[bool] = None
    in_source_text: Optional[str] = None  # S# of a snippet that contains the same number
    snippet_ids: Tuple[str, ...] = ()     # every snippet that contains the same number
    assumption: bool = False              # the report labels this number as its own assumption
    cited: Tuple[str, ...] = ()           # citation ids in the same bullet / paragraph
    context: str = ""
    origin: str = "free"                  # "fact" (inserted from {{ID}}), "assumption" ({{assume: …}}) or "free" (typed)
    fact_id: Optional[str] = None         # the fact a placeholder pointed to
    change_sign: Optional[int] = None     # +1 / -1 if the matched fact is a change, for the direction check


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
        iso = re.sub(f"[{_HYPHENS}]", "-", m.group("iso"))
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


_STOPWORDS = {"the", "and", "for", "with", "from", "that", "this", "ttm", "yoy", "annual", "latest", "change"}


def _words(text: str) -> Set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOPWORDS}


def _topic_match(sentence: str, fact: Fact, sentence_topics: Set[str], series_keywords: Dict[str, List[str]]) -> bool:
    if fact.series_id and matches_any(sentence, series_keywords.get(fact.series_id, [])):
        return True
    return bool(sentence_topics & set(fact.topics))


_STATUS_BY_KIND = {"level": "verified", "stat": "verified", "date": "verified", "text": "verified",
                   "change": "derived", "pct_change": "derived", "spread": "derived", "percentile": "derived"}


def _sign(v: float) -> Optional[int]:
    return None if not v else (1 if v > 0 else -1)


def check_numeric_grounding(
    report: str,
    question: str,
    tables: List[Dict[str, Any]],
    sources: List[Dict[str, Any]],
    refs: Optional[List[Any]] = None,
) -> List[Figure]:
    """Classify every figure; refs (from render.py) say which spans were inserted from facts."""
    refs = refs or []
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
            if any(r.kind == "fact" and r.start <= fig.start < r.end for r in refs):
                fig.origin, fig.status, fig.matched = "fact", "verified", "date from the fact table"
                figures.append(fig)
                continue
            iso, ym = _parse_date(fig.text)
            if (iso and iso in dates) or (not iso and ym in months):
                fig.status, fig.matched = "verified", "observation / as-of date in fetched data"
            figures.append(fig)
            continue

        ref = next((r for r in refs if r.start <= fig.start < r.end), None)
        if ref is not None and ref.kind == "fact":
            f = ref.fact
            fig.origin, fig.fact_id = "fact", f.id
            fig.status, fig.matched, fig.topic_match = _STATUS_BY_KIND.get(f.kind, "verified"), f.label, True
            fig.source_ids = tuple(url_to_id[u] for u in f.source_urls if u in url_to_id)
            fig.source_id = fig.source_ids[0] if fig.source_ids else None
            fig.change_sign = _sign(f.value) if f.is_change else None
            figures.append(fig)
            continue
        if ref is not None and ref.kind == "assumption":
            fig.origin, fig.assumption = "assumption", True
            figures.append(fig)
            continue

        if round(fig.value, 9) in question_values:
            continue  # the number comes from the question itself

        sentence_topics = set(topics_in(sentence))
        sentence_words = _words(sentence)
        best: Optional[Tuple[Tuple[int, int, float], Fact]] = None
        for fact in facts:
            for x, tol in _candidate_values(fig, fact):
                err = abs(x - abs(fact.value))
                if err <= tol:
                    topical = _topic_match(sentence, fact, sentence_topics, series_keywords)
                    # prefer facts about what the sentence discusses, then exact values, then a field whose
                    # name shares words with the sentence ("earnings growth" -> "EPS growth", not "ROE"),
                    # then closeness, then the most recent observation
                    recency = -int((fact.date or "0").replace("-", "")[:8] or 0)
                    overlap = -len(sentence_words & _words(fact.label))
                    rank = (0 if topical else 1, 0 if fact.kind == "observed" else 1, overlap, err / max(tol, 1e-12), recency)
                    if best is None or rank < best[0]:
                        best = (rank, fact)
        if best:
            fact = best[1]
            fig.status = "verified" if fact.kind == "observed" else "derived"
            fig.matched = fact.label
            fig.source_id = url_to_id.get(fact.source_url)
            fig.source_ids = tuple(url_to_id[u] for u in (fact.source_url, *fact.other_source_urls) if u in url_to_id)
            fig.topic_match = best[0][0] == 0
            if " change " in fact.label or "annualized growth" in fact.label:
                fig.change_sign = _sign(fact.value)
        else:
            fig.snippet_ids = tuple(dict.fromkeys(
                sid for sid, sf in snippet_figures
                if (sf.unit == fig.unit or "" in (sf.unit, fig.unit))
                and abs(sf.value - fig.value) <= max(sf.tolerance, fig.tolerance)))
            fig.in_source_text = fig.snippet_ids[0] if fig.snippet_ids else None
            fig.assumption = _is_assumption(report, fig)
        figures.append(fig)
    return figures


def _is_assumption(report: str, fig: Figure) -> bool:
    """The number is marked as the report's own assumption ("16× (assumption)") or its sentence says so."""
    after = report[fig.end:fig.end + 40]
    patterns = _cfg()["assumptions"]["patterns"]
    return bool(re.match(r"\W{0,3}\(\s*assumption", after, re.I)) or any(re.search(p, fig.context, re.I) for p in patterns)


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


def _in_snippet(fig: Figure, snippet_figs: List[Figure]) -> bool:
    return any((sf.unit == fig.unit or "" in (sf.unit, fig.unit)) and abs(sf.value - fig.value) <= max(sf.tolerance, fig.tolerance)
               for sf in snippet_figs)


def check_citations(report: str, sources: List[Dict[str, Any]], figures: List[Figure]) -> Dict[str, Any]:
    markers = parse_citation_markers(report)
    snippet_figs = {s["id"]: [f for f in extract_figures(s.get("snippet") or "") if f.unit != "date"] for s in sources}
    uncited, mismatched = [], []
    for a, b in _units(report):
        cited = {i for m in markers if a <= m["start"] < b for i in m["ids"]}
        for f in figures:
            if a <= f.start < b:
                f.cited = tuple(sorted(cited, key=lambda i: int(i[1:])))
        grounded = [f for f in figures if a <= f.start < b and f.status in ("verified", "derived") and f.unit != "date"]
        if not grounded:
            continue
        snippet = report[a:b].strip()[:200]
        data_sources = sorted({i for f in grounded for i in f.source_ids}, key=lambda i: int(i[1:]))
        if not cited:
            uncited.append({"text": snippet, "figures": [f.text for f in grounded], "sources": data_sources})
            continue
        # a figure is mis-cited only if none of its data sources is cited and the
        # cited sources' own text does not contain it either
        wrong = [f.text for f in grounded
                 if f.source_ids and not set(f.source_ids) & cited
                 and not any(_in_snippet(f, snippet_figs.get(i, [])) for i in cited)]
        if wrong:
            mismatched.append({"text": snippet, "figures": wrong, "cited": sorted(cited), "sources": data_sources})
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
                      if f.status == "unverified" and f.unit != "date" and not f.assumption
                      and topic in topics_in(f.context)]
        topics.append({"topic": topic, "disclosed": disclosed, "unverified_figures": unverified})
    issue = any(not t["disclosed"] or t["unverified_figures"] for t in topics)
    return {"topics_without_data": topics, "issue": issue}


# ---------------------------------------------------------------------------
# 5) Direction of change
# ---------------------------------------------------------------------------

_CLAUSE_END = re.compile(r"[.!?](?=\s|$)|[;\n|]")


def check_directions(report: str, figures: List[Figure]) -> List[Dict[str, Any]]:
    """Change figures next to a word saying the opposite ("rose" next to a fall), or with the wrong sign."""
    cfg = _cfg()["directions"]
    words = {w: 1 for w in cfg["up"]} | {w: -1 for w in cfg["down"]}
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, words), key=len, reverse=True)) + r")\b", re.I)
    errors = []
    for f in figures:
        if not f.change_sign:
            continue
        # clause around the figure: stop at sentence ends (not decimal points), ";", line breaks and table cells
        cuts = [m.end() for m in _CLAUSE_END.finditer(report, 0, f.start)]
        a = max([0, f.start - cfg["window_chars"]] + cuts[-1:])
        nxt = _CLAUSE_END.search(report, f.end)
        b = min(nxt.start() if nxt else len(report), f.end + cfg["window_chars"])
        best = None
        for m in pattern.finditer(report, a, b):
            dist = f.start - m.end() if m.end() <= f.start else m.start() - f.end
            if best is None or dist < best[0]:
                best = (dist, m.group(1), words[m.group(1).lower()])
        written_sign = {"+": 1, "-": -1, "\u2212": -1}.get(f.text[:1])
        says = best[2] if best else written_sign
        if says is not None and says != f.change_sign:
            errors.append({
                "figure": f.text,
                "says": "up" if says > 0 else "down",
                "data": "up" if f.change_sign > 0 else "down",
                "word": best[1] if best else f.text[:1],
                "fact": f.fact_id or f.matched,
                "context": f.context[:200],
            })
    return errors


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
    refs: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Run all checks; the result is JSON-serializable and stored with the report.

    refs come from render.py when the report was written with fact placeholders;
    then numbers typed directly by the model ("free" numbers) are open issues too.
    """
    figures = check_numeric_grounding(report, question, tables, sources, refs)
    citations = check_citations(report, sources, figures)
    counts = {k: sum(f.status == k for f in figures) for k in ("verified", "derived", "unverified")}
    result = {
        "counts": {"figures": len(figures), **counts},
        "figures": [asdict(f) for f in figures],
        "citations": citations,
        "freshness": check_freshness(tables, today),
        "no_data": check_no_data(report, tags, tables, figures),
        "data_status": [s for s in (data_status or []) if not s.get("ok")],
        "valid_source_ids": [s["id"] for s in sources],
        "directions": check_directions(report, figures),
        "placeholders": refs is not None,
        "unknown_facts": [r.fact_id for r in (refs or []) if r.kind == "unknown"],
    }
    result["open_issues"] = open_issues(result)
    result["summary"] = summary_line(result)
    return result


def _needs_attention(f: Dict[str, Any], placeholders: bool = False) -> bool:
    """A figure the writer should fix.

    Free text: unverified, not labelled as an assumption, and not backed by a
    snippet the paragraph cites. Placeholder mode: also any number typed
    directly instead of being written as {{ID}} (dates excepted).
    """
    if f["assumption"] or f.get("origin") == "fact":
        return False
    if placeholders and f["unit"] != "date":
        return True
    if f["status"] != "unverified":
        return False
    return not set(f["snippet_ids"]) & set(f["cited"])


def open_issues(v: Dict[str, Any]) -> int:
    """Problems a writer can fix: used to decide whether a revision is better than the draft."""
    cit = v["citations"]
    ph = v.get("placeholders", False)
    return (sum(_needs_attention(f, ph) for f in v["figures"])
            + len(cit["uncited_paragraphs"]) + len(cit["source_mismatches"]) + len(cit["dangling"])
            + sum(not t["disclosed"] for t in v["no_data"]["topics_without_data"])
            + len(v.get("directions", [])) + len(v.get("unknown_facts", [])))


def feedback_for_writer(v: Dict[str, Any], max_items: int = 25) -> str:
    """The validator's findings as concrete instructions for a revision pass ("" if nothing to fix)."""
    items: List[str] = []
    ph = v.get("placeholders", False)
    for fid in v.get("unknown_facts", []):
        items.append(f"{{{{{fid}}}}} is not in the FACT TABLE; use an ID from the table or rewrite without it.")
    for d in v.get("directions", []):
        items.append(f'"{d["context"][:160]}" says "{d["word"]}" ({d["says"]}), but {d["fact"]} went {d["data"]}; '
                     "fix the wording.")
    for f in v["figures"]:
        if not _needs_attention(f, ph):
            continue
        where = f'"{f["text"]}" in: "{f["context"][:160]}"'
        if ph:
            items.append(f"{where} — typed directly; write it as the matching {{{{ID}}}} from the FACT TABLE, "
                         "or as {{assume: …}} if it is your own estimate.")
        elif f["snippet_ids"]:
            items.append(f"{where} — this number appears in source [{f['snippet_ids'][0]}]; cite it in that sentence.")
        elif f["unit"] == "date":
            items.append(f"{where} — this date is not in the fetched data; use a date from KEY DATA or remove it.")
        else:
            items.append(f"{where} — not in KEY DATA or any source; replace it with the KEY DATA value and its [S#], "
                         "remove it, or mark it \"(assumption)\" if it is your own assumption.")
    cit = v["citations"]
    for u in cit["uncited_paragraphs"]:
        src = ", ".join(f"[{i}]" for i in u["sources"]) or "its source"
        items.append(f'Add a citation to: "{u["text"][:160]}" — its figures {", ".join(u["figures"])} come from {src}.')
    for m in cit["source_mismatches"]:
        src = ", ".join(f"[{i}]" for i in m["sources"])
        items.append(f'"{m["text"][:160]}" cites {", ".join(m["cited"])}, but figures {", ".join(m["figures"])} come from {src}.')
    if cit["dangling"]:
        valid = ", ".join(v.get("valid_source_ids", [])) or "none"
        items.append(f"Citations {', '.join(cit['dangling'])} do not exist; the only sources are {valid}.")
    for t in v["no_data"]["topics_without_data"]:
        if not t["disclosed"]:
            items.append(f"No data was fetched for {t['topic']}; add one sentence saying so.")
    if not items:
        return ""
    more = f"\n(and {len(items) - max_items} more of the same kind)" if len(items) > max_items else ""
    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(items[:max_items], start=1))
    return "VALIDATION FEEDBACK (deterministic checks of your draft against the fetched data):\n" + numbered + more


def summary_line(v: Dict[str, Any]) -> str:
    c = v["counts"]
    if c["figures"]:
        in_snippets = sum(f["status"] == "unverified" and bool(f["in_source_text"]) for f in v["figures"])
        assumed = sum(f["status"] == "unverified" and f["assumption"] for f in v["figures"])
        notes = [f"{in_snippets} of them appear in a fetched web/news snippet" if in_snippets else "",
                 f"{assumed} labelled as assumptions" if assumed else ""]
        notes = [n for n in notes if n]
        note = f" ({'; '.join(notes)})" if notes else ""
        parts = [f"{c['verified'] + c['derived']} of {c['figures']} figures verified against source data "
                 f"({c['verified']} exact, {c['derived']} derived); {c['unverified']} flagged{note}."]
    else:
        parts = ["No figures to verify."]
    if v.get("placeholders"):
        typed = sum(f.get("origin") == "free" and f["unit"] != "date" for f in v["figures"])
        from_facts = sum(f.get("origin") == "fact" for f in v["figures"])
        parts.append(f"{from_facts} figure(s) inserted from the fact table, {typed} typed directly.")
    if v.get("directions"):
        parts.append(f"{len(v['directions'])} change figure(s) described in the wrong direction.")
    if v.get("unknown_facts"):
        parts.append(f"{len(v['unknown_facts'])} unknown fact reference(s).")
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
