"""In-memory store for tests/local runs. Same semantics as RedisStore (single asyncio.Lock)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import time as _time

from .base import Store, rl_stamps


class MemoryStore(Store):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ctx: dict[tuple[str, str], tuple[int, dict]] = {}
        self._counts: dict[str, int] = {}
        self._boot: Optional[float] = None
        self._kv: dict[str, tuple[Any, Optional[float]]] = {}   # key -> (value, expires_at)
        self._sets: dict[str, set[str]] = {}
        self._lists: dict[str, list[str]] = {}
        self._quota: dict[str, dict[str, int]] = {}
        self._cool: dict[str, float] = {}

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

    async def quota_reserve(self, model, est_tokens, limits, ts):
        async with self._lock:
            if self._cool.get(model, 0) > _time.time():
                return "cooling"
            mstamp, dstamp = rl_stamps(ts)
            m = self._quota.setdefault(f"{model}:m:{mstamp}", {"req": 0, "tok": 0})
            d = self._quota.setdefault(f"{model}:d:{dstamp}", {"req": 0, "tok": 0})
            if (m["req"] + 1 > limits["rpm"] or m["tok"] + est_tokens > limits["tpm"]
                    or d["req"] + 1 > limits["rpd"] or d["tok"] + est_tokens > limits["tpd"]):
                return "quota"
            for c in (m, d):
                c["req"] += 1
                c["tok"] += est_tokens
            return "ok"

    async def quota_adjust(self, model, delta_tokens, ts, delta_requests=0):
        async with self._lock:
            for stamp, kind in zip(rl_stamps(ts), ("m", "d")):
                c = self._quota.setdefault(f"{model}:{kind}:{stamp}", {"req": 0, "tok": 0})
                c["tok"] = max(0, c["tok"] + delta_tokens)
                c["req"] = max(0, c["req"] + delta_requests)

    async def quota_cool(self, model, seconds):
        async with self._lock:
            self._cool[model] = _time.time() + seconds

    async def quota_state(self, model, ts):
        async with self._lock:
            mstamp, dstamp = rl_stamps(ts)
            return {"minute": dict(self._quota.get(f"{model}:m:{mstamp}", {"req": 0, "tok": 0})),
                    "day": dict(self._quota.get(f"{model}:d:{dstamp}", {"req": 0, "tok": 0})),
                    "cooling_s": max(0, round(self._cool.get(model, 0) - _time.time()))}

    async def wipe(self):
        async with self._lock:
            self._ctx.clear()
            self._counts.clear()
            self._boot = None
            self._kv.clear()
            self._sets.clear()
            self._lists.clear()
            self._quota.clear()
            self._cool.clear()
