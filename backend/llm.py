"""
LLM provider layer. One function: complete_json(system, user, schema_model) -> validated model | None.

Providers (LLM_PROVIDER):
  * openrouter - OpenRouter chat-completions API (OPENROUTER_API_KEY, OPENROUTER_MODEL)
  * anthropic  - Anthropic SDK structured outputs (ANTHROPIC_API_KEY, CLAUDE_MODEL)
Every response is validated against the Pydantic schema; any failure returns None so
callers fall back to the deterministic engine. Keys are never logged or returned.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional, Type, TypeVar

import requests
from pydantic import BaseModel, ValidationError

log = logging.getLogger("llm")
T = TypeVar("T", bound=BaseModel)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def provider() -> Optional[str]:
    if os.getenv("USE_LLM", "auto").lower() == "off":
        return None
    p = os.getenv("LLM_PROVIDER", "").lower()
    if p == "openrouter" and os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    if p in ("anthropic", "") and (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return "anthropic"
    if os.getenv("OPENROUTER_API_KEY"):
        return "openrouter"
    return None


def model_name() -> Optional[str]:
    p = provider()
    if p == "openrouter":
        return os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-5.5")
    if p == "anthropic":
        return os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    return None


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _openrouter(system: str, user: str, schema: Type[T], max_tokens: int, timeout: float) -> Optional[T]:
    js = schema.model_json_schema()
    sys_prompt = (f"{system}\n\nRespond with ONLY a single JSON object (no prose, no code fences) that "
                  f"validates against this JSON Schema:\n{json.dumps(js)}")
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
               "Content-Type": "application/json", "X-Title": "CrawlPilot"}
    base = {"model": model_name(), "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]}
    attempts = [
        {**base, "response_format": {"type": "json_schema",
                                     "json_schema": {"name": schema.__name__, "strict": False, "schema": js}}},
        base,  # retry without response_format if the route doesn't support it
    ]
    for body in attempts:
        try:
            r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            log.warning("openrouter: network error (%s)", type(e).__name__)
            return None
        if r.status_code != 200:
            log.warning("openrouter: HTTP %s %s", r.status_code, r.text[:200].replace(os.environ["OPENROUTER_API_KEY"], "***"))
            if r.status_code in (400, 404, 422):
                continue
            return None
        try:
            choice = r.json()["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, ValueError):
            continue
        data = _extract_json(content)
        if data is None:
            log.warning("openrouter: response was not JSON")
            continue
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            log.warning("openrouter: schema validation failed (%s errors)", e.error_count())
            continue
    return None


_anthropic_client = None


def _anthropic(system: str, user: str, schema: Type[T], max_tokens: int, timeout: float) -> Optional[T]:
    global _anthropic_client
    import anthropic
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(timeout=timeout, max_retries=1)
    try:
        resp = _anthropic_client.messages.parse(
            model=model_name(), max_tokens=max_tokens, system=system,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic: call failed (%s)", type(e).__name__)
        return None
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


def complete_json(system: str, user: str, schema: Type[T], max_tokens: int = 4000,
                  timeout: float = 60.0) -> Optional[T]:
    p = provider()
    if p == "openrouter":
        return _openrouter(system, user, schema, max_tokens, timeout)
    if p == "anthropic":
        return _anthropic(system, user, schema, max_tokens, timeout)
    return None
