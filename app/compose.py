"""Orchestrates Resolve -> Write (LLM) -> Verify -> Repair (1x) -> Fallback template for one decision."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import PROMPT_VERSION, Settings
from .fallback import N_VARIANTS, compose_fallback
from .humanize import strip_end, words
from .llm.mock import mock_write
from .llm.router import Lease, Router
from .models import FactSheet
from .normalize import Trigger
from .playbooks import Playbook, get_playbook
from .store.base import Store
from .verifier import verify, verify_rationale
from .writer import write_with_llm

log = logging.getLogger("vera.compose")
DRAFT_TTL_S = 2 * 24 * 3600


@dataclass
class Composed:
    body: str
    cta: str
    facts_used: list[str]
    rationale: str
    template_params: list[str]
    via: str                       # "llm" | "mock" | "fallback" | "cache:<via>"
    violations: list[str]


@dataclass
class ComposeEnv:
    store: Store
    settings: Settings
    router: Router
    deadline_at: float                                   # time.monotonic() value after which we stop waiting for the LLM
    lease: Optional[Lease] = None                        # quota pre-reserved (in score order) by the tick
    cache_key: str = ""
    allow_llm: bool = True
    llm_out: Optional[dict] = None                       # draft from a batch call (verified against recents here)
    cached: Optional[dict] = None                        # prefetched draft (None = miss)
    prefetched: bool = False


def draft_cache_key(t: Trigger, versions: dict, tick_date: str, hook_key: str = "hook") -> str:
    raw = "|".join([f"{t.id}:{t.version}", f"m{versions.get('m')}", f"c{versions.get('cat')}", f"u{versions.get('cust', '-')}",
                    tick_date, PROMPT_VERSION, hook_key])
    return "draft:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def build_rationale(t: Trigger, pb: Playbook, fs: FactSheet, cta: str) -> str:
    hook = fs.text("hook") or ""
    why = pb.why_now or "new information arrived"
    return (f"{words(t.kind)} trigger ({why}; urgency {t.urgency}/5); leading with “{strip_end(hook)}”; "
            f"objective: {pb.objective}; CTA: {cta}.")


def _finish(t: Trigger, pb: Playbook, fs: FactSheet, out: dict, via: str, violations: list[str]) -> Composed:
    rationale = build_rationale(t, pb, fs, out["cta"])
    if verify_rationale(rationale, out["body"], fs, words(t.kind)):
        rationale = f"{words(t.kind)} trigger; objective: {pb.objective}; CTA: {out['cta']}."
    hook = fs.text("hook") or ""
    body = out["body"]
    ask = body.rsplit(". ", 1)[-1] if ". " in body else body
    return Composed(body=body, cta=out["cta"], facts_used=list(out.get("facts_used") or []), rationale=rationale,
                    template_params=[fs.salutation, strip_end(hook), ask], via=via, violations=violations)


def compose_template(t: Trigger, fs: FactSheet, recent_bodies: list[str]) -> Composed:
    """Deterministic, always-valid composer (no LLM)."""
    pb = get_playbook(t.kind)
    best, best_v = None, None
    for variant, minimal in [(None, False)] + [(i, False) for i in range(N_VARIANTS)] + [(None, True)]:
        out = compose_fallback(fs, pb, t.id, variant=variant, minimal=minimal)
        v = verify(out, fs, recent_bodies)
        if best is None or len(v) < len(best_v or []):
            best, best_v = out, v
        if not v:
            break
    assert best is not None
    return _finish(t, pb, fs, best, "fallback", best_v or [])


async def compose_message(env: ComposeEnv, t: Trigger, fs: FactSheet, recent_bodies: list[str]) -> Composed:
    pb = get_playbook(t.kind)
    s = env.settings

    if env.cache_key:                                                         # determinism: same inputs -> same draft
        try:
            cached = env.cached if env.prefetched else await env.store.get_json(env.cache_key)
        except Exception:
            cached = None
        if cached and not verify(cached["out"], fs, recent_bodies):
            if env.lease:
                await env.router.release(env.lease)
            return _finish(t, pb, fs, cached["out"], f"cache:{cached['via']}", [])

    out, via = None, ""
    if env.llm_out is not None:
        if not verify(env.llm_out, fs, recent_bodies):
            out, via = env.llm_out, "llm"
    elif env.allow_llm and s.llm_mode == "mock":
        cand = mock_write(fs, pb, t.id)
        if not verify(cand, fs, recent_bodies):
            out, via = cand, "mock"
    elif env.allow_llm and env.router.enabled:
        remaining = env.deadline_at - time.monotonic()
        if remaining > 1.5:
            try:
                out = await asyncio.wait_for(
                    write_with_llm(env.router, t, fs, pb, recent_bodies, env.deadline_at, env.lease), timeout=remaining)
                via = "llm"
            except asyncio.TimeoutError:
                log.warning("llm compose for %s hit the deadline; using template", t.id)
            except Exception:
                log.exception("llm compose failed for %s; using template", t.id)
        elif env.lease:
            await env.router.release(env.lease)
    elif env.lease:
        await env.router.release(env.lease)

    if out is not None:
        c = _finish(t, pb, fs, out, via, [])
    else:
        c = compose_template(t, fs, recent_bodies)
    if env.cache_key and not c.violations:
        try:
            await env.store.set_json(env.cache_key, {"out": {"body": c.body, "cta": c.cta, "facts_used": c.facts_used},
                                                     "via": c.via.replace("cache:", "")}, DRAFT_TTL_S)
        except Exception:
            log.exception("draft cache write failed")
    return c
