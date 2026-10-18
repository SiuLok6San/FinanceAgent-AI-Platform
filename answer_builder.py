"""
answer_builder.py
-----------------
Final institutional report builder.

Responsibilities:
- Take multi-agent outputs + data tables + charts + sources
- Produce a clean, readable institutional-style report text
- Insert proper citation markers [S1], [S2], …
- Return a structured dict consumed directly by app.py:
        {
            "report_text": ...,
            "key_data_tables": [...],
            "chart_specs": [...],
            "sources": [...],
            "citations_used": [...]
        }
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any, Dict, List, Optional

from llm import call_llm
from settings import load_config

WRITER_TEMPERATURE = load_config("pipeline")["temperatures"]["writer"]

WRITER_SYSTEM_PROMPT = textwrap.dedent("""
    You are the Final Writer for B17 Institutional Research.

    Objectives:
    - Produce a crisp, structured, professional finance report.
    - Avoid long paragraphs. Prefer bullets + concise sections.
    - Use ONLY provided sources and cite them as [S1], [S2], ... (plain square brackets).
    - If a claim cannot be sourced → phrase as scenario (e.g., “likely”, “probable”).

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
    - DO NOT fabricate numbers or dates. Every number and date you state must
      appear in KEY DATA or in a source snippet, and must carry the [S#] of
      that source in the same bullet or sentence.
    - A number that is your own assumption (a scenario level, a suggested
      weight) must be marked "(assumption)" right after it.
    - For every topic listed under NO DATA FETCHED, write one sentence saying
      that no data was fetched for that topic, and keep that part qualitative.
    - Professional tone. No filler language.
""").strip()


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
# Main Builder
# ---------------------------------------------------------------------

def build_institutional_report(
    question: str,
    agent_outputs: Dict[str, str],
    tags: List[str],
    tier2_subtags: Dict[str, str],
    ctx_text: str,
    data_tables: List[Dict[str, Any]],
    chart_specs: List[Dict[str, Any]],
    raw_sources: Any,
    key_data: str = "",
    no_data_topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Produce final structured report.
    """
    # Normalize sources
    sources = normalize_sources(raw_sources)
    sources_block = build_sources_block(sources)


    # Compose user prompt
    agent_block = "\n\n".join(
        [f"--- {k.upper()} ---\n{v}" for k, v in agent_outputs.items() if v]
    )

    # List available tables & charts
    tables_block = "AVAILABLE TABLES:\n"
    if data_tables:
        for i, t in enumerate(data_tables, start=1):
            cols = list(getattr(t.get("data"), "columns", []))
            tables_block += f"- Table {i}: {t.get('title','Untitled')} (cols={cols})\n"
    else:
        tables_block += "- (none)\n"

    charts_block = "AVAILABLE CHARTS:\n"
    if chart_specs:
        for i, c in enumerate(chart_specs, start=1):
            charts_block += f"- Chart {i}: {c.get('title','Untitled')} ({c.get('type','line')})\n"
    else:
        charts_block += "- (none)\n"

    no_data_block = "NO DATA FETCHED FOR: " + (", ".join(no_data_topics) if no_data_topics else "(none)")

    user_prompt = "\n\n".join([
        f"USER QUESTION:\n{question}",
        f"TAGS:\n{tags}",
        f"TIER-2 SPECIALIZATION:\n{tier2_subtags}",
        f"CONTEXT:\n{ctx_text or '(none)'}",
        agent_block,
        key_data or "KEY DATA: (none fetched)",
        no_data_block,
        tables_block + charts_block,
        sources_block,
        "Now write the final institutional report.",
    ])

    # Call LLM (LLMError propagates; run_multiagent_pipeline turns it into a friendly message)
    report = call_llm(WRITER_SYSTEM_PROMPT, user_prompt, temperature=WRITER_TEMPERATURE)

    # Extract citations actually used
    used = extract_citations_used(report, sources)

    # Return in app.py format
    return {
        "report_text": report,
        "key_data_tables": data_tables,
        "chart_specs": chart_specs,
        "sources": sources,
        "citations_used": used,
    }


def revise_report(
    question: str,
    draft: str,
    feedback: str,
    key_data: str,
    sources: List[Dict[str, Any]],
) -> str:
    """Second Writer pass: revise the draft using the validator's findings."""
    user_prompt = "\n\n".join([
        f"USER QUESTION:\n{question}",
        key_data or "KEY DATA: (none fetched)",
        build_sources_block(sources),
        f"YOUR DRAFT REPORT:\n{draft}",
        feedback,
        "Return the complete revised report with the same sections. Fix every item above. "
        "Do not add numbers that are not in KEY DATA or a source snippet.",
    ])
    return call_llm(WRITER_SYSTEM_PROMPT, user_prompt, temperature=WRITER_TEMPERATURE)


# ---------------------------------------------------------------------
# Backward-compatible wrapper
# ---------------------------------------------------------------------

def build_answer(
    question: str,
    agent_outputs: Dict[str, str],
    tags: Optional[List[str]] = None,
    tier2_subtags: Optional[Dict[str, str]] = None,
    ctx_text: str = "",
    data_tables: Optional[List[Dict[str, Any]]] = None,
    chart_specs: Optional[List[Dict[str, Any]]] = None,
    sources: Any = None,
    key_data: str = "",
    no_data_topics: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return build_institutional_report(
        question=question,
        agent_outputs=agent_outputs or {},
        tags=tags or [],
        tier2_subtags=tier2_subtags or {},
        ctx_text=ctx_text or "",
        data_tables=data_tables or [],
        chart_specs=chart_specs or [],
        raw_sources=sources,
        key_data=key_data,
        no_data_topics=no_data_topics,
    )
