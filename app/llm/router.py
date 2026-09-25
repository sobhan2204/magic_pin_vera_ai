"""Quota-aware model routing: primary -> secondary -> optional alt provider -> (caller uses template fallback).

Rules: never call a model we can't afford (atomic Redis reservation), never hit a 429 on purpose, cool a failing
model for 60 s, and give up quickly when the caller's deadline is near.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..config import Settings
from ..store.base import Store
from . import client as llm_client
from .client import LLMError, LLMResult

log = logging.getLogger("vera.llm")
COOL_S = 60
SAFETY = 0.9          # keep 10% headroom under every configured limit


@dataclass(frozen=True)
class Target:
    name: str
    base_url: str
    api_key: str
    model: str
    json_mode: str        # "schema" (Groq structured outputs) | "object"
    reasoning_effort: Optional[str]
    limits: Optional[dict] = None   # provider-specific free-tier limits; None = the global MODEL_* limits


@dataclass
class Lease:
    target: Target
    est_tokens: int


def build_targets(s: Settings) -> list[Target]:
    out: list[Target] = []
    if s.groq_api_key:
        for m in (s.primary_model, s.secondary_model):
            if m and all(t.model != m for t in out):
                out.append(Target("groq", s.groq_base_url, s.groq_api_key, m, "schema",
                                  "low" if "gpt-oss" in m else None))
    if s.alt_base_url and s.alt_api_key and s.alt_model:
        out.append(Target(s.alt_name or "alt", s.alt_base_url, s.alt_api_key, s.alt_model, s.alt_json_mode,
                          "low" if "gpt-oss" in s.alt_model else None, dict(s.alt_limits)))
    return out


def estimate_tokens(messages: list[dict], max_tokens: int) -> int:
    chars = sum(len(m.get("content", "")) for m in messages)
    return int(chars / 3.5) + max_tokens


ChatFn = Callable[..., Awaitable[LLMResult]]


class Router:
    def __init__(self, store: Store, settings: Settings, chat: Optional[ChatFn] = None) -> None:
        self.store, self.s = store, settings
        self.chat = chat or llm_client.chat_completion
        self.targets = build_targets(settings)
        self.limits = self._margin({"rpm": settings.model_rpm, "tpm": settings.model_tpm,
                                    "rpd": settings.model_rpd, "tpd": settings.model_tpd})

    @staticmethod
    def _margin(raw: dict) -> dict:
        return {k: int(v * SAFETY) for k, v in raw.items()}

    def limits_for(self, t: "Target") -> dict:
        return self._margin(t.limits) if t.limits else self.limits

    @property
    def enabled(self) -> bool:
        return self.s.llm_mode == "live" and bool(self.targets)

    async def acquire(self, est_tokens: int, skip: frozenset[str] = frozenset()) -> Optional[Lease]:
        """Reserve quota on the first usable model. Skips (without calling) any model that is cooling or out of quota."""
        for t in self.targets:
            if t.model in skip:
                continue
            try:
                res = await self.store.quota_reserve(t.model, est_tokens, self.limits_for(t), time.time())
            except Exception:
                log.exception("quota reservation failed for %s", t.model)
                continue
            if res == "ok":
                return Lease(t, est_tokens)
            log.info("skip model=%s reason=%s", t.model, res)
        return None

    async def acquire_many(self, est_tokens: int, n: int) -> list[Lease]:
        """Reserve up to n leases with ONE atomic call per model (primary first, then the next model for the remainder).
        Same result as n sequential acquire() calls, but O(models) round-trips instead of O(n)."""
        leases: list[Lease] = []
        for t in self.targets:
            if len(leases) >= n:
                break
            try:
                got = await self.store.quota_reserve_n(t.model, est_tokens, n - len(leases), self.limits_for(t), time.time())
            except Exception:
                log.exception("bulk quota reservation failed for %s", t.model)
                continue
            leases += [Lease(t, est_tokens) for _ in range(got)]
        return leases

    async def release(self, lease: Lease) -> None:
        """Return an unused reservation."""
        try:
            await self.store.quota_adjust(lease.target.model, -lease.est_tokens, time.time(), delta_requests=-1)
        except Exception:
            log.exception("quota release failed")

    async def run(self, lease: Optional[Lease], messages: list[dict], *, max_tokens: int,
                  validate: Callable[[str], Any], deadline_at: float, schema: Optional[dict] = None,
                  ) -> Optional[tuple[Any, LLMResult]]:
        """Call the leased model; on 429/5xx/timeout/invalid output cool it and fail over. None => use fallback."""
        tried: set[str] = set()
        est = lease.est_tokens if lease else estimate_tokens(messages, max_tokens)
        while lease is not None:
            t = lease.target
            remaining = deadline_at - time.monotonic()
            if remaining < 1.0:
                await self.release(lease)
                return None
            timeout = min(float(self.s.llm_call_timeout_s), remaining)
            failure: Optional[str] = None
            try:
                res = await self.chat(base_url=t.base_url, api_key=t.api_key, model=t.model, messages=messages,
                                      max_tokens=max_tokens, seed=self.s.llm_seed, timeout_s=timeout, schema=schema,
                                      json_mode=t.json_mode, reasoning_effort=t.reasoning_effort)
                actual = res.total_tokens or est
                await self.store.quota_adjust(t.model, actual - lease.est_tokens, time.time())
                try:
                    return validate(res.text), res
                except (ValueError, KeyError, TypeError) as e:
                    failure = f"bad_output: {e}"
            except LLMError as e:
                failure = e.kind
                # the provider did not process it: refund tokens, keep the request counted
                await self.store.quota_adjust(t.model, -lease.est_tokens, time.time())
                if e.kind == "rate_limit" and e.retry_after and e.retry_after > COOL_S:
                    await self.store.quota_cool(t.model, int(min(e.retry_after, 300)))
                    failure = None
                    tried.add(t.model)
                    lease = await self.acquire(est, frozenset(tried))
                    continue
            except Exception as e:  # never let an LLM problem escape into a request
                failure = f"unexpected {type(e).__name__}"
                await self.store.quota_adjust(t.model, -lease.est_tokens, time.time())
            log.warning("llm failure model=%s %s; cooling %ss and failing over", t.model, failure, COOL_S)
            await self.store.quota_cool(t.model, COOL_S)
            tried.add(t.model)
            lease = await self.acquire(est, frozenset(tried))
        return None

    async def state(self) -> dict:
        out = {}
        for t in self.targets:
            try:
                out[t.model] = await self.store.quota_state(t.model, time.time())
            except Exception:
                out[t.model] = {"error": "unavailable"}
        return {"limits_with_margin": self.limits, "per_model_limits": {t.model: self.limits_for(t) for t in self.targets},
                "models": out, "llm_mode": self.s.llm_mode}
