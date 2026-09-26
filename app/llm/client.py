"""Thin OpenAI-compatible chat-completions client over httpx (no SDKs: keeps cold starts small)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

_client: Optional[httpx.AsyncClient] = None
_client_loop: Any = None


def _get_client() -> httpx.AsyncClient:
    """One pooled client per event loop (a client must not outlive the loop it was created on)."""
    import asyncio
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        _client_loop = loop
    return _client


class LLMError(Exception):
    """kind: rate_limit | server | timeout | auth | bad_request | bad_output | network"""

    def __init__(self, kind: str, message: str = "", retry_after: Optional[float] = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.retry_after = retry_after


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    headers: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def chat_completion(*, base_url: str, api_key: str, model: str, messages: list[dict], max_tokens: int,
                          seed: int, timeout_s: float, schema: Optional[dict] = None, json_mode: str = "schema",
                          reasoning_effort: Optional[str] = None) -> LLMResult:
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens, "seed": seed}
    if schema is not None:
        if json_mode == "schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "strict": True, "schema": schema}}
        elif json_mode == "object":
            body["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    try:
        r = await _get_client().post(
            base_url.rstrip("/") + "/chat/completions", json=body, timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    except httpx.TimeoutException as e:
        raise LLMError("timeout", str(e)) from e
    except httpx.HTTPError as e:
        raise LLMError("network", str(e)) from e

    hdrs = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "retry-after"))}
    if r.status_code == 429:
        raise LLMError("rate_limit", r.text[:200], retry_after=_f(hdrs.get("retry-after")))
    if r.status_code in (401, 403):
        raise LLMError("auth", f"HTTP {r.status_code}")
    if r.status_code >= 500:
        raise LLMError("server", f"HTTP {r.status_code}")
    if r.status_code == 400 and ("tool_use_failed" in r.text or "json_validate_failed" in r.text):
        # gpt-oss sometimes emits a tool call / an unparsable JSON for a structured request: a generation glitch, not a bad request
        raise LLMError("bad_output", f"provider could not produce the structured output: {r.text[:120]}")
    if r.status_code >= 400:
        raise LLMError("bad_request", f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
        if not text.strip() and choice.get("finish_reason") == "length":
            raise LLMError("bad_output", "completion truncated: reasoning used the whole token budget")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError("bad_output", "unparseable completion envelope") from e
    usage = data.get("usage") or {}
    return LLMResult(text=text, prompt_tokens=int(usage.get("prompt_tokens") or 0),
                     completion_tokens=int(usage.get("completion_tokens") or 0), model=model, headers=hdrs)


def now_ts() -> float:
    return time.time()
