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
from fact_store import build_fact_store
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
        # any whitespace between words (models often write "S&P\u00a0500")
        pattern = r"\s+".join(re.escape(w) for w in phrase.split())
        spans += [m.span() for m in re.finditer(pattern, text, re.I)]
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
    written_in: Tuple[str, ...] = ()      # labelled assumptions: snippets with the same number and unit, as written
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
                   "change": "derived", "pct_change": "derived", "spread": "derived", "percentile": "derived",
                   "rate": "derived"}


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
            # kept as the writer's assumption, but a number that is in the text of a source the line
            # cites is that source's figure, not an assumption (checked in _needs_attention)
            fig.origin, fig.assumption = "assumption", True
            fig.written_in = _written_in(fig, snippet_figures)
            figures.append(fig)
            continue
        if ref is not None and ref.kind == "quote":
            # quoted from a web/news source: stays "unverified" (not in the data tables), but must
            # appear in the text of the source it is attributed to
            fig.origin = "quote"
            fig.snippet_ids = _in_snippets(fig, snippet_figures, only=ref.source_id)[:1]
            fig.in_source_text = fig.snippet_ids[0] if fig.snippet_ids else None
            fig.matched = f"quoted from {ref.source_id}" + ("" if fig.snippet_ids else " (not found in its text)")
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
            fig.snippet_ids = _in_snippets(fig, snippet_figures)
            fig.in_source_text = fig.snippet_ids[0] if fig.snippet_ids else None
            fig.assumption = _is_assumption(report, fig)
            if _labelled_assumption(report, fig):
                fig.written_in = _written_in(fig, snippet_figures)
        figures.append(fig)
    return figures


def _in_snippets(fig: Figure, snippet_figures: List[Tuple[str, Figure]], only: Optional[str] = None) -> Tuple[str, ...]:
    """Ids of the fetched web/news snippets that contain the same number (optionally one source only).

    Same at the precision the report writes it: "4%" matches a source's "4.2%", but "0.8 pp" does not
    match the "1" in "1 of the 50 states".
    """
    return tuple(dict.fromkeys(
        sid for sid, sf in snippet_figures
        if (only is None or sid == only)
        and (sf.unit == fig.unit or "" in (sf.unit, fig.unit))
        and abs(sf.value - fig.value) <= fig.tolerance))


def _written_in(fig: Figure, snippet_figures: List[Tuple[str, Figure]]) -> Tuple[str, ...]:
    """Snippets containing this exact number with the same unit (stricter than _in_snippets)."""
    return tuple(dict.fromkeys(sid for sid, sf in snippet_figures
                               if sf.unit == fig.unit and abs(sf.value - fig.value) <= 1e-9 * max(1.0, abs(fig.value))))


def _labelled_assumption(report: str, fig: Figure) -> bool:
    """The number itself is followed by "(assumption)" ("16× (assumption)")."""
    return bool(re.match(r"\W{0,3}\(\s*assumption", report[fig.end:fig.end + 40], re.I))


def _is_assumption(report: str, fig: Figure) -> bool:
    """The number is marked as the report's own assumption ("16× (assumption)") or its sentence says so."""
    patterns = _cfg()["assumptions"]["patterns"]
    return _labelled_assumption(report, fig) or any(re.search(p, fig.context, re.I) for p in patterns)


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

# clause boundaries for the direction check: sentence ends (not decimal points), commas, ";",
# line breaks, table cells. "+12 bp, narrowing the differential" talks about two things.
_CLAUSE_END = re.compile(r"[.!?,](?=\s|$)|[;\n|]")


