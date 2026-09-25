"""Async store interface. Both backends must have identical semantics."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

SCOPES = ("category", "merchant", "customer", "trigger")


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

    @abstractmethod
    async def wipe(self) -> None: ...
