"""Async store interface. Both backends must have identical semantics."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


def rl_stamps(ts: float) -> tuple[str, str]:
    """(minute bucket, day bucket) in UTC for rate-limit counters."""
    import time as _t
    g = _t.gmtime(ts)
    return _t.strftime("%Y%m%d%H%M", g), _t.strftime("%Y%m%d", g)


class Store(ABC):
    # --- contexts -----------------------------------------------------------------
    @abstractmethod
    async def put_context(self, scope: str, cid: str, version: int, payload: dict) -> tuple[str, int]:
        """Atomic compare-and-set. Returns (status, version) with status in
        new | replaced | same | stale ; version is the stored version after the call."""

    @abstractmethod
    async def get_context(self, scope: str, cid: str) -> Optional[tuple[int, dict]]: ...

    @abstractmethod
    async def mget_contexts(self, scope: str, cids: list[str]) -> list[Optional[tuple[int, dict]]]: ...

    @abstractmethod
    async def health(self) -> tuple[dict, Optional[float]]:
        """One round-trip. Returns (counts, boot_ts or None)."""

    # --- generic primitives ---------------------------------------------------------
    @abstractmethod
    async def set_nx(self, key: str, value: str, ttl_s: int) -> bool: ...

    @abstractmethod
    async def get_json(self, key: str) -> Optional[Any]: ...

    @abstractmethod
    async def set_json(self, key: str, value: Any, ttl_s: Optional[int] = None) -> None: ...

    @abstractmethod
    async def mget_json(self, keys: list[str]) -> list[Optional[Any]]: ...

    @abstractmethod
    async def sadd(self, key: str, *members: str) -> None: ...

    @abstractmethod
    async def smembers(self, key: str) -> set[str]: ...

    @abstractmethod
    async def list_push_cap(self, key: str, item: Any, cap: int) -> None: ...

    @abstractmethod
    async def list_range(self, key: str) -> list[Any]: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    # --- LLM quota accounting (atomic check-and-increment) ------------------------------------
    @abstractmethod
    async def quota_reserve(self, model: str, est_tokens: int, limits: dict, ts: float) -> str:
        """Atomically reserve one request + est_tokens against RPM/TPM/RPD/TPD.
        limits = {rpm, tpm, rpd, tpd}. Returns 'ok' | 'quota' | 'cooling'."""

    @abstractmethod
    async def quota_adjust(self, model: str, delta_tokens: int, ts: float, delta_requests: int = 0) -> None: ...

    @abstractmethod
    async def quota_cool(self, model: str, seconds: int) -> None: ...

    @abstractmethod
    async def quota_state(self, model: str, ts: float) -> dict: ...

    @abstractmethod
    async def wipe(self) -> None: ...
