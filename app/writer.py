"""LLM writer: prompt -> structured JSON -> verify -> one repair -> None (caller falls back to the template)."""
from __future__ import annotations

import logging
import re
from typing import Optional

from .llm.router import Lease, Router, estimate_tokens
from .models import FactSheet
from .normalize import Trigger
from .playbooks import Playbook
from .prompts import (BATCH_IDS, BATCH_SCHEMA, MAX_TOKENS, MESSAGE_SCHEMA, build_batch_messages, build_repair_messages,
                      build_writer_messages, parse_batch, parse_output)
from .playbooks import get_playbook
from .verifier import changed_field_used, hook_covered, ungrounded_sentences, verify

log = logging.getLogger("vera.writer")


_SPECULATION = re.compile(r"\b(risk\w*|ignor\w*|affect\w*|impact\w*|hurt\w*|damag\w*|harm\w*)\b|miss ho\b", re.I)


def check(out: dict, fs: FactSheet, recent: list[str]) -> list[str]:
    v = verify(out, fs, recent)
    if not v and not changed_field_used(out["body"], fs, get_playbook(fs.kind).changed_first):
        v.append("the merchant's data was updated: use the updated field (see the fact starting 'Your Google profile now shows')")
    for s in ungrounded_sentences(out.get("body", ""), fs):
        v.append(f"sentence is not based on any listed fact: {s[:80]!r}. Restate a fact or remove it")
    fact_text = " ".join(f.text for f in fs.facts).lower()
    for m in _SPECULATION.finditer(out.get("body", "")):
        if m.group(0).lower() in fact_text:
            continue                                     # e.g. "affected batches" is the alert's own wording
        v.append(f"speculative claim {m.group(0)!r}: state only what the facts say, no invented causes or consequences")
    if not v and not hook_covered(out["body"], fs):
        v.append("message does not lead with the hook fact")
    return v


async def write_with_llm(router: Router, t: Trigger, fs: FactSheet, pb: Playbook, recent: list[str],
                         deadline_at: float, lease: Optional[Lease] = None) -> Optional[dict]:
    messages = build_writer_messages(fs, pb, t)
    if lease is None:
        lease = await router.acquire(estimate_tokens(messages, MAX_TOKENS))
    if lease is None:
        return None
    got = await router.run(lease, messages, max_tokens=MAX_TOKENS, validate=parse_output,
                           deadline_at=deadline_at, schema=MESSAGE_SCHEMA)
    if got is None:
        return None
    out, _ = got
    violations = check(out, fs, recent)
    if not violations:
        return out
    log.info("llm draft for %s rejected: %s", t.id, violations)

    repair_messages = build_repair_messages(messages, out, violations)
    lease2 = await router.acquire(estimate_tokens(repair_messages, MAX_TOKENS))
    if lease2 is None:
        return None
    got = await router.run(lease2, repair_messages, max_tokens=MAX_TOKENS, validate=parse_output,
                           deadline_at=deadline_at, schema=MESSAGE_SCHEMA)
    if got is None:
        return None
    fixed, _ = got
    violations = check(fixed, fs, recent)
    if violations:
        log.info("llm repair for %s still failing: %s", t.id, violations)
        return None
    return fixed


async def write_batch(router: Router, items: list[tuple[Trigger, FactSheet, Playbook, list[str]]], deadline_at: float,
                      lease: Optional[Lease]) -> dict[str, dict]:
    """One LLM call for up to 4 decisions. Returns trigger_id -> verified output; items that fail verification are simply
    absent (the caller uses the template for them). No per-item repair: a batch is an economy measure."""
    messages = build_batch_messages([(fs, pb) for _, fs, pb, _ in items])
    max_tokens = MAX_TOKENS * len(items)
    if lease is None:
        lease = await router.acquire(estimate_tokens(messages, max_tokens))
    if lease is None:
        return {}
    got = await router.run(lease, messages, max_tokens=max_tokens, validate=parse_batch, deadline_at=deadline_at,
                           schema=BATCH_SCHEMA)
    if got is None:
        return {}
    parsed, _ = got
    out: dict[str, dict] = {}
    for i, (t, fs, pb, recent) in enumerate(items):
        cand = parsed.get(BATCH_IDS[i])
        if cand is None:
            continue
        problems = check(cand, fs, recent)
        if problems:
            log.info("batch draft for %s rejected: %s", t.id, problems)
            continue
        out[t.id] = cand
    return out
