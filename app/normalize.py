"""Canonical Trigger model from any pushed trigger payload shape."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from .humanize import parse_dt

_META_KEYS = {"id", "scope", "kind", "source", "merchant_id", "customer_id", "payload",
              "urgency", "suppression_key", "expires_at", "version"}


@dataclass(frozen=True)
class Trigger:
    id: str
    version: int
    kind: str
    scope: Literal["merchant", "customer"]
    source: Optional[str]
    merchant_id: Optional[str]
    customer_id: Optional[str]
    urgency: int
    suppression_key: str
    expires_at: Optional[datetime]
    payload: dict = field(default_factory=dict)


def _clamp_urgency(v: Any) -> int:
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 2


def _str_or_none(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def normalize_trigger(context_id: str, version: int, raw: Any) -> Trigger:
    """Accepts both shapes:
      A) {id, kind, merchant_id, customer_id, payload:{...}, urgency, ...}   (testing brief §3.4)
      B) {kind, payload:{merchant_id, customer_id, ...}}                     (challenge brief §6)
    Unknown kinds are accepted as-is."""
    raw = raw if isinstance(raw, dict) else {}
    inner = raw.get("payload")
    if not isinstance(inner, dict):
        # payload fields flattened next to the metadata
        inner = {k: v for k, v in raw.items() if k not in _META_KEYS}

    merchant_id = _str_or_none(raw.get("merchant_id")) or _str_or_none(inner.get("merchant_id"))
    customer_id = _str_or_none(raw.get("customer_id")) or _str_or_none(inner.get("customer_id"))
    kind = _str_or_none(raw.get("kind")) or _str_or_none(inner.get("kind")) or "generic"
    scope = raw.get("scope")
    if scope not in ("merchant", "customer"):
        scope = "customer" if customer_id else "merchant"
    trig_id = context_id or _str_or_none(raw.get("id")) or "unknown"
    supp = _str_or_none(raw.get("suppression_key")) or _str_or_none(inner.get("suppression_key")) \
        or f"{kind}:{merchant_id}:{customer_id or '-'}:{trig_id}"
    return Trigger(
        id=trig_id,
        version=int(version),
        kind=kind,
        scope=scope,  # type: ignore[arg-type]
        source=_str_or_none(raw.get("source")),
        merchant_id=merchant_id,
        customer_id=customer_id,
        urgency=_clamp_urgency(raw.get("urgency", inner.get("urgency"))),
        suppression_key=supp,
        expires_at=parse_dt(raw.get("expires_at") or inner.get("expires_at")),
        payload=inner,
    )
