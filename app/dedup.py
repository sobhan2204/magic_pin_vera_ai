"""Dedup helpers. Layer 1 (event reservation) lives in tick.py; layers 2/3 are pure functions here."""
from __future__ import annotations

import hashlib
import re
from dataclasses import replace

SIMILARITY_LIMIT = 0.7
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def normalize_body(text: str) -> list[str]:
    t = _EMOJI.sub(" ", (text or "").lower())
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return [w for w in t.split() if w]


def jaccard(a: str, b: str) -> float:
    sa, sb = set(normalize_body(a)), set(normalize_body(b))
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def too_similar(body: str, previous: list[str], limit: float = SIMILARITY_LIMIT) -> str | None:
    """Return the offending previous body (exact or >= limit token Jaccard), else None."""
    norm = " ".join(normalize_body(body))
    for p in previous:
        if " ".join(normalize_body(p)) == norm or jaccard(body, p) >= limit:
            return p
    return None


_GENERIC_ALTERNATES = ("m.perf30", "m.ctr", "m.offers", "m.week", "cat.trend", "cat.season", "m.retention", "m.lapsed")


def hook_identity(fact) -> tuple[str, set[str]]:
    """(fact type, atoms) that identify a hook. Facts with no numbers fall back to a text hash."""
    atoms = set(fact.atoms) or {hashlib.sha1(" ".join(normalize_body(fact.text)).encode()).hexdigest()[:8]}
    return fact.source, atoms


def alternate_hook_keys(fs, support_keys: tuple[str, ...]) -> list[str]:
    keys = [k for k in (*support_keys, *_GENERIC_ALTERNATES) if fs.get(k)]
    return list(dict.fromkeys(keys))[:4]


def promote_hook(fs, key: str):
    """Copy of fs that leads with fact `key`; the original hook is kept as first supporting fact (why-now survives)."""
    chosen, old = fs.get(key), fs.get("hook")
    if not chosen or not old or chosen is old:
        return None
    rest = [f for f in fs.facts if f is not chosen and f is not old]
    facts = [replace(chosen, key="hook"), replace(old, key="t.prev_hook"), *rest]
    return replace(fs, facts=facts)


def action_fingerprint(merchant_id: str, customer_id: str | None, hook_key: str, hook_atoms: set[str], cta_type: str) -> str:
    raw = "|".join([merchant_id, customer_id or "-", hook_key, ",".join(sorted(hook_atoms)), cta_type])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