def check_directions(report: str, figures: List[Figure]) -> List[Dict[str, Any]]:
    """Change figures next to a word saying the opposite ("rose" next to a fall), or with the wrong sign."""
    cfg = _cfg()["directions"]
    words = {w: 1 for w in cfg["up"]} | {w: -1 for w in cfg["down"]}
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, words), key=len, reverse=True)) + r")\b", re.I)
    errors = []
    signed = lambda g: -g.value if g.text[:1] in "-\u2212" else g.value  # noqa: E731
    for f in figures:
        if not f.change_sign:
            continue
        # clause around the figure: stop at sentence ends (not decimal points), ";", line breaks and table cells
        cuts = [m.end() for m in _CLAUSE_END.finditer(report, 0, f.start)]
        a = max([0, f.start - cfg["window_chars"]] + cuts[-1:])
        nxt = _CLAUSE_END.search(report, f.end)
        b = min(nxt.start() if nxt else len(report), f.end + cfg["window_chars"])
        change_sign, written_sign = f.change_sign, {"+": 1, "-": -1, "\u2212": -1}.get(f.text[:1])
        if (f.fact_id or "").endswith(".pct_1y"):
            # "inflation fell from {{CPI.pct_1y_prev}} to {{CPI.pct_1y}}": the word describes the rate
            prev = next((g for g in figures if g.fact_id == f.fact_id + "_prev" and a <= g.start < b), None)
            if prev is not None:
                d = signed(f) - signed(prev)
                change_sign, written_sign = (1 if d > 0 else -1 if d < 0 else 0), None
                if not change_sign:
                    continue
        best = None
        for m in pattern.finditer(report, a, b):
            dist = f.start - m.end() if m.end() <= f.start else m.start() - f.end
            if best is None or dist < best[0]:
                best = (dist, m.group(1), words[m.group(1).lower()])
        says = best[2] if best else written_sign
        if says is not None and says != change_sign:
            errors.append({
                "figure": f.text,
                "says": "up" if says > 0 else "down",
                "data": "up" if change_sign > 0 else "down",
                "word": best[1] if best else f.text[:1],
                "fact": f.fact_id or f.matched,
                "context": f.context[:200],
            })
    return errors


# ---------------------------------------------------------------------------
# 6) Comparisons between facts (placeholder reports only)
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[;\n|]")
_CLAUSE_JOIN = re.compile(r",\s|\s(?:or|and|but|while|whereas)\s", re.I)


def check_comparisons(report: str, refs: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """"{{A}} is above {{B}}": A and B must have comparable units and the order must match the data."""
    facts = [r for r in refs or [] if r.kind == "fact" and isinstance(r.fact.value, (int, float))]
    if len(facts) < 2:
        return []
    cfg = _cfg()["comparisons"]
    families = cfg["unit_families"]
    words = {w: 1 for w in cfg["above"]} | {w: -1 for w in cfg["below"]} | {w: 0 for w in cfg["neutral"]}
    pattern = re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, words), key=len, reverse=True)) + r")(?![\w])", re.I)
    # both sides must sit in the same clause as the comparison word: "beta exceeds X or VIX rises
    # above Y" compares beta with X and VIX with Y, never X with Y
    bounds = sorted({0, len(report)} | {m.end() for m in _SENTENCE_END.finditer(report)}
                    | {m.start() for m in _CLAUSE_JOIN.finditer(report)} | {m.end() for m in _CLAUSE_JOIN.finditer(report)})
    issues = []
    for m in pattern.finditer(report):
        lo = max(b for b in bounds if b <= m.start())
        hi = min(b for b in bounds if b >= m.end())
        before = [r for r in facts if lo <= r.start and r.end <= m.start()]
        after = [r for r in facts if m.end() <= r.start and r.end <= hi]
        if not before or not after:
            continue
        a, b = before[-1].fact, after[0].fact
        fa, fb = families.get(a.unit), families.get(b.unit)
        if not fa or not fb:
            continue
        context = report[lo:hi].strip()[:200]
        word = m.group(1).lower()
        if fa != fb:
            issues.append({"kind": "units", "word": word, "a": a.id, "b": b.id, "context": context,
                           "detail": f"{a.id} ({a.unit}) and {b.id} ({b.unit}) are not comparable"})
            continue
        claim = words[word]
        if claim and a.value != b.value and (a.value > b.value) != (claim > 0):
            issues.append({"kind": "order", "word": word, "a": a.id, "b": b.id, "context": context,
                           "detail": f"{a.id} = {a.display} is {'below' if claim > 0 else 'above'} {b.id} = {b.display}"})
    return issues


# ---------------------------------------------------------------------------
# 7) Wording: where a series sits in its history, and which way it is moving
# ---------------------------------------------------------------------------

def _phrase_pattern(phrases: List[str]) -> re.Pattern:
    """Whole-word match for phrases, with any whitespace or hyphen between words."""
    def one(p: str) -> str:
        return r"[\s\-\u2010\u2011]+".join(re.escape(w) for w in re.split(r"[\s\-]+", p.strip()))
    alts = sorted(phrases, key=len, reverse=True)
    return re.compile(r"(?<![\w])(" + "|".join(one(p) for p in alts) + r")(?![\w])", re.I)


def _norm_phrase(text: str) -> str:
    return re.sub(r"[\s\-\u2010\u2011]+", " ", text.strip().lower())


def _sentences(report: str) -> List[Tuple[int, int]]:
    cuts = [0] + [m.end() for m in _SENTENCE_END.finditer(report)] + [len(report)]
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if report[a:b].strip()]


