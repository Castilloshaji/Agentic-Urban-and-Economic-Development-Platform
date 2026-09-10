"""LLM backend for the agents.

Resolved at call time: ANTHROPIC_API_KEY set means the Anthropic API, otherwise
Ollama on the host. Switching backends is an .env change, not a code change.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from ..storage.postgres.load import PROJECT_ROOT

# Step 7 names "claude-sonnet-4-6", which is not a released model id. The current
# Sonnet is claude-sonnet-5; override with ANTHROPIC_MODEL if you want another.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct-q4_K_M"

MAX_TOKENS = 2000
JSON_RETRIES = 2

# Ollama's default context window is small enough that a Supervisor prompt
# (~1.8k tokens of agent output, conflicts and budget context) plus the reserved
# output can overflow it — and an overflow truncates the prompt *silently*, from
# the front, which is where the system rules live. Set it explicitly rather than
# inherit whatever the server defaults to. qwen2.5 supports far more than this.
OLLAMA_NUM_CTX = 8192


@dataclass
class Backend:
    kind: str      # "anthropic" | "ollama"
    model: str
    note: str = ""


def resolve_backend() -> Backend:
    load_dotenv(PROJECT_ROOT / ".env")

    if os.getenv("ANTHROPIC_API_KEY"):
        return Backend("anthropic", os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL))

    configured = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    available = _ollama_models()
    if configured in available:
        return Backend("ollama", configured)
    if available:
        # Falling back is better than failing, but it must be stated: a 3B model
        # and a 7B model do not produce comparable analysis, and a report should
        # never imply it was written by the model the config claims.
        return Backend(
            "ollama", available[0],
            note=(f"configured OLLAMA_MODEL {configured!r} is not pulled; using {available[0]!r}. "
                  f"Run: ollama pull {configured}"),
        )
    raise RuntimeError(
        "No LLM backend available. Either set ANTHROPIC_API_KEY in .env, or start "
        f"Ollama and run: ollama pull {configured}"
    )


def _ollama_models() -> list[str]:
    try:
        import httpx

        host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        response = httpx.get(f"{host}/api/tags", timeout=3.0)
        response.raise_for_status()
        return [m["name"] for m in response.json().get("models", [])]
    except Exception:
        return []


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Small local models wrap JSON in prose or fences far more often than the API
    models do, so this is deliberately forgiving before it gives up.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, depth = text.find("{"), 0
    if start == -1:
        raise ValueError(f"no JSON object in model response: {text[:200]!r}")
    for index in range(start, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            return json.loads(text[start:index + 1])
    raise ValueError(f"unterminated JSON in model response: {text[:200]!r}")


def complete_json(system: str, user: str, backend: Backend | None = None) -> dict:
    """Ask the model for a JSON object and return it parsed."""
    backend = backend or resolve_backend()
    last_error: Exception | None = None

    for attempt in range(JSON_RETRIES + 1):
        prompt = user if attempt == 0 else (
            f"{user}\n\nYour previous reply was not valid JSON ({last_error}). "
            "Reply with the JSON object only — no prose, no code fences."
        )
        try:
            raw = _call(backend, system, prompt)
            return _extract_json(raw)
        except Exception as error:
            last_error = error
            if attempt == JSON_RETRIES:
                raise RuntimeError(
                    f"{backend.kind}/{backend.model} did not return valid JSON after "
                    f"{JSON_RETRIES + 1} attempt(s): {error}"
                ) from error
    raise AssertionError("unreachable")


def _call(backend: Backend, system: str, user: str) -> str:
    if backend.kind == "anthropic":
        import anthropic

        client = anthropic.Anthropic()
        message = client.messages.create(
            model=backend.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in message.content if block.type == "text")

    import ollama

    client = ollama.Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    response = client.chat(
        model=backend.model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        format="json",
        options={"temperature": 0.2, "num_predict": MAX_TOKENS, "num_ctx": OLLAMA_NUM_CTX},
    )
    return response["message"]["content"]
