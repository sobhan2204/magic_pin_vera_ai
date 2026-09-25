"""Dedup helpers. Layer 1 (event reservation) lives in tick.py; layers 2/3 are pure functions here."""
from __future__ import annotations

import hashlib
import re

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


def action_fingerprint(merchant_id: str, customer_id: str | None, hook_key: str, hook_atoms: set[str], cta_type: str) -> str:
    raw = "|".join([merchant_id, customer_id or "-", hook_key, ",".join(sorted(hook_atoms)), cta_type])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
