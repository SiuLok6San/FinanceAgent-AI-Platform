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

from settings import ROOT
from validators import annotate_report, validate_report

DEMO_DIR = ROOT / "eval" / "results" / "revised"

_spec = importlib.util.spec_from_file_location("eval_common", ROOT / "eval" / "common.py")
_common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_common)


def _read(qid: str) -> Dict[str, Any]:
    return json.loads((DEMO_DIR / f"{qid}.json").read_text())


@lru_cache(maxsize=None)
def demo_runs() -> List[Dict[str, Any]]:
    """[{"id", "category", "question", "ticker", "run_utc"}] for every saved run without a pipeline error."""
    runs = []
    for path in sorted(DEMO_DIR.glob("Q*.json")):
        rec = json.loads(path.read_text())
        if not rec["error"]:
            runs.append({k: rec[k] for k in ("id", "category", "question", "ticker", "run_utc")})
    return runs


@lru_cache(maxsize=None)
def load_demo_result(qid: str) -> Dict[str, Any]:
    rec = _read(qid)
    tables = [_common.table_from_json(t) for t in rec["tables"]]
    today = datetime.fromisoformat(rec["run_utc"]).astimezone(timezone.utc).date()

    def check(text: str) -> Dict[str, Any]:
        return validate_report(text, rec["question"], tables, rec["sources"], rec["router_tags"],
                               data_status=rec.get("data_status"), today=today)

    validation = check(rec["report_text"])
    result = {
        "error": False,
        "report_text_raw": rec["report_text"],
        "report_text": annotate_report(rec["report_text"], validation),
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
    if rec.get("draft_report_text") and rounds:
        # keep/reject was decided during the run; the open-issue counts are recomputed so they
        # match the current validator, like everything else on the page
        draft_validation = check(rec["draft_report_text"])
        r0 = dict(rounds[0])
        r0["open_issues_before"] = draft_validation["open_issues"]
        if r0.get("accepted"):
            r0["open_issues_after"] = validation["open_issues"]
        elif r0.get("revised_report_text"):
            r0["open_issues_after"] = check(r0["revised_report_text"])["open_issues"]
        result.update(draft_report_text=rec["draft_report_text"], draft_validation=draft_validation,
                      repair={"enabled": True, "rounds": [r0]})
    return result
