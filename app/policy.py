"""Policy gate: returns (allowed, reason). Pure function over already-loaded data."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .humanize import parse_dt
from .normalize import Trigger
from .playbooks import get_playbook

OPEN_CONVERSATION_WINDOW = timedelta(hours=24)


def event_key(t: Trigger) -> str:
    """Per-recipient reservation key (see README: the seed data reuses category-wide suppression keys)."""
    return f"sent:event:{t.suppression_key}|{t.merchant_id}|{t.customer_id or '-'}"


def check_policy(t: Trigger, merchant: Optional[dict], category: Optional[dict], customer: Optional[dict],
                 mstate: dict, cstate: dict, now: datetime, already_sent: bool, consent_mode: str = "lenient") -> tuple[bool, str]:
    if not t.merchant_id or not merchant:
        return False, "missing_merchant"
    if not category:
        return False, "missing_category"
    # expires_at is NOT a gate: a trigger the judge lists in available_triggers is active. Expiry only affects ranking.
    if already_sent:
        return False, "already_sent"
    if mstate.get("opted_out"):
        return False, "merchant_opted_out"

    if t.scope == "customer":
        if not t.customer_id or not customer:
            return False, "missing_customer"
        if cstate.get("opted_out"):
            return False, "customer_opted_out"
        consent = customer.get("consent") or {}
        scopes = [s for s in (consent.get("scope") or []) if isinstance(s, str)]
        if not consent.get("opted_in_at") and not scopes:
            return False, "no_consent"
        wanted = get_playbook(t.kind).consent
        if consent_mode == "strict" and wanted and not (set(wanted) & set(scopes)):
            return False, "consent_scope_mismatch"
        if not wanted and not scopes:
            return False, "no_consent"

    # Restraint is per RECIPIENT: a merchant's open conversation must not block a message to one of their customers,
    # and a customer's open conversation must not block a message to the merchant.
    state = cstate if t.scope == "customer" else mstate
    unanswered = int(state.get("unanswered", 0) or 0)
    if unanswered >= 1 and t.urgency < 4:
        last = parse_dt(state.get("last_proactive_ts"))
        if unanswered >= 2 or (last and now - last < OPEN_CONVERSATION_WINDOW):
            return False, "awaiting_reply"
    return True, "ok"
