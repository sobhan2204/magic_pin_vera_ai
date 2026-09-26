"""/v1/tick orchestration: load -> policy -> decide -> compose -> dedup -> finalize."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .compose import ComposeEnv, Composed, compose_message, draft_cache_key
from .llm.router import Router, estimate_tokens
from .prompts import MAX_TOKENS
from .writer import write_batch
from .config import Settings
from .decision import decide
from .dedup import action_fingerprint, alternate_hook_keys, hook_identity, promote_hook
from .humanize import parse_dt
from .models import Action, Decision, FactSheet
from .normalize import Trigger, normalize_trigger
from .playbooks import get_playbook
from .policy import check_policy, event_key
from .replies.state import ckey, load_states, mkey, save_state
from .resolver import build_factsheet
from .store.base import Store

log = logging.getLogger("vera.tick")
FINALIZE_RESERVE_S = 3.0          # time kept back for Redis writes after composing
MAX_CONCURRENT_COMPOSES = 8
EVENT_TTL_S = 7 * 24 * 3600
CONV_TTL_S = 3 * 24 * 3600


def conversation_id(t: Trigger) -> str:
    cid = f"conv_{t.merchant_id}_{t.id}"
    return f"{cid}_{t.customer_id}" if t.customer_id else cid


async def _load_bundle(store: Store, triggers: list[Trigger], previous: dict):
    mids = sorted({t.merchant_id for t in triggers if t.merchant_id})
    cids = sorted({t.customer_id for t in triggers if t.customer_id})
    # everything that only needs the trigger list is fetched together (Redis latency is per round-trip, not per key)
    m_rows, c_rows, m_states, c_states, sent_flags, m_prev = await asyncio.gather(
        store.mget_contexts("merchant", mids), store.mget_contexts("customer", cids),
        load_states(store, [mkey(m, "") for m in mids]), load_states(store, [ckey(c) for c in cids]),
        store.mget_json([event_key(t) for t in triggers]), store.mget_previous("merchant", mids))
    merchants, customers = dict(zip(mids, m_rows)), dict(zip(cids, c_rows))
    previous.update(dict(zip(mids, m_prev)))
    mstates, cstates = dict(zip(mids, m_states)), dict(zip(cids, c_states))
    slugs = sorted({(m[1].get("category_slug") or "") for m in merchants.values() if m} - {""})
    categories = dict(zip(slugs, await store.mget_contexts("category", slugs)))
    return merchants, categories, customers, mstates, cstates, sent_flags


async def _recent_bodies(store: Store, merchant_id: str) -> list[str]:
    return [b for b in await store.list_range(f"sent:bodies:{merchant_id}") if isinstance(b, str)]


async def run_tick(store: Store, settings: Settings, body: dict) -> list[dict]:
    started = time.monotonic()
    now = parse_dt(body.get("now")) or datetime.now(timezone.utc)
    ids = [i for i in dict.fromkeys(body.get("available_triggers") or []) if isinstance(i, str)]
    if not ids:
        return []

    rows = await store.mget_contexts("trigger", ids)
    triggers = [normalize_trigger(i, r[0], r[1]) for i, r in zip(ids, rows) if r]
    if not triggers:
        return []

    previous: dict = {}                                   # merchant_id -> payload that the current version replaced
    merchants, categories, customers, mstates, cstates, sent_flags = await _load_bundle(store, triggers, previous)
    deadline_at = started + settings.tick_deadline_s - FINALIZE_RESERVE_S
    router = Router(store, settings)
    tick_date = now.date().isoformat()

    candidates: list[tuple[Trigger, FactSheet, dict]] = []
    for t, sent in zip(triggers, sent_flags):
        m = merchants.get(t.merchant_id or "")
        merchant = m[1] if m else None
        cat = categories.get((merchant or {}).get("category_slug") or "")
        cu = customers.get(t.customer_id or "")
        mstate = mstates.get(t.merchant_id or "", {})
        ok, reason = check_policy(t, merchant, cat[1] if cat else None, cu[1] if cu else None, mstate,
                                  cstates.get(t.customer_id or "", {}), now, already_sent=sent is not None,
                                  consent_mode=settings.consent_mode)
        if not ok:
            log.info("no_op trigger=%s reason=%s", t.id, reason)
            continue
        fs = build_factsheet(t, merchant, cat[1], cu[1] if cu else None, now, prev_merchant=previous.get(t.merchant_id or ""))   # type: ignore[index]
        if fs is None:
            log.info("no_op trigger=%s reason=missing_join", t.id)
            continue
        candidates.append((t, fs, mstate))

    decisions = decide(candidates, now, settings.max_actions_per_tick)
    by_id = {t.id: (t, fs) for t, fs, _ in candidates}

    def versions_for(t: Trigger) -> dict:
        m = merchants.get(t.merchant_id or "")
        cat = categories.get(((m[1] if m else {}) or {}).get("category_slug") or "")
        cu = customers.get(t.customer_id or "")
        return {"m": m[0] if m else None, "cat": cat[0] if cat else None, "cust": cu[0] if cu else "-"}

    # Cache lookups (determinism) and quota leases are settled up front, in score order, so which drafts
    # get the LLM never depends on task scheduling. The HTTP calls themselves then run concurrently.
    keys = {d.trigger_id: draft_cache_key(by_id[d.trigger_id][0], versions_for(by_id[d.trigger_id][0]), tick_date)
            for d in decisions}
    try:
        hits = dict(zip(keys, await store.mget_json(list(keys.values()))))
    except Exception:
        hits = {}
    leases = {}
    batch_out: dict[str, dict] = {}
    todo = [d for d in decisions if not hits.get(d.trigger_id)]
    if router.enabled and settings.llm_batch_size > 1 and todo and time.monotonic() < deadline_at - 2:
        # Economy mode: several decisions share one prompt/call. Groups are formed in score order.
        size = settings.llm_batch_size
        groups = [todo[i:i + size] for i in range(0, len(todo), size)]

        recents = dict(zip((d.trigger_id for d in todo),
                           await asyncio.gather(*(_recent_bodies(store, d.merchant_id) for d in todo))))
        planned = []                                     # (group items, lease) - quota goes to the best-scored groups first
        for group in groups:
            items = [(by_id[d.trigger_id][0], by_id[d.trigger_id][1], get_playbook(by_id[d.trigger_id][0].kind),
                      recents[d.trigger_id]) for d in group]
            est = estimate_tokens([{"content": "x" * (1500 + 2200 * len(items))}], MAX_TOKENS * len(items))
            lease = await router.acquire(est)            # sequential on purpose: deterministic priority by score
            if lease is None:
                break                                    # quota exhausted: every lower-ranked group uses the template
            planned.append((items, lease))
        for out in await asyncio.gather(*(write_batch(router, items, deadline_at, lease) for items, lease in planned)):
            batch_out.update(out)
    elif router.enabled and time.monotonic() < deadline_at - 2:
        need = [d for d in decisions if not hits.get(d.trigger_id)]
        est = estimate_tokens([{"content": "x" * 3600}], MAX_TOKENS)          # ~1k-token prompt + max completion
        for d, lease in zip(need, await router.acquire_many(est, len(need))):   # score order; the rest use templates
            leases[d.trigger_id] = lease
    sem = asyncio.Semaphore(MAX_CONCURRENT_COMPOSES)

    async def build(d: Decision) -> Optional[tuple[Trigger, FactSheet, Composed, list[str]]]:
        async with sem:
            return await _build(d)

    async def _build(d: Decision) -> Optional[tuple[Trigger, FactSheet, Composed, list[str]]]:
        t, fs = by_id[d.trigger_id]
        recent, sent_fps = await asyncio.gather(_recent_bodies(store, d.merchant_id),
                                                store.smembers(f"sent:action:{d.merchant_id}"))
        pb = get_playbook(t.kind)
        # dedup layers 2+3: try the planned hook, then the next-best hook facts, until the action is new
        # (layer 2) and the wording is not a near-duplicate of anything already sent (layer 3, in verify()).
        options = [fs] + [o for o in (promote_hook(fs, k) for k in alternate_hook_keys(fs, pb.support_keys)) if o]
        skipped_dup = False
        vers = versions_for(t)
        for n, opt in enumerate(options):
            hook = opt.get("hook")
            src, atoms = hook_identity(hook) if hook else ("", set())
            first = n == 0
            env = ComposeEnv(store=store, settings=settings, router=router, deadline_at=deadline_at,
                             lease=leases.get(d.trigger_id) if first else None,
                             cache_key=keys[d.trigger_id] if first else draft_cache_key(t, vers, tick_date, f"alt{n}"),
                             allow_llm=first and settings.llm_batch_size <= 1,
                             llm_out=batch_out.get(d.trigger_id) if first else None,
                             cached=hits.get(d.trigger_id) if first else None, prefetched=first)
            c = await compose_message(env, t, opt, recent)
            fp = action_fingerprint(d.merchant_id, d.customer_id, src, atoms, c.cta)
            if fp in sent_fps:
                skipped_dup = True
                continue
            if c.violations:
                log.info("trigger=%s option rejected: %s", t.id, c.violations)
                continue
            return t, opt, c, [fp]
        log.info("no_op trigger=%s reason=%s", t.id, "duplicate_action" if skipped_dup else "no_valid_message")
        return None

    built = [b for b in await asyncio.gather(*(build(d) for d in decisions)) if b]

    async def finalize(item) -> Optional[dict]:
        t, fs, c, (fp,) = item
        # dedup layer 1: atomic event reservation
        if not await store.set_nx(event_key(t), "1", EVENT_TTL_S):
            return None
        pb = get_playbook(t.kind)
        action = Action(
            conversation_id=conversation_id(t), merchant_id=t.merchant_id or "", customer_id=t.customer_id,
            send_as=fs.send_as, trigger_id=t.id,
            template_name=f"{'merchant' if fs.send_as == 'merchant_on_behalf' else 'vera'}_{t.kind}_v1",
            template_params=c.template_params, body=c.body, cta=c.cta, suppression_key=t.suppression_key,
            rationale=c.rationale,
        ).model_dump()
        ts = now.isoformat()
        pending = {"kind": t.kind, "deliverable": pb.deliverable, "trigger_id": t.id,
                   "hook": fs.text("hook"), "customer_id": t.customer_id}
        writes = [
            store.set_json(f"conv:{action['conversation_id']}", {
                "merchant_id": t.merchant_id, "customer_id": t.customer_id, "trigger_id": t.id, "kind": t.kind,
                "send_as": fs.send_as, "language": fs.language, "slots": fs.slots,
                "turns": [{"from": "bot", "body": c.body, "ts": ts}],
            }, CONV_TTL_S),
            store.list_push_cap(f"sent:bodies:{t.merchant_id}", c.body, 30),
            store.sadd(f"sent:action:{t.merchant_id}", fp),
            store.list_push_cap("dbg:actions", {"trigger": t.id, "merchant": t.merchant_id, "body": c.body}, 20),
        ]
        if t.customer_id:
            # customer-facing: only the CUSTOMER's conversation state changes; the merchant's open/unanswered state is untouched
            cs = dict(cstates.get(t.customer_id, {}))
            cs["unanswered"] = int(cs.get("unanswered", 0)) + 1
            cs["last_proactive_ts"] = ts
            cs["pending"] = pending
            writes.append(save_state(store, ckey(t.customer_id), cs))
        else:
            mstate = dict(mstates.get(t.merchant_id or "", {}))
            mstate["unanswered"] = int(mstate.get("unanswered", 0)) + 1
            mstate["last_proactive_ts"] = ts
            mstate["active_convs"] = (mstate.get("active_convs") or []) + [action["conversation_id"]]
            mstate["last_topic"] = t.kind
            mstate["pending"] = pending
            writes.append(save_state(store, mkey(t.merchant_id, ""), mstate))
        await asyncio.gather(*writes)
        return action

    actions = [a for a in await asyncio.gather(*(finalize(i) for i in built)) if a]
    log.info("tick done actions=%d elapsed=%.2fs", len(actions), time.monotonic() - started)
    return actions[: settings.max_actions_per_tick]