def _series_mentions(report: str, a: int, b: int, refs, store, names) -> List[Tuple[int, str]]:
    """(position, series alias) for every series the sentence points to: a fact it quotes or a name."""
    aliases = {k.split(".")[0] for k, e in store.items() if e.series_id}
    found = [(r.start, r.fact_id.split(".")[0]) for r in refs or []
             if r.kind == "fact" and a <= r.start < b and r.fact.series_id]
    for alias, pattern in names.items():
        if alias in aliases:
            found += [(m.start(), alias) for m in pattern.finditer(report, a, b)]
    return found


def check_wording(report: str, refs, store) -> Dict[str, List[Dict[str, Any]]]:
    cfg = _cfg()["wording"]
    skip = _phrase_pattern(cfg["skip_words"])
    negation = _phrase_pattern(cfg["negations"])
    names = {alias: _phrase_pattern(n) for alias, n in cfg["series_names"].items()}
    hist = cfg["history"]
    history_pat = {c: _phrase_pattern(hist[c]) for c in ("low", "high", "middle")}
    trend = cfg["trend"]
    trend_words = ({_norm_phrase(w): 1 for w in trend["up"]} | {_norm_phrase(w): -1 for w in trend["down"]}
                   | {"tightening": 0})
    trend_pat = _phrase_pattern(list(trend_words))
    horizon_pat = _phrase_pattern(list(trend["horizons"]))
    horizons = {_norm_phrase(k): v for k, v in trend["horizons"].items()}
    groups = {s["alias"]: s.get("spread_group") for s in load_config("data_sources")["fred"]["series"]}
    # history words are judged only against a full window ("5y", "10y"): a report built on one year
    # of data cannot be told it is wrong about "five-year lows"
    full_windows = {f"pctile_{w}" for w in load_config("data_sources")["fred"]["history_label"].values()}
    rate = cfg["rate_trend"]
    rate_words = {_norm_phrase(w): 1 for w in rate["up"]} | {_norm_phrase(w): -1 for w in rate["down"]}
    rate_pat = _phrase_pattern(list(rate_words))
    rate_exclude = _phrase_pattern(rate["exclude"])
    rate_other = _phrase_pattern(rate["other_measures"])
    history_issues, trend_issues, rate_issues = [], [], []

    for a, b in _sentences(report):
        sentence = report[a:b]
        if skip.search(sentence):
            continue  # hedged or conditional: a scenario, not a claim about the data
        mentions = _series_mentions(report, a, b, refs, store, names)
        if not mentions:
            continue

        def nearest(pos: int) -> str:
            return min(mentions, key=lambda m: abs(m[0] - pos))[1]

        def negated(m) -> bool:
            return bool(negation.search(report, max(a, m.start() - 25), m.start()))

        for cls, pattern in history_pat.items():
            for m in pattern.finditer(report, a, b):
                if negated(m):
                    continue
                alias = nearest(m.start())
                key = next((k for k in store if k.startswith(f"{alias}.") and k.split(".", 1)[1] in full_windows), None)
                if not key:
                    continue
                pct = store[key].value
                lo, hi = hist["middle_range"]
                wrong = ((cls == "low" and pct > hist["low_max_pctile"])
                         or (cls == "high" and pct < hist["high_min_pctile"])
                         or (cls == "middle" and not lo <= pct <= hi))
                if wrong:
                    history_issues.append({"phrase": m.group(1), "says": cls, "series": alias, "pctile": pct,
                                           "fact": key, "context": sentence.strip()[:200]})

        h = horizon_pat.search(sentence)
        horizon = horizons.get(_norm_phrase(h.group(1))) if h else None
        for m in trend_pat.finditer(report, a, b):
            if negated(m):
                continue
            word = _norm_phrase(m.group(1))
            alias = nearest(m.start())
            if alias in trend["skip_series"]:
                continue
            says = trend_words.get(word, 0)
            if word == "tightening":
                says = {"up": 1, "down": -1}.get(trend["tightening"].get(groups.get(alias) or "", ""), 0)
            if not says:
                continue
            # a sentence that quotes a change of this series is covered by the direction check
            if any(r.kind == "fact" and a <= r.start < b and r.fact_id.startswith(f"{alias}.")
                   and r.fact.is_change for r in refs or []):
                continue
            suffixes = [horizon] if horizon else ["1m", "1q", "1w"]
            key = next((f"{alias}.{kind}_{sfx}" for sfx in suffixes for kind in ("chg", "pct")
                        if f"{alias}.{kind}_{sfx}" in store), None)
            if not key or not store[key].value:
                continue
            if (store[key].value > 0) != (says > 0):
                trend_issues.append({"word": word, "says": "up" if says > 0 else "down", "series": alias,
                                     "fact": key, "change": store[key].display, "context": sentence.strip()[:200]})

        excluded = [x.span() for x in rate_exclude.finditer(report, a, b)]
        other_measure = bool(rate_other.search(report, a, b))
        for m in rate_pat.finditer(report, a, b):
            alias = nearest(m.start())
            if negated(m) or alias not in rate["series"] or other_measure:
                continue
            if any(s <= m.start() < e for s, e in excluded):
                continue
            word = _norm_phrase(m.group(1))
            found = _rate_direction(store, alias, rate["min_gap"])
            if found and found[0] != rate_words[word]:
                rate_issues.append({"word": word, "says": "up" if rate_words[word] > 0 else "down", "series": alias,
                                    "evidence": found[1], "facts": found[2], "context": sentence.strip()[:200]})
    return {"history": history_issues, "trend": trend_issues, "rate": rate_issues}


