"""Upstash Redis store (REST). Atomic operations use Lua EVAL / SET NX."""
from __future__ import annotations

import json
from typing import Any, Optional

from .base import Store, rl_stamps

# KEYS[1]=ctx hash, KEYS[2]=counts hash ; ARGV = version, payload_json, scope, now_ts
_PUT_CONTEXT = """
local nv = tonumber(ARGV[1])
local cur = redis.call('HGET', KEYS[1], 'v')
if cur then
  cur = tonumber(cur)
  if cur == nv then return {'same', tostring(cur)} end
  if cur > nv then return {'stale', tostring(cur)} end
  if ARGV[3] == 'merchant' then
    redis.call('HSET', KEYS[1], 'v', ARGV[1], 'p', ARGV[2], 'pp', redis.call('HGET', KEYS[1], 'p'))
  else
    redis.call('HSET', KEYS[1], 'v', ARGV[1], 'p', ARGV[2])
  end
  return {'replaced', ARGV[1]}
end
redis.call('HSET', KEYS[1], 'v', ARGV[1], 'p', ARGV[2])
redis.call('HINCRBY', KEYS[2], ARGV[3], 1)
redis.call('HSETNX', KEYS[2], 'boot_ts', ARGV[4])
return {'new', ARGV[1]}
"""


# KEYS: minute hash, day hash, cooling key ; ARGV: est, rpm, tpm, rpd, tpd
_QUOTA_RESERVE = """
if redis.call('EXISTS', KEYS[3]) == 1 then return 2 end
local est = tonumber(ARGV[1])
local mr = tonumber(redis.call('HGET', KEYS[1], 'req') or '0')
local mt = tonumber(redis.call('HGET', KEYS[1], 'tok') or '0')
local dr = tonumber(redis.call('HGET', KEYS[2], 'req') or '0')
local dt = tonumber(redis.call('HGET', KEYS[2], 'tok') or '0')
if mr + 1 > tonumber(ARGV[2]) or mt + est > tonumber(ARGV[3]) or dr + 1 > tonumber(ARGV[4]) or dt + est > tonumber(ARGV[5]) then
  return 0
end
redis.call('HINCRBY', KEYS[1], 'req', 1)
redis.call('HINCRBY', KEYS[1], 'tok', est)
redis.call('EXPIRE', KEYS[1], 180)
redis.call('HINCRBY', KEYS[2], 'req', 1)
redis.call('HINCRBY', KEYS[2], 'tok', est)
redis.call('EXPIRE', KEYS[2], 172800)
return 1
"""


# KEYS: minute hash, day hash, cooling key ; ARGV: est, n, rpm, tpm, rpd, tpd  -> number granted
_QUOTA_RESERVE_N = """
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
local est = math.max(1, tonumber(ARGV[1]))
local n = tonumber(ARGV[2])
local mr = tonumber(redis.call('HGET', KEYS[1], 'req') or '0')
local mt = tonumber(redis.call('HGET', KEYS[1], 'tok') or '0')
local dr = tonumber(redis.call('HGET', KEYS[2], 'req') or '0')
local dt = tonumber(redis.call('HGET', KEYS[2], 'tok') or '0')
local k = math.min(n, tonumber(ARGV[3]) - mr, tonumber(ARGV[5]) - dr,
                   math.floor((tonumber(ARGV[4]) - mt) / est), math.floor((tonumber(ARGV[6]) - dt) / est))
if k <= 0 then return 0 end
redis.call('HINCRBY', KEYS[1], 'req', k)
redis.call('HINCRBY', KEYS[1], 'tok', k * est)
redis.call('EXPIRE', KEYS[1], 180)
redis.call('HINCRBY', KEYS[2], 'req', k)
redis.call('HINCRBY', KEYS[2], 'tok', k * est)
redis.call('EXPIRE', KEYS[2], 172800)
return k
"""


def _pairs_to_dict(res: Any) -> dict:
    if isinstance(res, dict):
        return res
    if isinstance(res, list):
        return {res[i]: res[i + 1] for i in range(0, len(res) - 1, 2)}
    return {}


