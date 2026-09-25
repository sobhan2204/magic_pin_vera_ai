import os

import pytest

from app.store.memory_store import MemoryStore


@pytest.fixture(params=["memory", "redis"])
def any_store(request):
    if request.param == "memory":
        return MemoryStore()
    if not os.environ.get("UPSTASH_REDIS_REST_URL"):
        pytest.skip("set UPSTASH_REDIS_REST_URL/TOKEN to exercise the Lua scripts against a real Upstash DB")
    from app.store.redis_store import RedisStore
    return RedisStore(os.environ["UPSTASH_REDIS_REST_URL"], os.environ["UPSTASH_REDIS_REST_TOKEN"], "vera_test:")


async def test_context_cas_semantics(any_store):
    s = any_store
    await s.wipe()
    assert await s.put_context("merchant", "m1", 1, {"a": 1}) == ("new", 1)
    assert await s.put_context("merchant", "m1", 1, {"a": 999}) == ("same", 1)      # no change
    assert (await s.get_context("merchant", "m1")) == (1, {"a": 1})
    assert await s.put_context("merchant", "m1", 3, {"a": 3}) == ("replaced", 3)
    assert await s.put_context("merchant", "m1", 2, {"a": 2}) == ("stale", 3)
    assert (await s.get_context("merchant", "m1")) == (3, {"a": 3})
    await s.wipe()


async def test_counts_and_boot_marker(any_store):
    s = any_store
    await s.wipe()
    counts, boot = await s.health()
    assert counts == {} and boot is None
    await s.put_context("category", "dentists", 1, {})
    await s.put_context("merchant", "m1", 1, {})
    await s.put_context("merchant", "m1", 2, {})       # replace must not double count
    await s.put_context("merchant", "m2", 1, {})
    counts, boot = await s.health()
    assert counts == {"category": 1, "merchant": 2} and boot
    await s.wipe()


async def test_set_nx_is_exclusive(any_store):
    s = any_store
    await s.wipe()
    assert await s.set_nx("sent:event:k", "1", 60) is True
    assert await s.set_nx("sent:event:k", "1", 60) is False
    await s.wipe()


async def test_list_cap_sets_and_json(any_store):
    s = any_store
    await s.wipe()
    for i in range(35):
        await s.list_push_cap("sent:bodies:m1", f"b{i}", 30)
    lst = await s.list_range("sent:bodies:m1")
    assert len(lst) == 30 and lst[0] == "b5" and lst[-1] == "b34"
    await s.sadd("idx", "a", "b")
    assert await s.smembers("idx") == {"a", "b"}
    await s.set_json("k", {"x": [1, 2]})
    assert await s.get_json("k") == {"x": [1, 2]}
    assert await s.mget_json(["k", "missing"]) == [{"x": [1, 2]}, None]
    await s.wipe()
    assert await s.get_json("k") is None


async def test_mget_contexts_preserves_order(any_store):
    s = any_store
    await s.wipe()
    await s.put_context("trigger", "t1", 2, {"n": 1})
    res = await s.mget_contexts("trigger", ["t0", "t1"])
    assert res[0] is None and res[1] == (2, {"n": 1})
    await s.wipe()


async def test_quota_reserve_n_is_atomic_and_matches_sequential_reserve(any_store):
    s = any_store
    await s.wipe()
    import time
    lim = {"rpm": 9, "tpm": 900, "rpd": 900, "tpd": 180000}
    ts = time.time()
    assert await s.quota_reserve_n("m", 300, 5, lim, ts) == 3            # TPM allows 900 // 300
    assert await s.quota_reserve_n("m", 300, 5, lim, ts) == 0            # nothing left this minute
    st = await s.quota_state("m", ts)
    assert st["minute"] == {"req": 3, "tok": 900}
    await s.quota_cool("m2", 30)
    assert await s.quota_reserve_n("m2", 10, 3, lim, ts) == 0            # cooling model grants nothing
    assert await s.quota_reserve_n("m3", 10, 0, lim, ts) == 0
    await s.wipe()
