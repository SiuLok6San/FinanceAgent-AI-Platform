"""
answer_builder.py
-----------------
Final Writer and source handling.

Responsibilities:
- Normalize sources and give them citation ids [S1], [S2], ...
- Parse citation markers in a report ([S1], [S1, S2], 【S1】, (S1))
- Write the report as a template with fact placeholders ({{HY_OAS.latest}});
  render.py turns it into text with exact values and citations
- Revise a template using the validator's feedback
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any, Dict, List, Optional

from llm import call_llm
from settings import load_config

WRITER_TEMPERATURE = load_config("pipeline")["temperatures"]["writer"]

PLACEHOLDER_RULES = (
    "NUMBER RULES (strict):\n"
    "- Write every number from the data as its placeholder, e.g. 'HY spreads are {{HY_OAS.latest}}, "
    "{{HY_OAS.chg_1m}} over the past month'. Copy IDs exactly from the FACT TABLE. Never type digits yourself.\n"
    "- A placeholder already contains its unit and sign ({{HY_OAS.chg_1m}} becomes '+53 bp'): do not add %, bp, "
    "+ or - around it.\n"
    "- A number quoted from a web/news source in SOURCES: {{S5: 2.9%}} (copy it exactly as the source writes it).\n"
    "- Your own estimate (a scenario level, a suggested weight): {{assume: 10-15%}}.\n"
    "- Words must match the sign of a change: positive changes rose / widened / increased, negative changes "
    "fell / narrowed / decreased.\n"
    "- Citations [S#] for numbers are added automatically; cite web sources yourself only for qualitative claims."
)

WRITER_SYSTEM_PROMPT = textwrap.dedent("""
    You are the Final Writer for B17 Institutional Research.

    Objectives:
    - Produce a crisp, structured, professional finance report.
    - Avoid long paragraphs. Prefer bullets + concise sections.
    - Use ONLY the FACT TABLE and the provided sources.
    - If a claim cannot be supported → phrase it as a scenario (e.g., “likely”, “probable”).

    Required Sections:
    1. Executive Summary (3–5 bullets)
    2. Core Analysis (macro / sector / credit / portfolio)
    3. Scenarios (base / upside / downside)
    4. Key Risks & Monitoring
    5. Portfolio / Decision Implications
    6. Conclusion

    Additional rules:
    - DO NOT include tables or charts inside the text. Mention them by name only:
        “See Table 1 for yield-curve shift data.”
    - For every topic listed under NO DATA FETCHED, write one sentence saying
      that no data was fetched for that topic, and keep that part qualitative.
    - Professional tone. No filler language.
