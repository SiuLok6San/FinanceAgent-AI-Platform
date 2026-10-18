"""
settings.py
-----------
Single place where configuration is loaded.

- Secrets and machine-specific values come from environment variables
  (a local .env file is loaded if present; existing env vars win).
- Rules and tunable parameters (routing keywords, FRED series, thresholds,
  temperatures) live in TOML files under config/, not in the code.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    """Environment variable, treating an empty value as unset."""
    v = os.getenv(key)
    return v.strip() if v and v.strip() else default


@lru_cache(maxsize=None)
def load_config(name: str) -> Dict[str, Any]:
    """Load config/<name>.toml (cached)."""
    with open(CONFIG_DIR / f"{name}.toml", "rb") as f:
        return tomllib.load(f)
