"""
llm.py — Native Ollama Chat Caller
"""

import os
import json
import time
import requests
from typing import Any, Dict, Optional, Union

from dotenv import load_dotenv

# Load .env (if present) before reading settings; existing env vars win.
load_dotenv()

# Environment variables
def _env(k, d=""):
    v = os.getenv(k)
    return v if v else d

LLM_API_BASE = _env("LLM_API_BASE", "http://localhost:11434")
LLM_MODEL_NAME = _env("LLM_MODEL_NAME", "llama3")
LLM_TIMEOUT_SEC = float(_env("LLM_TIMEOUT_SEC", "120"))
# Fixed seed so repeated runs with the same inputs give the same output.
LLM_SEED = int(_env("LLM_SEED", "42"))


def llm_settings() -> Dict[str, Any]:
    """Settings that are logged with every report for reproducibility."""
    return {
        "model": LLM_MODEL_NAME,
        "api_base": LLM_API_BASE,
        "seed": LLM_SEED,
        "timeout_sec": LLM_TIMEOUT_SEC,
    }

class LLMError(RuntimeError):
    pass


def call_llm(
    system_prompt: Union[str, None],
    user_prompt: Optional[str] = None,
    *,
    temperature: float = 0.2,
    model: Optional[str] = None,
    retries: int = 2,
) -> str:
    """
    Call Ollama's native /api/chat endpoint.
    Supports:
    - call_llm(prompt)
    - call_llm(system_prompt, user_prompt)
    """

    # Backward compatibility: call_llm(prompt)
    if user_prompt is None:
        user_prompt = system_prompt
        system_prompt = "You are a professional institutional finance analyst."

    model = model or LLM_MODEL_NAME

    url = f"{LLM_API_BASE}/api/chat"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": temperature,
            "seed": LLM_SEED,
        },
        "stream": False
    }

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                url,
                json=payload,
                timeout=LLM_TIMEOUT_SEC
            )
            if r.status_code != 200:
                raise LLMError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")

            data = r.json()
            msg = data.get("message", {}).get("content", "")

            if not msg:
                raise LLMError(f"Ollama returned empty message: {data}")

            return msg.strip()

        except Exception as e:
            last_err = e
            time.sleep(0.5)

    raise LLMError(
        f"[LLM ERROR] Could not connect to Ollama.\n"
        f"Base: {LLM_API_BASE}\n"
        f"Detail: {last_err}"
    )
