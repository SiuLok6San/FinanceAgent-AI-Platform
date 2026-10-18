"""
render.py
---------
Turns a report template written with fact placeholders into the final text.

    {{HY_OAS.latest}}          -> "3.23% [S1]"   (value and citation from the fact store)
    {{assume: 10-15%}}         -> "10-15% (assumption)"
    {{S5: 2.9%}}               -> "2.9% [S5]"     (a number quoted from web/news source S5;
                                                   the validator checks that S5's text contains it)
    {{NOT_A_FACT}}             -> "[unknown fact: NOT_A_FACT]"  (kept visible, flagged by the validator)

A source is cited once per line: the first fact from S1 on a line gets
"[S1]", later facts from S1 on the same line do not repeat it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fact_store import FactEntry

# {{ID}}, {{ assume: text }}; a single-brace {ID} is accepted only if ID is a known fact
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}|\{\s*([A-Z][A-Za-z0-9_]*(?:\.[a-z0-9_]+)?)\s*\}")
_ASSUME = re.compile(r"^(?:assume|assumption)\s*:\s*(.+)$", re.I | re.S)
_QUOTE = re.compile(r"^\[?(S\d+)\]?\s*:\s*(.+)$", re.S)

# Writers often add the unit or sign around a placeholder ("{{X.latest}} %", "+{{X.chg_1m}} bp")
# although the display already has it; these duplicates are dropped.
_UNIT_AFTER = [
    ("%", re.compile(r"\s?(?:%|percent\b)", re.I)),
    (" bp", re.compile(r"\s?(?:bps?\b|basis points?\b)", re.I)),
    (" pp", re.compile(r"\s?(?:pp\b|ppts?\b|percentage points?\b)", re.I)),
    ("x", re.compile(r"\s?(?:x\b|times\b)", re.I)),
    (" pts", re.compile(r"\s?(?:pts?\b|points?\b)", re.I)),
    ("percentile", re.compile(r"\s?percentile\b", re.I)),
]
_CITATION_NEXT = re.compile(r"\s*[\[\u3010(]\s*S\d+")  # the writer cited it themselves right after
_SIGN_BEFORE = re.compile(r"(?:(?<=\s)|(?<=\()|^)[+\-\u2212]\s?$")


def _drop_duplicate_sign(literal: str, display: str) -> str:
    if display[:1] in "+\u2212" and _SIGN_BEFORE.search(literal):
        return _SIGN_BEFORE.sub("", literal)
    if display.startswith("$") and literal.endswith("$"):
        return literal[:-1]
    return literal


def _duplicate_unit_len(after: str, display: str) -> int:
    for ending, pattern in _UNIT_AFTER:
        if display.endswith(ending):
            m = pattern.match(after)
            return m.end() if m else 0
    return 0


@dataclass
class Ref:
    start: int                 # span of the inserted value in the rendered text (citation excluded)
    end: int
    kind: str                  # "fact" | "assumption" | "quote" | "unknown"
    fact_id: Optional[str]
    fact: Optional[FactEntry] = None
    source_id: Optional[str] = None   # for quotes: the source the number is taken from


def render(template: str, store: Dict[str, FactEntry], sources: List[Dict[str, Any]]) -> Tuple[str, List[Ref]]:
    url_to_id = {s["url"]: s["id"] for s in sources if s.get("url")}
    out: List[str] = []
    refs: List[Ref] = []
    pos = 0          # length of rendered text so far
    last = 0         # position in template
    cited_on_line: set = set()

    for m in _PLACEHOLDER.finditer(template):
        if m.start() < last:   # inside a unit we already skipped
            continue
        literal = template[last:m.start()]
        if "\n" in literal:
            cited_on_line = set()
        token = (m.group(1) or m.group(2) or "").strip()
        if token in store:
            literal = _drop_duplicate_sign(literal, store[token].display)
        out.append(literal)
        pos += len(literal)
        last = m.end()
        if token in store:
            last += _duplicate_unit_len(template[last:], store[token].display)

        if m.group(2) and token not in store:      # stray single braces, not a fact: leave as written
            out.append(m.group(0))
            pos += len(m.group(0))
            continue

        assume = _ASSUME.match(token)
        quote = _QUOTE.match(token)
        if quote and quote.group(1) in url_to_id.values():
            sid, value = quote.group(1), quote.group(2).strip()
            refs.append(Ref(pos, pos + len(value), "quote", None, None, sid))
            text = value + ("" if sid in cited_on_line or _CITATION_NEXT.match(template, last) else f" [{sid}]")
            cited_on_line.add(sid)
        elif assume:
            value = assume.group(1).strip()
            refs.append(Ref(pos, pos + len(value), "assumption", None))
            text = f"{value} (assumption)"
        elif token in store:
            fact = store[token]
            refs.append(Ref(pos, pos + len(fact.display), "fact", token, fact))
            new_ids = [url_to_id[u] for u in fact.source_urls if u in url_to_id and url_to_id[u] not in cited_on_line]
            cited_on_line.update(new_ids)
            if _CITATION_NEXT.match(template, last):
                new_ids = []  # keep the writer's own marker instead of adding a second one
            text = fact.display + (f" [{', '.join(new_ids)}]" if new_ids else "")
        else:
            text = f"[unknown fact: {token}]"
            refs.append(Ref(pos, pos + len(text), "unknown", token))
        out.append(text)
        pos += len(text)

    out.append(template[last:])
    return "".join(out), refs


def placeholder_ids(template: str) -> List[str]:
    """IDs referenced in a template (assumptions excluded)."""
    ids = []
    for m in _PLACEHOLDER.finditer(template):
        token = (m.group(1) or m.group(2) or "").strip()
        if not _ASSUME.match(token) and not _QUOTE.match(token):
            ids.append(token)
    return ids
