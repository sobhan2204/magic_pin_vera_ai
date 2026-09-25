"""LLM writer: prompt -> structured JSON -> verify -> one repair -> None (caller falls back to the template)."""
from __future__ import annotations

import logging
from typing import Optional

from .llm.router import Lease, Router, estimate_tokens
from .models import FactSheet
from .normalize import Trigger
from .playbooks import Playbook
from .prompts import (MAX_TOKENS, MESSAGE_SCHEMA, build_repair_messages, build_writer_messages, parse_output)
from .verifier import hook_covered, verify

log = logging.getLogger("vera.writer")


def check(out: dict, fs: FactSheet, recent: list[str]) -> list[str]:
    v = verify(out, fs, recent)
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
