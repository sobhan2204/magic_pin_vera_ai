"""In-memory store for tests/local runs. Same semantics as RedisStore (single asyncio.Lock)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from .base import Store


class MemoryStore(Store):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ctx: dict[tuple[str, str], tuple[int, dict]] = {}
        self._counts: dict[str, int] = {}
        self._boot: Optional[float] = None
        self._kv: dict[str, tuple[Any, Optional[float]]] = {}   # key -> (value, expires_at)
        self._sets: dict[str, set[str]] = {}
        self._lists: dict[str, list[str]] = {}

    def _alive(self, key: str) -> bool:
        item = self._kv.get(key)
        if item is None:
            return False
        if item[1] is not None and item[1] <= time.time():
            del self._kv[key]
            return False
        return True

    async def put_context(self, scope, cid, version, payload):
        async with self._lock:
            cur = self._ctx.get((scope, cid))
            if cur:
                if cur[0] == version:
                    return "same", cur[0]
                if cur[0] > version:
                    return "stale", cur[0]
                self._ctx[(scope, cid)] = (version, json.loads(json.dumps(payload)))
                return "replaced", version
            self._ctx[(scope, cid)] = (version, json.loads(json.dumps(payload)))
            self._counts[scope] = self._counts.get(scope, 0) + 1
            if self._boot is None:
                self._boot = time.time()
            return "new", version

    async def get_context(self, scope, cid):
        async with self._lock:
            return self._ctx.get((scope, cid))

    async def mget_contexts(self, scope, cids):
        async with self._lock:
            return [self._ctx.get((scope, c)) for c in cids]

    async def health(self):
        async with self._lock:
            return dict(self._counts), self._boot

    async def set_nx(self, key, value, ttl_s):
        async with self._lock:
            if self._alive(key):
                return False
            self._kv[key] = (value, time.time() + ttl_s if ttl_s else None)
            return True

    async def get_json(self, key):
        async with self._lock:
            if not self._alive(key):
                return None
            v = self._kv[key][0]
            return json.loads(v) if isinstance(v, str) else v

    async def set_json(self, key, value, ttl_s=None):
        async with self._lock:
            self._kv[key] = (json.dumps(value), time.time() + ttl_s if ttl_s else None)

    async def mget_json(self, keys):
        return [await self.get_json(k) for k in keys]

    async def sadd(self, key, *members):
        async with self._lock:
            self._sets.setdefault(key, set()).update(members)

    async def smembers(self, key):
        async with self._lock:
            return set(self._sets.get(key, set()))

    async def list_push_cap(self, key, item, cap):
        async with self._lock:
            lst = self._lists.setdefault(key, [])
            lst.append(json.dumps(item))
            del lst[:-cap]

    async def list_range(self, key):
        async with self._lock:
            return [json.loads(x) for x in self._lists.get(key, [])]

    async def exists(self, key):
        async with self._lock:
            return self._alive(key) or key in self._sets or key in self._lists

    async def wipe(self):
        async with self._lock:
            self._ctx.clear()
            self._counts.clear()
            self._boot = None
            self._kv.clear()
            self._sets.clear()
            self._lists.clear()
