"""
render.py
---------
Turns a report template written with fact placeholders into the final text.

    {{HY_OAS.latest}}          -> "3.23% [S1]"   (value and citation from the fact store)
    {{assume: 10-15%}}         -> "10-15% (assumption)"
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


@dataclass
class Ref:
    start: int                 # span of the inserted value in the rendered text (citation excluded)
    end: int
    kind: str                  # "fact" | "assumption" | "unknown"
    fact_id: Optional[str]
    fact: Optional[FactEntry] = None


def render(template: str, store: Dict[str, FactEntry], sources: List[Dict[str, Any]]) -> Tuple[str, List[Ref]]:
    url_to_id = {s["url"]: s["id"] for s in sources if s.get("url")}
    out: List[str] = []
    refs: List[Ref] = []
    pos = 0          # length of rendered text so far
    last = 0         # position in template
    cited_on_line: set = set()

    for m in _PLACEHOLDER.finditer(template):
        literal = template[last:m.start()]
        if "\n" in literal:
            cited_on_line = set()
        out.append(literal)
        pos += len(literal)
        last = m.end()

        token = (m.group(1) or m.group(2) or "").strip()
        if m.group(2) and token not in store:      # stray single braces, not a fact: leave as written
            out.append(m.group(0))
            pos += len(m.group(0))
            continue

        assume = _ASSUME.match(token)
        if assume:
            value = assume.group(1).strip()
            refs.append(Ref(pos, pos + len(value), "assumption", None))
            text = f"{value} (assumption)"
        elif token in store:
            fact = store[token]
            refs.append(Ref(pos, pos + len(fact.display), "fact", token, fact))
            new_ids = [url_to_id[u] for u in fact.source_urls if u in url_to_id and url_to_id[u] not in cited_on_line]
            cited_on_line.update(new_ids)
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
        if not _ASSUME.match(token):
            ids.append(token)
    return ids
