"""
Shared helpers for the evaluation scripts.

- PIPELINES: which code each evaluated pipeline runs
    original  tag v1.0-course     (Dec 2025 course version)
    v2        tag v2.0-validation (Oct 2026: validation layer + feedback loop)
    v3        the working tree     (fact placeholders)
- extract_code(): checks a commit's files out into eval/.versions/<commit>/
  (gitignored) with `git archive`, so old versions really run their old code
- question sets: dev (questions.csv, also used while developing), heldout
  (questions_heldout.csv, frozen before v3 was built; H05 and H10 were once
  used in a manual Planner test) and fresh (questions_fresh.csv, frozen
  later and never shown to any pipeline during development)
- table (de)serialization for saving pipeline outputs as JSON
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO / "eval"
RESULTS_DIR = EVAL_DIR / "results"
VERSIONS_DIR = EVAL_DIR / ".versions"
ORIGINAL_COMMIT = "0468eea"  # tag v1.0-course: Feb 2026 upload of the Dec 2025 course code
V2_COMMIT = "faf19bc"        # tag v2.0-validation

# pipeline -> (commit to run, or None for the working tree; inject the current llm.py?)
PIPELINES = {
    "original": (ORIGINAL_COMMIT, True),   # its llm.py cannot call the hosted API
    "v2": (V2_COMMIT, False),
    "v3": (None, False),
}
QUESTION_FILES = {"dev": "questions.csv", "heldout": "questions_heldout.csv", "fresh": "questions_fresh.csv"}


def load_questions(which: str = "dev") -> List[Dict[str, str]]:
    """Questions of one set ("dev", "heldout") or both ("all"), each tagged with its set."""
    sets = list(QUESTION_FILES) if which == "all" else [which]
    rows = []
    for name in sets:
        with open(EVAL_DIR / QUESTION_FILES[name], newline="") as f:
            for r in csv.DictReader(f):
                r["expected_tags"] = [t for t in r["expected_tags"].split(";") if t]
                r["ticker"] = r["ticker"].strip() or None
                r["set"] = name
                rows.append(r)
    return rows


def git_commit() -> str:
    """Short HEAD hash, marked "+uncommitted" if any code or config file has local changes."""
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", "*.py", "config/", "requirements*.txt"],
                           cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip() + ("+uncommitted" if dirty.stdout.strip() else "")


def extract_code(commit: str) -> Path:
    """Write a commit's files to eval/.versions/<commit>/ (idempotent)."""
    target = VERSIONS_DIR / commit
    marker = target / ".commit"
    if marker.exists() and marker.read_text() == commit:
        return target
    archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=REPO, capture_output=True, check=True).stdout
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    marker.write_text(commit)
    return target


def extract_original_code() -> Path:
    return extract_code(ORIGINAL_COMMIT)


# ---------------------------------------------------------------------------
# JSON (de)serialization of pipeline tables
# ---------------------------------------------------------------------------

def _jsonable(v: Any) -> Any:
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if hasattr(v, "item"):  # numpy scalars
        return v.item()
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def table_to_json(t: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: _jsonable(v) for k, v in t.items() if k != "data" and not isinstance(v, pd.DataFrame)}
    df = t.get("data")
    if isinstance(df, pd.DataFrame):
        out["columns"] = [str(c) for c in df.columns]
        out["rows"] = [[_jsonable(v) for v in row] for row in df.itertuples(index=False)]
    else:
        out["columns"], out["rows"] = [], []
    return out


def table_from_json(t: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in t.items() if k not in ("columns", "rows")}
    df = pd.DataFrame(t["rows"], columns=t["columns"])
    if "date" in df.columns and t.get("kind") == "timeseries":
        df["date"] = pd.to_datetime(df["date"])
    out["data"] = df
    return out


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=_jsonable))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())
