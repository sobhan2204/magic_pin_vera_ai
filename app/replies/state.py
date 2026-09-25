"""Merchant-/customer-level behavioural memory. Conversation ids are only transport ids."""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from ..store.base import Store

STATE_TTL_S = 7 * 24 * 3600


def default_state() -> dict:
    return {
        "unanswered": 0, "last_proactive_ts": None, "opted_out": False, "auto_count": 0,
        "seen_hashes": [], "hostile_count": 0, "active_convs": [], "pending": None,
        "last_topic": None, "closed_convs": [],
    }


def mkey(merchant_id: Optional[str], conversation_id: str) -> str:
    return f"mstate:{merchant_id}" if merchant_id else f"mstate:conv:{conversation_id}"


def ckey(customer_id: str) -> str:
    return f"cstate:{customer_id}"


async def load_state(store: Store, key: str) -> dict:
    s = default_state()
    s.update(await store.get_json(key) or {})
    return s


async def load_states(store: Store, keys: list[str]) -> list[dict]:
    out = []
    for raw in await store.mget_json(keys):
        s = default_state()
        s.update(raw or {})
        out.append(s)
    return out


async def save_state(store: Store, key: str, state: dict) -> None:
    state["seen_hashes"] = state.get("seen_hashes", [])[-30:]
    state["active_convs"] = state.get("active_convs", [])[-10:]
    state["closed_convs"] = state.get("closed_convs", [])[-20:]
    await store.set_json(key, state, STATE_TTL_S)


def message_hash(text: str) -> str:
    norm = re.sub(r"[^\w\s]", " ", (text or "").lower())
    norm = " ".join(norm.split())
    return hashlib.sha1(norm.encode()).hexdigest()[:12]
