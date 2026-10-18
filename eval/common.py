"""
Shared helpers for the evaluation scripts.

- ORIGINAL_COMMIT: the Dec 2025 course version, used as the "before" pipeline
- extract_original_code(): checks that commit's files out into eval/.original/
  (gitignored) with `git archive`, so the baseline really runs the old code
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
ORIGINAL_DIR = EVAL_DIR / ".original"
ORIGINAL_COMMIT = "0468eea"  # "Add files via upload", Feb 2026 upload of the Dec 2025 course code


def load_questions() -> List[Dict[str, str]]:
    with open(EVAL_DIR / "questions.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["expected_tags"] = [t for t in r["expected_tags"].split(";") if t]
        r["ticker"] = r["ticker"].strip() or None
    return rows


def git_commit() -> str:
    """Short HEAD hash, marked "+uncommitted" if any code or config file has local changes."""
    out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", "*.py", "config/", "requirements*.txt"],
                           cwd=REPO, capture_output=True, text=True)
    return out.stdout.strip() + ("+uncommitted" if dirty.stdout.strip() else "")


def extract_original_code() -> Path:
    """Write the original commit's files to eval/.original/ (idempotent)."""
    marker = ORIGINAL_DIR / ".commit"
    if marker.exists() and marker.read_text() == ORIGINAL_COMMIT:
        return ORIGINAL_DIR
    archive = subprocess.run(["git", "archive", "--format=tar", ORIGINAL_COMMIT], cwd=REPO, capture_output=True, check=True).stdout
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(ORIGINAL_DIR, filter="data")
    marker.write_text(ORIGINAL_COMMIT)
    return ORIGINAL_DIR


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
