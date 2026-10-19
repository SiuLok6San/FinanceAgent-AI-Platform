"""
demo.py
-------
Saved runs from the evaluation, shown on the public site without calling any API.

Each record in eval/results/revised/ is turned back into the result dict that
run_multiagent_pipeline returns and re-validated with the current validator
(as eval/score.py does), so the page shows the same numbers as eval/RESULTS.md.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List

from fact_store import build_fact_store
from render import render
from settings import ROOT
from validators import annotate_report, validate_report

# saved runs of the current pipeline (falls back to the older free-text runs if none exist)
DEMO_DIR = next((d for d in (ROOT / "eval" / "results" / "v3", ROOT / "eval" / "results" / "v2",
                             ROOT / "eval" / "results" / "revised") if any(d.glob("Q*.json"))),
                ROOT / "eval" / "results" / "v3")

_spec = importlib.util.spec_from_file_location("eval_common", ROOT / "eval" / "common.py")
_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_common)


def _read(qid: str) -> Dict[str, Any]:
    return json.loads((DEMO_DIR / f"{qid}.json").read_text())


@lru_cache(maxsize=None)
def demo_runs() -> List[Dict[str, Any]]:
    """[{"id", "category", "question", "ticker", "run_utc"}] for every saved run without a pipeline error:
    development questions (Q), then fresh (F) and held-out (H) ones."""
    runs = []
    for path in [p for prefix in "QFH" for p in sorted(DEMO_DIR.glob(f"{prefix}[0-9]*.json"))]:
        rec = json.loads(path.read_text())
        if not rec["error"]:
            runs.append({k: rec[k] for k in ("id", "category", "question", "ticker", "run_utc")})
    return runs


@lru_cache(maxsize=None)
def load_demo_result(qid: str) -> Dict[str, Any]:
    rec = _read(qid)
    tables = [_common.table_from_json(t) for t in rec["tables"]]
    today = datetime.fromisoformat(rec["run_utc"]).astimezone(timezone.utc).date()

    tags = rec.get("tags") or rec["router_tags"]
    store = build_fact_store(tables) if rec.get("report_template") else None

    def refs_json(refs):
        return [{"start": r.start, "end": r.end, "kind": r.kind, "fact_id": r.fact_id, "source_id": r.source_id}
                for r in refs]

    def check(text: str) -> Dict[str, Any]:
        """Validate a free-text report, or render + validate a placeholder template."""
        if store is None:
            return {"text": text, "refs": None,
                    "validation": validate_report(text, rec["question"], tables, rec["sources"], tags,
                                                  data_status=rec.get("data_status"), today=today)}
        rendered, refs = render(text, store, rec["sources"])
        return {"text": rendered, "refs": refs_json(refs),
                "validation": validate_report(rendered, rec["question"], tables, rec["sources"], tags,
                                              data_status=rec.get("data_status"), today=today, refs=refs)}

    final = check(rec.get("report_template") or rec["report_text"])
    validation = final["validation"]
    result = {
        "error": False,
        "report_text_raw": final["text"],
        "report_text": annotate_report(final["text"], validation),
        "refs": final["refs"],
        "facts": {k: {"display": e.display, "label": e.label, "kind": e.kind} for k, e in store.items()} if store else None,
        "plan": rec.get("plan"),
        "validation": validation,
        "key_data_tables": tables,
        "chart_specs": rec["charts"],
        "sources": rec["sources"],
        "tags": rec["router_tags"],
        "data_status": rec.get("data_status") or [],
        "run_meta": rec["run_meta"],
        "run_utc": rec["run_utc"],
        "question": rec["question"],
        "ticker": rec["ticker"],
    }

    rounds = (rec.get("repair") or {}).get("rounds") or []
    draft_src = rec.get("draft_report_template") or rec.get("draft_report_text")
    if draft_src and rounds:
        # keep/reject was decided during the run; the open-issue counts are recomputed so they
        # match the current validator, like everything else on the page
        draft = check(draft_src)
        r0 = dict(rounds[0])
        r0["open_issues_before"] = draft["validation"]["open_issues"]
        if r0.get("accepted"):
            r0["open_issues_after"] = validation["open_issues"]
        elif r0.get("revised_report_text"):
            r0["open_issues_after"] = check(r0["revised_report_text"])["validation"]["open_issues"]
        result.update(draft_report_text=draft["text"], draft_validation=draft["validation"], draft_refs=draft["refs"],
                      repair={"enabled": True, "rounds": [r0]})
    return result
