"""Decision engine: score, keep one per merchant, cap. Deterministic (ties broken by trigger id)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .humanize import parse_dt
from .models import Decision, FactSheet
from .normalize import Trigger
from .playbooks import get_playbook

RECENT_SEND_PENALTY_WINDOW = timedelta(hours=6)


def score_trigger(t: Trigger, now: datetime, mstate: dict) -> float:
    score = t.urgency * 10.0
    if t.expires_at:
        left = t.expires_at - now
        if left < timedelta(0):
            score -= 20                                  # already past its stated expiry: still sendable, but rank it last
        elif left <= timedelta(hours=48):
            score += 10
        elif left <= timedelta(days=7):
            score += 5
    if t.version > 1:
        score += 3                                   # freshly updated / newly pushed
    last = parse_dt(mstate.get("last_proactive_ts"))
    if last and now - last < RECENT_SEND_PENALTY_WINDOW:
        score -= 15
    return score


def decide(candidates: list[tuple[Trigger, FactSheet, dict]], now: datetime, cap: int) -> list[Decision]:
    """candidates: (trigger, factsheet, merchant_state) that already passed the policy gate."""
    scored: list[tuple[float, str, Decision]] = []
    for t, fs, mstate in candidates:
        pb = get_playbook(t.kind)
        hook = fs.get("hook")
        s = score_trigger(t, now, mstate)
        scored.append((s, t.id, Decision(
            action="send", trigger_id=t.id, merchant_id=t.merchant_id or "", customer_id=t.customer_id,
            objective=pb.objective, hook_fact_id=hook.id if hook else "",
            facts_allowed=[f.id for f in fs.facts], cta_type=pb.cta_type,
            reason=pb.why_now, score=s, kind=t.kind)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    seen: set[str] = set()
    out: list[Decision] = []
    for _, _, d in scored:
        if d.merchant_id in seen:
            continue
        seen.add(d.merchant_id)
        out.append(d)
        if len(out) >= cap:
            break
    return out