def _rate_direction(store, alias: str, gap: float) -> Optional[Tuple[int, str, List[str]]]:
    """(+1 speeding up / -1 slowing down, evidence, fact ids) when both rate measures agree, else None."""
    ids = {k: f"{alias}.{k}" for k in ("pct_1y", "pct_1y_prev", "ann_3m", "ann_growth_1q", "ann_growth_1q_prev")}
    got = {k: store[i] for k, i in ids.items() if i in store}
    if "pct_1y" not in got or "pct_1y_prev" not in got:
        return None
    yoy, prev = got["pct_1y"], got["pct_1y_prev"]
    if "ann_3m" in got:
        pace, against = got["ann_3m"], yoy
        pace_text = f"the last 3 months ran at {pace.display} annualized"
    elif "ann_growth_1q" in got and "ann_growth_1q_prev" in got:
        pace, against = got["ann_growth_1q"], got["ann_growth_1q_prev"]
        pace_text = f"the latest quarter grew {pace.display} annualized after {against.display}"
    else:
        return None
    sign = lambda d: 1 if d > gap else -1 if d < -gap else 0  # noqa: E731
    a, b = sign(yoy.value - prev.value), sign(pace.value - against.value)
    if not a or a != b:
        return None
    evidence = f"its 12-month rate went from {prev.display} to {yoy.display} and {pace_text}"
    return a, evidence, [prev.id, yoy.id, pace.id]


def classify_lines(report: str, figures: List[Figure]) -> List[Dict[str, Any]]:
    """Each bullet / paragraph: "data" if it states a figure from the data or a quoted source, else
    "interpretation" (headings and very short lines are left out)."""
    out = []
    for a, b in _units(report):
        text = report[a:b].strip()
        if len(text.split()) < 4 or set(text) <= set("|-: "):
            continue
        backed = any(a <= f.start < b and (f.status in ("verified", "derived") or f.origin in ("fact", "quote"))
                     for f in figures)
        out.append({"start": a, "end": b, "kind": "data" if backed else "interpretation"})
    return out


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
        "comparisons": check_comparisons(report, refs),
        "wording": check_wording(report, refs, build_fact_store(tables)),
        "lines": classify_lines(report, figures),
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
    if f["assumption"]:
        return bool(mislabelled_assumption(f))
    if f.get("origin") == "fact":
        return False
    if f.get("origin") == "quote":
        return not f["snippet_ids"]  # a quote must be found in the source it names
    if placeholders and f["unit"] != "date":
        return True
    if f["status"] != "unverified":
        return False
    return not set(f["snippet_ids"]) & set(f["cited"])


def mislabelled_assumption(f: Dict[str, Any]) -> Optional[str]:
    """The cited source whose text contains a number labelled as the writer's assumption, if any.

    "New York Fed: 25.2% (assumption) [S4]" with 25.2% in S4's text is S4's figure, and labelling it
    an assumption would let a revision clear a typed number without grounding it.
    """
    if not f.get("assumption"):
        return None
    return next((sid for sid in f.get("written_in", ()) if sid in f.get("cited", ())), None)


def open_issues(v: Dict[str, Any]) -> int:
    """Problems a writer can fix: used to decide whether a revision is better than the draft."""
    cit = v["citations"]
    ph = v.get("placeholders", False)
    return (sum(_needs_attention(f, ph) for f in v["figures"])
            + len(cit["uncited_paragraphs"]) + len(cit["source_mismatches"]) + len(cit["dangling"])
            + sum(not t["disclosed"] for t in v["no_data"]["topics_without_data"])
            + len(v.get("directions", [])) + len(v.get("unknown_facts", [])) + len(v.get("comparisons", []))
            + sum(len(x) for x in (v.get("wording") or {}).values()))


