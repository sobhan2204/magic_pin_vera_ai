from __future__ import annotations

from ..config import get_settings
from .base import Store

_store: Store | None = None


def get_store() -> Store:
    """Process-wide store singleton (Redis in production, memory for tests/local)."""
    global _store
    if _store is None:
        s = get_settings()
        if s.store_backend == "memory":
            from .memory_store import MemoryStore
            _store = MemoryStore()
        else:
            from .redis_store import RedisStore
            _store = RedisStore(s.upstash_url, s.upstash_token, s.key_prefix)
    return _store


def set_store(store: Store | None) -> None:
    global _store
    _store = store
