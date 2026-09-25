"""LLM path for genuine questions/answers: same closed-world facts, verified like outbound messages."""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from ..config import Settings
from ..llm.router import Router, estimate_tokens
from ..normalize import normalize_trigger
from ..prompts import MAX_TOKENS, MESSAGE_SCHEMA, build_reply_messages, parse_output
from ..resolver import build_factsheet
from ..store.base import Store
from ..verifier import verify

log = logging.getLogger("vera.reply.llm")


async def _fact_sheet(store: Store, conv: dict, merchant_id: Optional[str], customer_id: Optional[str]):
    if not merchant_id:
        return None
    m = await store.get_context("merchant", merchant_id)
    if not m:
        return None
    cat = await store.get_context("category", m[1].get("category_slug") or "")
    if not cat:
        return None
    cust = await store.get_context("customer", customer_id) if customer_id else None
    trig = await store.get_context("trigger", conv["trigger_id"]) if conv.get("trigger_id") else None
    t = normalize_trigger(conv.get("trigger_id") or "reply", trig[0] if trig else 1,
                          trig[1] if trig else {"kind": "generic", "merchant_id": merchant_id, "payload": {"placeholder": True}})
    now = datetime.now(timezone.utc)
    fs = build_factsheet(t, m[1], cat[1], cust[1] if cust else None, now)
    if fs is None:                                    # e.g. digest item vanished: fall back to a generic sheet
        g = normalize_trigger("reply", 1, {"kind": "generic", "merchant_id": merchant_id, "payload": {"placeholder": True}})
        fs = build_factsheet(g, m[1], cat[1], cust[1] if cust else None, now)
    return fs


async def try_llm_reply(store: Store, settings: Settings, *, conv: dict, merchant_id: Optional[str],
                        customer_id: Optional[str], message: str, lang: str, used: list[str], deliverable: str,
                        customer: bool) -> Optional[dict]:
    router = Router(store, settings)
    if not router.enabled:
        return None
    try:
        fs = await _fact_sheet(store, conv, merchant_id, customer_id)
        if fs is None:
            return None
        fs = replace(fs, language=lang)                                       # type: ignore[arg-type]
        messages = build_reply_messages(fs, conv.get("turns", []), message, deliverable, customer)
        lease = await router.acquire(estimate_tokens(messages, MAX_TOKENS))
        if lease is None:
            return None
        budget = max(2.0, min(float(settings.llm_call_timeout_s), settings.reply_deadline_s - 4))
        got = await router.run(lease, messages, max_tokens=MAX_TOKENS, validate=parse_output,
                               deadline_at=time.monotonic() + budget, schema=MESSAGE_SCHEMA)
        if got is None:
            return None
        out, _ = got
        problems = verify(out, fs, used, reply_mode=True, extra_text=f"{message} {deliverable}")
        if problems:
            log.info("llm reply rejected: %s", problems)
            return None
        return out
    except Exception:
        log.exception("llm reply failed; using deterministic reply")
        return None