class RedisStore(Store):
    def __init__(self, url: str, token: str, prefix: str = "vera:") -> None:
        from upstash_redis.asyncio import Redis  # imported lazily: keeps cold start light
        self.r = Redis(url=url, token=token)
        self.prefix = prefix

    def _k(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    # --- contexts -----------------------------------------------------------------
    async def put_context(self, scope, cid, version, payload):
        import time
        res = await self.r.eval(
            _PUT_CONTEXT,
            keys=[self._k("ctx", scope, cid), self._k("counts")],
            args=[str(int(version)), json.dumps(payload, ensure_ascii=False), scope, str(time.time())],
        )
        return str(res[0]), int(float(res[1]))

    @staticmethod
    def _decode(res: Any) -> Optional[tuple[int, dict]]:
        if not res or res[0] is None or res[1] is None:
            return None
        return int(float(res[0])), json.loads(res[1])

    async def get_context(self, scope, cid):
        return self._decode(await self.r.hmget(self._k("ctx", scope, cid), "v", "p"))

    async def mget_contexts(self, scope, cids):
        if not cids:
            return []
        pipe = self.r.pipeline()
        for c in cids:
            pipe.hmget(self._k("ctx", scope, c), "v", "p")
        return [self._decode(r) for r in await pipe.exec()]

    async def mget_previous(self, scope, cids):
        if not cids:
            return []
        pipe = self.r.pipeline()
        for c in cids:
            pipe.hget(self._k("ctx", scope, c), "pp")
        return [json.loads(v) if isinstance(v, str) and v else None for v in await pipe.exec()]

    async def health(self):
        raw = _pairs_to_dict(await self.r.hgetall(self._k("counts")))
        counts = {k: int(v) for k, v in raw.items() if k != "boot_ts"}
        boot = float(raw["boot_ts"]) if raw.get("boot_ts") else None
        return counts, boot

    # --- generic primitives ---------------------------------------------------------
    async def set_nx(self, key, value, ttl_s):
        return bool(await self.r.set(self._k(key), value, nx=True, ex=ttl_s))

    async def get_json(self, key):
        v = await self.r.get(self._k(key))
        return json.loads(v) if isinstance(v, str) else v

    async def set_json(self, key, value, ttl_s=None):
        await self.r.set(self._k(key), json.dumps(value, ensure_ascii=False), ex=ttl_s)

    async def mget_json(self, keys):
        if not keys:
            return []
        res = await self.r.mget(*[self._k(k) for k in keys])
        return [json.loads(v) if isinstance(v, str) else v for v in res]

    async def sadd(self, key, *members):
        if members:
            await self.r.sadd(self._k(key), *members)

    async def smembers(self, key):
        return set(await self.r.smembers(self._k(key)) or [])

    async def list_push_cap(self, key, item, cap):
        pipe = self.r.pipeline()
        pipe.rpush(self._k(key), json.dumps(item, ensure_ascii=False))
        pipe.ltrim(self._k(key), -cap, -1)
        await pipe.exec()

    async def list_range(self, key):
        return [json.loads(x) for x in (await self.r.lrange(self._k(key), 0, -1) or [])]

    async def exists(self, key):
        return bool(await self.r.exists(self._k(key)))

    def _quota_keys(self, model: str, ts: float) -> tuple[str, str, str]:
        m, d = rl_stamps(ts)
        return self._k("rl", model, "m", m), self._k("rl", model, "d", d), self._k("rl", model, "cool")

    async def quota_reserve(self, model, est_tokens, limits, ts):
        mk, dk, ck = self._quota_keys(model, ts)
        res = await self.r.eval(_QUOTA_RESERVE, keys=[mk, dk, ck],
                                args=[str(est_tokens), str(limits["rpm"]), str(limits["tpm"]),
                                      str(limits["rpd"]), str(limits["tpd"])])
        return {1: "ok", 2: "cooling"}.get(int(res), "quota")

    async def quota_reserve_n(self, model, est_tokens, n, limits, ts):
        if n <= 0:
            return 0
        mk, dk, ck = self._quota_keys(model, ts)
        res = await self.r.eval(_QUOTA_RESERVE_N, keys=[mk, dk, ck],
                                args=[str(est_tokens), str(n), str(limits["rpm"]), str(limits["tpm"]),
                                      str(limits["rpd"]), str(limits["tpd"])])
        return max(0, int(res))

    async def quota_adjust(self, model, delta_tokens, ts, delta_requests=0):
        mk, dk, _ = self._quota_keys(model, ts)
        pipe = self.r.pipeline()
        for k in (mk, dk):
            pipe.hincrby(k, "tok", int(delta_tokens))
            if delta_requests:
                pipe.hincrby(k, "req", int(delta_requests))
        await pipe.exec()

    async def quota_cool(self, model, seconds):
        await self.r.set(self._k("rl", model, "cool"), "1", ex=int(seconds))

    async def quota_state(self, model, ts):
        mk, dk, ck = self._quota_keys(model, ts)
        pipe = self.r.pipeline()
        pipe.hgetall(mk)
        pipe.hgetall(dk)
        pipe.ttl(ck)
        m, d, ttl = await pipe.exec()
        conv = lambda h: {k: int(v) for k, v in _pairs_to_dict(h).items()} or {"req": 0, "tok": 0}
        return {"minute": conv(m), "day": conv(d), "cooling_s": max(0, int(ttl or 0))}

    async def wipe(self):
        cursor = 0
        while True:
            cursor, keys = await self.r.scan(cursor, match=self.prefix + "*", count=500)
            if keys:
                await self.r.delete(*keys)
            if int(cursor) == 0:
                break