""").strip() + "\n\n" + PLACEHOLDER_RULES


# ---------------------------------------------------------------------
# Source Normalization
# ---------------------------------------------------------------------

def normalize_sources(raw: Any) -> List[Dict[str, Any]]:
    """
    Normalize sources into:
      [
        {
           "id": "S1",
           "title": "...",
           "url": "...",
           "source": "...",
           "snippet": "..."
        }, ...
      ]
    """
    if not raw:
        return []

    # If string, attempt JSON
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = [{"title": raw, "url": "", "source": "unknown"}]

    sources = []
    if isinstance(raw, list):
        for i, s in enumerate(raw, start=1):
            if isinstance(s, str):
                entry = {"title": s, "url": "", "source": "unknown", "snippet": ""}
            else:
                entry = {
                    "title": s.get("title") or s.get("name") or f"Source {i}",
                    "url": s.get("url") or s.get("link") or "",
                    "source": s.get("source") or "web",
                    "snippet": s.get("snippet") or s.get("summary") or "",
                }
            entry["id"] = f"S{i}"
            sources.append(entry)

    return sources


def build_sources_block(sources: List[Dict[str, Any]]) -> str:
    """
    For the LLM: show all sources with their IDs.
    """
    if not sources:
        return "SOURCES: (none)"

    lines = ["SOURCES (use citation tags like [S1], [S2] when referencing):"]
    for s in sources:
        snippet = (s.get("snippet") or "").replace("\n", " ")[:200]
        line = f"[{s['id']}] {s.get('title','')} | {s.get('url','')} | {snippet}"
        lines.append(line)
    return "\n".join(lines)


# [S1], [S1, S3], [S1; S2], [S1-S3] / [S1–3]; some models write 【S1】, 【S1†L4-L9】 or (S4, S8)
_CITATION_RE = re.compile(
    r"[\[\u3010]\s*(S\d+(?:\s*(?:[,;]|[-\u2013])\s*S?\d+)*)\s*(?:\u2020[^\]\u3011]*)?[\]\u3011]"
    r"|\(\s*(S\d+(?:\s*(?:[,;]|[-\u2013])\s*S?\d+)*)\s*\)"
)


def parse_citation_markers(text: str) -> List[Dict[str, Any]]:
    """Every citation marker in text: [{"ids": ["S1", "S2"], "start": i, "end": j}, ...]."""
    out = []
    for m in _CITATION_RE.finditer(text or ""):
        ids: List[str] = []
        for part in re.split(r"\s*[,;]\s*", m.group(1) or m.group(2)):
            rng = re.match(r"S(\d+)\s*[-\u2013]\s*S?(\d+)$", part)
            if rng and int(rng.group(1)) <= int(rng.group(2)) <= int(rng.group(1)) + 50:
                ids.extend(f"S{i}" for i in range(int(rng.group(1)), int(rng.group(2)) + 1))
            else:
                ids.append(part.strip())
        out.append({"ids": ids, "start": m.start(), "end": m.end()})
    return out


def extract_citations_used(text: str, sources: List[Dict[str, Any]]) -> List[str]:
    """Citation ids used in text that refer to a real source."""
    ids = {s["id"] for s in sources}
    found = {i for m in parse_citation_markers(text) for i in m["ids"]}
    return sorted((i for i in found if i in ids), key=lambda i: int(i[1:]))


def find_dangling_citations(text: str, sources: List[Dict[str, Any]]) -> List[str]:
    """Citation ids used in text that do NOT refer to any fetched source."""
    ids = {s["id"] for s in sources}
    found = {i for m in parse_citation_markers(text) for i in m["ids"]}
    return sorted((i for i in found if i not in ids), key=lambda i: int(i[1:]))


# ---------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------

def _tables_and_charts(data_tables: List[Dict[str, Any]], chart_specs: List[Dict[str, Any]]) -> str:
    lines = ["AVAILABLE TABLES:"]
    for i, t in enumerate(data_tables, start=1):
        lines.append(f"- Table {i}: {t.get('title', 'Untitled')}")
    lines.append("AVAILABLE CHARTS:")
    for i, c in enumerate(chart_specs, start=1):
        lines.append(f"- Chart {i}: {c.get('title', 'Untitled')}")
    return "\n".join(lines)


def write_template(
    question: str,
    analyst: str,
    reasoner: str,
    tags: List[str],
    tier2_subtags: Dict[str, str],
    fact_table: str,
    no_data_topics: List[str],
    sources: List[Dict[str, Any]],
    data_tables: List[Dict[str, Any]],
    chart_specs: List[Dict[str, Any]],
) -> str:
    """The report as a template with fact placeholders (rendered by render.py)."""
    user_prompt = "\n\n".join([
        f"USER QUESTION:\n{question}",
        f"TOPICS: {tags}\nTIER-2 SPECIALIZATION: {tier2_subtags}",
        f"--- ANALYST ---\n{analyst}",
        f"--- REASONER ---\n{reasoner}",
        fact_table,
        "NO DATA FETCHED FOR: " + (", ".join(no_data_topics) if no_data_topics else "(none)"),
        _tables_and_charts(data_tables, chart_specs),
        build_sources_block(sources),
        "Now write the final institutional report, with every number as a placeholder.",
    ])
    # LLMError propagates; run_multiagent_pipeline turns it into a friendly message
    return call_llm(WRITER_SYSTEM_PROMPT, user_prompt, temperature=WRITER_TEMPERATURE)


def revise_template(
    question: str,
    template: str,
    feedback: str,
    fact_table: str,
    sources: List[Dict[str, Any]],
) -> str:
    """Second Writer pass: revise the template using the validator's findings."""
    user_prompt = "\n\n".join([
        f"USER QUESTION:\n{question}",
        fact_table,
        build_sources_block(sources),
        f"YOUR DRAFT REPORT (template with placeholders):\n{template}",
        feedback,
        "Return the complete revised report template with the same sections. Fix every item above. "
        "Keep every number as a placeholder.",
    ])
    return call_llm(WRITER_SYSTEM_PROMPT, user_prompt, temperature=WRITER_TEMPERATURE)