def feedback_for_writer(v: Dict[str, Any], max_items: int = 25) -> str:
    """The validator's findings as concrete instructions for a revision pass ("" if nothing to fix)."""
    items: List[str] = []
    ph = v.get("placeholders", False)
    for fid in v.get("unknown_facts", []):
        items.append(f"{{{{{fid}}}}} is not in the FACT TABLE; use an ID from the table or rewrite without it.")
    wording = v.get("wording") or {}
    for w in wording.get("history", []):
        items.append(f'"{w["context"][:160]}" says "{w["phrase"]}", but {w["series"]} sits at the '
                     f'{w["pctile"]}th percentile of its history ({{{{{w["fact"]}}}}}); fix the wording.')
    for w in wording.get("trend", []):
        items.append(f'"{w["context"][:160]}" says "{w["word"]}", but {w["series"]} moved {w["change"]} '
                     f'({{{{{w["fact"]}}}}}); fix the wording or name the period you mean.')
    for w in wording.get("rate", []):
        ids = ", ".join(f"{{{{{i}}}}}" for i in w["facts"])
        items.append(f'"{w["context"][:160]}" says "{w["word"]}", but {w["series"]} is '
                     f'{"speeding up" if w["says"] == "down" else "slowing down"}: {w["evidence"]} ({ids}); '
                     "fix the wording.")
    for c in v.get("comparisons", []):
        items.append(f'"{c["context"][:160]}" compares with "{c["word"]}", but {c["detail"]}; '
                     "fix or drop the comparison.")
    for d in v.get("directions", []):
        items.append(f'"{d["context"][:160]}" says "{d["word"]}" ({d["says"]}), but {d["fact"]} went {d["data"]}; '
                     "fix the wording.")
    for f in v["figures"]:
        if not _needs_attention(f, ph):
            continue
        where = f'"{f["text"]}" in: "{f["context"][:160]}"'
        sid = mislabelled_assumption(f)
        if sid:
            fix = f"quote it as {{{{{sid}: {f['text']}}}}}" if ph else f"drop the \"(assumption)\" label and keep [{sid}]"
            items.append(f"{where} — labelled as an assumption, but it is in the text of [{sid}], which this "
                         f"line cites; {fix}.")
        elif f.get("origin") == "quote":
            items.append(f"{where} — quoted from a source whose text does not contain it; fix the number or "
                         "the source, or drop it.")
        elif ph and f["snippet_ids"]:
            sid = f["snippet_ids"][0]
            items.append(f"{where} — typed directly; it appears in the text of [{sid}], so quote it as "
                         f"{{{{{sid}: {f['text']}}}}}. Use {{{{assume: …}}}} only for your own estimates.")
        elif ph and f["status"] != "unverified":
            items.append(f"{where} — typed directly; it matches \"{f['matched']}\", so write the matching "
                         "{{ID}} from the FACT TABLE.")
        elif ph:
            items.append(f"{where} — typed directly and not in the FACT TABLE or any source text; remove it, or "
                         "write it as {{assume: …}} only if it is your own estimate.")
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
        mislabelled = sum(bool(mislabelled_assumption(f)) for f in v["figures"])
        notes = [f"{in_snippets} of them appear in a fetched web/news snippet" if in_snippets else "",
                 f"{assumed} labelled as assumptions" if assumed else "",
                 f"{mislabelled} of those labels on a number from a source the line cites" if mislabelled else ""]
        notes = [n for n in notes if n]
        note = f" ({'; '.join(notes)})" if notes else ""
        parts = [f"{c['verified'] + c['derived']} of {c['figures']} figures verified against source data "
                 f"({c['verified']} exact, {c['derived']} derived); {c['unverified']} flagged{note}."]
    else:
        parts = ["No figures to verify."]
    if v.get("placeholders"):
        typed = sum(f.get("origin") == "free" and f["unit"] != "date" for f in v["figures"])
        from_facts = sum(f.get("origin") == "fact" for f in v["figures"])
        quoted = sum(f.get("origin") == "quote" for f in v["figures"])
        parts.append(f"{from_facts} figure(s) inserted from the fact table, {quoted} quoted from web sources, "
                     f"{typed} typed directly.")
    if v.get("directions"):
        parts.append(f"{len(v['directions'])} change figure(s) described in the wrong direction.")
    if v.get("comparisons"):
        parts.append(f"{len(v['comparisons'])} comparison(s) between facts that do not hold.")
    wording = v.get("wording") or {}
    n_wording = sum(len(x) for x in wording.values())
    if n_wording:
        parts.append(f"{n_wording} statement(s) about history or trend that the data contradicts.")
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
