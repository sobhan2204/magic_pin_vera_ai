"""/v1/tick orchestration: load -> policy -> decide -> compose -> dedup -> finalize."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .compose import Composed, compose_message
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
EVENT_TTL_S = 7 * 24 * 3600
CONV_TTL_S = 3 * 24 * 3600


def conversation_id(t: Trigger) -> str:
    cid = f"conv_{t.merchant_id}_{t.id}"
    return f"{cid}_{t.customer_id}" if t.customer_id else cid


async def _load_bundle(store: Store, triggers: list[Trigger]):
    mids = sorted({t.merchant_id for t in triggers if t.merchant_id})
    cids = sorted({t.customer_id for t in triggers if t.customer_id})
    merchants = dict(zip(mids, await store.mget_contexts("merchant", mids)))
    slugs = sorted({(m[1].get("category_slug") or "") for m in merchants.values() if m} - {""})
    categories = dict(zip(slugs, await store.mget_contexts("category", slugs)))
    customers = dict(zip(cids, await store.mget_contexts("customer", cids)))
    mstates = dict(zip(mids, await load_states(store, [mkey(m, "") for m in mids])))
    cstates = dict(zip(cids, await load_states(store, [ckey(c) for c in cids])))
    sent_flags = await store.mget_json([event_key(t) for t in triggers])
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

    merchants, categories, customers, mstates, cstates, sent_flags = await _load_bundle(store, triggers)

    candidates: list[tuple[Trigger, FactSheet, dict]] = []
    for t, sent in zip(triggers, sent_flags):
        m = merchants.get(t.merchant_id or "")
        merchant = m[1] if m else None
        cat = categories.get((merchant or {}).get("category_slug") or "")
        cu = customers.get(t.customer_id or "")
        mstate = mstates.get(t.merchant_id or "", {})
        ok, reason = check_policy(t, merchant, cat[1] if cat else None, cu[1] if cu else None, mstate,
                                  cstates.get(t.customer_id or "", {}), now, already_sent=sent is not None)
        if not ok:
            log.info("no_op trigger=%s reason=%s", t.id, reason)
            continue
        fs = build_factsheet(t, merchant, cat[1], cu[1] if cu else None, now)   # type: ignore[index]
        if fs is None:
            log.info("no_op trigger=%s reason=missing_join", t.id)
            continue
        candidates.append((t, fs, mstate))

    decisions = decide(candidates, now, settings.max_actions_per_tick)
    by_id = {t.id: (t, fs) for t, fs, _ in candidates}

    async def build(d: Decision) -> Optional[tuple[Trigger, FactSheet, Composed, list[str]]]:
        t, fs = by_id[d.trigger_id]
        recent = await _recent_bodies(store, d.merchant_id)
        sent_fps = await store.smembers(f"sent:action:{d.merchant_id}")
        pb = get_playbook(t.kind)
        # dedup layers 2+3: try the planned hook, then the next-best hook facts, until the action is new
        # (layer 2) and the wording is not a near-duplicate of anything already sent (layer 3, in verify()).
        options = [fs] + [o for o in (promote_hook(fs, k) for k in alternate_hook_keys(fs, pb.support_keys)) if o]
        skipped_dup = False
        for opt in options:
            hook = opt.get("hook")
            src, atoms = hook_identity(hook) if hook else ("", set())
            c = compose_message(t, opt, recent)
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
        mstate = dict(mstates.get(t.merchant_id or "", {}))
        mstate["unanswered"] = int(mstate.get("unanswered", 0)) + 1
        mstate["last_proactive_ts"] = ts
        mstate["active_convs"] = (mstate.get("active_convs") or []) + [action["conversation_id"]]
        mstate["last_topic"] = t.kind
        mstate["pending"] = {"kind": t.kind, "deliverable": pb.deliverable, "trigger_id": t.id,
                             "hook": fs.text("hook"), "customer_id": t.customer_id}
        writes = [
            save_state(store, mkey(t.merchant_id, ""), mstate),
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
            cs = dict(cstates.get(t.customer_id, {}))
            cs["unanswered"] = int(cs.get("unanswered", 0)) + 1
            cs["last_proactive_ts"] = ts
            cs["pending"] = mstate["pending"]
            writes.append(save_state(store, ckey(t.customer_id), cs))
        await asyncio.gather(*writes)
        return action

    actions = [a for a in await asyncio.gather(*(finalize(i) for i in built)) if a]
    log.info("tick done actions=%d elapsed=%.2fs", len(actions), time.monotonic() - started)
    return actions[: settings.max_actions_per_tick]
