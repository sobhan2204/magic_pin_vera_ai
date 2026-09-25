from conftest import NOW_ISO, push


async def test_healthz_empty_then_counts(client):
    r = await client.get("/v1/healthz")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok" and j["contexts_loaded"] == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    await push(client, "category", "dentists", {"slug": "dentists"})
    await push(client, "merchant", "m1", {"merchant_id": "m1"})
    j = (await client.get("/v1/healthz")).json()
    assert j["contexts_loaded"]["category"] == 1 and j["contexts_loaded"]["merchant"] == 1
    assert isinstance(j["uptime_seconds"], int)


async def test_metadata_shape(client):
    j = (await client.get("/v1/metadata")).json()
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert k in j
    assert isinstance(j["team_members"], list) and "gpt-oss-120b" in j["model"]


async def test_context_versioning_200_noop_409(client):
    r = await push(client, "merchant", "m1", {"v": 1}, version=1)
    assert r.status_code == 200 and r.json()["accepted"] is True and r.json()["ack_id"] == "ack_m1_v1"
    same = await push(client, "merchant", "m1", {"v": "changed"}, version=1)
    assert same.status_code == 200 and same.json()["accepted"] is True          # idempotent no-op
    up = await push(client, "merchant", "m1", {"v": 2}, version=2)
    assert up.status_code == 200
    stale = await push(client, "merchant", "m1", {"v": 0}, version=1)
    assert stale.status_code == 409
    assert stale.json() == {"accepted": False, "reason": "stale_version", "current_version": 2}
    from app.store import get_store
    assert (await get_store().get_context("merchant", "m1")) == (2, {"v": 2})   # same-version push changed nothing


async def test_same_version_can_be_configured_to_409(client, monkeypatch):
    from app.config import reset_settings
    monkeypatch.setenv("SAME_VERSION_STATUS", "409")
    reset_settings()
    await push(client, "merchant", "m1", {"v": 1})
    r = await push(client, "merchant", "m1", {"v": 1})
    assert r.status_code == 409 and r.json()["current_version"] == 1


async def test_context_400s(client):
    bad_scope = await client.post("/v1/context", json={"scope": "nope", "context_id": "x", "version": 1, "payload": {}})
    assert bad_scope.status_code == 400 and bad_scope.json()["reason"] == "invalid_scope"
    for body in ({"scope": "merchant", "version": 1, "payload": {}},
                 {"scope": "merchant", "context_id": "x", "version": "abc", "payload": {}},
                 {"scope": "merchant", "context_id": "x", "version": 1, "payload": "str"},
                 {"scope": "merchant", "context_id": "x", "version": True, "payload": {}}):
        r = await client.post("/v1/context", json=body)
        assert r.status_code == 400 and r.json()["reason"] == "malformed" and r.json()["accepted"] is False
    r = await client.post("/v1/context", content=b"{not json")
    assert r.status_code == 400 and r.json()["reason"] == "malformed"


async def test_unknown_fields_are_ignored(client):
    r = await client.post("/v1/context", json={"scope": "customer", "context_id": "c1", "version": 1, "payload": {},
                                               "delivered_at": NOW_ISO, "extra_field": [1, 2]})
    assert r.status_code == 200


async def test_payload_too_large_is_400_not_crash(client):
    big = {"scope": "merchant", "context_id": "big", "version": 1, "payload": {"blob": "x" * (600 * 1024)}}
    r = await client.post("/v1/context", json=big)
    assert r.status_code == 400 and r.json()["reason"] == "payload_too_large"


async def test_empty_tick_and_garbage_tick(client):
    assert (await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": []})).json() == {"actions": []}
    assert (await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": ["nope"]})).json() == {"actions": []}
    assert (await client.post("/v1/tick", content=b"garbage")).json() == {"actions": []}
    assert (await client.post("/v1/tick", json={})).json() == {"actions": []}


async def test_reply_unknown_conversation_is_valid(client):
    r = await client.post("/v1/reply", json={"conversation_id": "conv_never_seen", "merchant_id": "m_x",
                                             "customer_id": None, "from_role": "merchant", "message": "Yes please",
                                             "received_at": NOW_ISO, "turn_number": 2})
    assert r.status_code == 200
    j = r.json()
    assert j["action"] == "send" and j["body"].strip() and j["rationale"]


async def test_reply_never_500_on_garbage(client):
    for content in (b"", b"[]", b"{bad"):
        r = await client.post("/v1/reply", content=content)
        assert r.status_code == 200 and r.json()["action"] in ("send", "wait", "end")
    r = await client.post("/v1/reply", json={"message": 123})
    assert r.status_code == 200 and r.json()["action"] in ("send", "wait", "end")


async def test_reply_survives_store_failure(client, fresh_env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(fresh_env, "get_json", boom)
    r = await client.post("/v1/reply", json={"conversation_id": "c", "message": "hello", "from_role": "merchant"})
    assert r.status_code == 200 and r.json()["action"] == "wait"


async def test_tick_survives_store_failure(client, fresh_env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(fresh_env, "mget_contexts", boom)
    r = await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": ["t"]})
    assert r.status_code == 200 and r.json() == {"actions": []}


async def test_healthz_degrades_instead_of_failing(client, fresh_env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(fresh_env, "health", boom)
    r = await client.get("/v1/healthz")
    assert r.status_code == 200 and r.json()["status"] == "degraded"


async def test_teardown_wipes_everything(client):
    await push(client, "merchant", "m1", {})
    assert (await client.post("/v1/teardown")).json() == {"ok": True}
    assert (await client.get("/v1/healthz")).json()["contexts_loaded"]["merchant"] == 0
    r = await push(client, "merchant", "m1", {}, version=1)      # version counter restarts too
    assert r.status_code == 200


async def test_debug_endpoint_hidden_without_token(client):
    assert (await client.get("/v1/_debug?token=x")).status_code == 404
