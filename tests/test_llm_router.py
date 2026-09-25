"""LLM layer: quota accounting, failover, cooling, deadlines, structured-output request shape (Groq mocked with respx)."""
import asyncio
import json
import time

import httpx
import pytest
import respx

from app.config import get_settings, reset_settings
from app.llm.client import LLMError, LLMResult, chat_completion
from app.llm.router import Router, build_targets, estimate_tokens
from app.prompts import MESSAGE_SCHEMA, parse_output
from conftest import NOW_ISO, push_seed

GROQ = "https://api.groq.com/openai/v1/chat/completions"


def completion(text, prompt=400, completion_tokens=90):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                     "usage": {"prompt_tokens": prompt, "completion_tokens": completion_tokens}},
                          headers={"x-ratelimit-remaining-tokens": "7000"})


def msg(body, cta="open_ended", used=("F1",)):
    return json.dumps({"body": body, "cta": cta, "facts_used": list(used), "rationale_note": ""})


@pytest.fixture
def live(monkeypatch, fresh_env):
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    reset_settings()
    with respx.mock(assert_all_called=False) as router:
        yield router


def router_for(store):
    return Router(store, get_settings())


# ---- targets / estimates / schema ------------------------------------------------------------------------
def test_target_chain_and_alt_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    assert [t.model for t in build_targets(get_settings())] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert all(t.reasoning_effort == "low" and t.json_mode == "schema" for t in build_targets(get_settings()))
    monkeypatch.setenv("ALT_BASE_URL", "https://alt.example/v1")
    monkeypatch.setenv("ALT_API_KEY", "a")
    monkeypatch.setenv("ALT_MODEL", "alt-model")
    monkeypatch.setenv("ALT_PROVIDER_NAME", "alt")
    reset_settings()
    ts = build_targets(get_settings())
    assert ts[-1].model == "alt-model" and ts[-1].json_mode == "object" and ts[-1].reasoning_effort is None
    monkeypatch.delenv("GROQ_API_KEY")
    reset_settings()
    assert [t.model for t in build_targets(get_settings())] == ["alt-model"]


def test_token_estimate_matches_spec():
    assert estimate_tokens([{"content": "x" * 350}], 400) == 500


def test_strict_schema_is_valid_for_groq():
    assert set(MESSAGE_SCHEMA["required"]) == set(MESSAGE_SCHEMA["properties"])
    assert MESSAGE_SCHEMA["additionalProperties"] is False


def test_parse_output_variants():
    assert parse_output('```json\n{"body": "hi there", "cta": "open_ended"}\n```')["body"] == "hi there"
    assert parse_output('noise {"body": "x  y", "cta": "weird"} tail') == {"body": "x y", "cta": "open_ended", "facts_used": [],
                                                                            "rationale_note": ""}
    for bad in ("", "no json", '{"body": ""}', '{"body": 3}', "[1]"):
        with pytest.raises(ValueError):
            parse_output(bad)


# ---- client request shape ------------------------------------------------------------------------------------
async def test_request_uses_structured_outputs_low_reasoning_seed_temperature_zero(live):
    route = live.post(GROQ).mock(return_value=completion(msg("hello")))
    res = await chat_completion(base_url="https://api.groq.com/openai/v1", api_key="k", model="openai/gpt-oss-120b",
                                messages=[{"role": "user", "content": "x"}], max_tokens=450, seed=7, timeout_s=5,
                                schema=MESSAGE_SCHEMA, json_mode="schema", reasoning_effort="low")
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["strict"] is True
    assert sent["reasoning_effort"] == "low" and sent["seed"] == 7 and sent["temperature"] == 0 and sent["max_tokens"] == 450
    assert route.calls[0].request.headers["authorization"] == "Bearer k"
    assert (res.prompt_tokens, res.completion_tokens) == (400, 90) and res.headers["x-ratelimit-remaining-tokens"] == "7000"


@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (500, "server"), (503, "server"), (401, "auth"), (400, "bad_request")])
async def test_error_classification(live, status, kind):
    live.post(GROQ).mock(return_value=httpx.Response(status, text="nope", headers={"retry-after": "3"}))
    with pytest.raises(LLMError) as e:
        await chat_completion(base_url="https://api.groq.com/openai/v1", api_key="k", model="m", messages=[], max_tokens=5,
                              seed=1, timeout_s=5)
    assert e.value.kind == kind


async def test_timeout_and_garbage_envelope(live):
    live.post(GROQ).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError) as e:
        await chat_completion(base_url="https://api.groq.com/openai/v1", api_key="k", model="m", messages=[], max_tokens=5, seed=1, timeout_s=1)
    assert e.value.kind == "timeout"
    live.post(GROQ).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(LLMError) as e:
        await chat_completion(base_url="https://api.groq.com/openai/v1", api_key="k", model="m", messages=[], max_tokens=5, seed=1, timeout_s=1)
    assert e.value.kind == "bad_output"


# ---- quota accounting ---------------------------------------------------------------------------------------------
async def test_reservation_is_atomic_and_respects_all_four_limits(fresh_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("MODEL_RPM", "10")       # 9 with the 10% margin
    monkeypatch.setenv("MODEL_TPM", "1000")     # 900
    reset_settings()
    r = router_for(fresh_env)
    assert r.limits == {"rpm": 9, "tpm": 900, "rpd": 900, "tpd": 180000}
    leases = await asyncio.gather(*(r.acquire(300) for _ in range(12)))
    granted = [l for l in leases if l]
    # 900 TPM per model / 300 = 3 per model, two models -> 6; the rest are skipped WITHOUT calling anything
    assert len(granted) == 6
    assert sorted(l.target.model for l in granted).count("openai/gpt-oss-120b") == 3
    st = await r.state()
    assert st["models"]["openai/gpt-oss-120b"]["minute"] == {"req": 3, "tok": 900}


async def test_daily_limits_and_adjustment_to_actual_usage(fresh_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("MODEL_TPD", "1000")
    reset_settings()
    r = router_for(fresh_env)
    lease = await r.acquire(800)
    assert lease and lease.target.model == "openai/gpt-oss-120b"
    await fresh_env.quota_adjust(lease.target.model, 500 - 800, time.time())         # actual usage was 500
    assert (await r.state())["models"]["openai/gpt-oss-120b"]["day"]["tok"] == 500
    second = await r.acquire(300)                                                     # 500+300 <= 900 -> still primary
    assert second.target.model == "openai/gpt-oss-120b"
    third = await r.acquire(300)                                                      # primary daily budget spent -> secondary
    assert third.target.model == "openai/gpt-oss-20b"


async def test_release_returns_unused_reservation(fresh_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    r = router_for(fresh_env)
    lease = await r.acquire(500)
    await r.release(lease)
    assert (await r.state())["models"][lease.target.model]["minute"] == {"req": 0, "tok": 0}


# ---- failover / cooling ------------------------------------------------------------------------------------------
async def test_429_then_secondary_then_cooling(live, fresh_env):
    def responder(request):
        model = json.loads(request.content)["model"]
        return httpx.Response(429, text="rate limited") if model.endswith("120b") else completion(msg("from secondary"))
    route = live.post(GROQ).mock(side_effect=responder)
    r = router_for(fresh_env)
    lease = await r.acquire(500)
    out, res = await r.run(lease, [{"role": "user", "content": "x"}], max_tokens=100, validate=parse_output,
                           deadline_at=time.monotonic() + 10)
    assert out["body"] == "from secondary" and res.model == "openai/gpt-oss-20b"
    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]
    assert (await r.state())["models"]["openai/gpt-oss-120b"]["cooling_s"] > 0
    # while cooling the primary is skipped without any HTTP call
    lease = await r.acquire(500)
    assert lease.target.model == "openai/gpt-oss-20b"


async def test_invalid_json_cools_model_and_fails_over(live, fresh_env):
    def responder(request):
        model = json.loads(request.content)["model"]
        return completion("not json at all") if model.endswith("120b") else completion(msg("clean answer"))
    live.post(GROQ).mock(side_effect=responder)
    r = router_for(fresh_env)
    out, res = await r.run(await r.acquire(500), [{"role": "user", "content": "x"}], max_tokens=100, validate=parse_output,
                           deadline_at=time.monotonic() + 10)
    assert out["body"] == "clean answer" and res.model.endswith("20b")


async def test_everything_failing_returns_none_never_raises(live, fresh_env):
    live.post(GROQ).mock(return_value=httpx.Response(500))
    r = router_for(fresh_env)
    got = await r.run(await r.acquire(500), [{"role": "user", "content": "x"}], max_tokens=100, validate=parse_output,
                      deadline_at=time.monotonic() + 10)
    assert got is None
    assert await r.acquire(500) is None                       # both models cooling -> caller uses the template


async def test_long_retry_after_cools_for_that_long(live, fresh_env):
    live.post(GROQ).mock(return_value=httpx.Response(429, headers={"retry-after": "120"}))
    r = router_for(fresh_env)
    await r.run(await r.acquire(500), [{"role": "user", "content": "x"}], max_tokens=100, validate=parse_output,
                deadline_at=time.monotonic() + 10)
    assert (await r.state())["models"]["openai/gpt-oss-120b"]["cooling_s"] >= 100


async def test_deadline_stops_calls_and_refunds(live, fresh_env):
    route = live.post(GROQ).mock(return_value=completion(msg("late")))
    r = router_for(fresh_env)
    lease = await r.acquire(500)
    got = await r.run(lease, [{"role": "user", "content": "x"}], max_tokens=100, validate=parse_output,
                      deadline_at=time.monotonic() + 0.2)
    assert got is None and route.call_count == 0
    assert (await r.state())["models"][lease.target.model]["minute"] == {"req": 0, "tok": 0}


async def test_no_key_means_no_targets_and_no_calls(fresh_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("LLM_MODE", "live")
    reset_settings()
    r = router_for(fresh_env)
    assert not r.enabled and await r.acquire(100) is None


# ---- end to end through /v1/tick ----------------------------------------------------------------------------------
GOOD = ("Dr. Bharat, your calls are down 50% over the last 7 days (your usual is 12). "
        "Kya main dekh loon ki kya badla hai aur ek fix suggest kar doon?")
BAD_NUMBER = GOOD.replace("Kya main", "That is 57% worse than peers. Kya main")


async def tick_one(client, dataset, tid="trg_004_perf_dip_bharat"):
    t, _ = await push_seed(client, dataset, tid)
    r = await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": [t["id"]]})
    assert r.status_code == 200
    return r.json()["actions"]


async def test_valid_llm_message_is_used(live, client, dataset):
    route = live.post(GROQ).mock(return_value=completion(msg(GOOD)))
    acts = await tick_one(client, dataset)
    assert acts[0]["body"] == GOOD and route.call_count == 1
    assert json.loads(route.calls[0].request.content)["model"] == "openai/gpt-oss-120b"


async def test_hallucinated_number_is_repaired_once(live, client, dataset):
    seen_prompts = []
    def responder(request):
        body = json.loads(request.content)
        seen_prompts.append(body["messages"])
        return completion(msg(BAD_NUMBER if len(seen_prompts) == 1 else GOOD))
    live.post(GROQ).mock(side_effect=responder)
    acts = await tick_one(client, dataset)
    assert acts[0]["body"] == GOOD and len(seen_prompts) == 2
    repair = seen_prompts[1][-1]["content"]
    assert "Fix ONLY these problems" in repair and "'57'" in repair
    assert seen_prompts[1][-2]["role"] == "assistant"


async def test_failed_repair_falls_back_to_verified_template(live, client, dataset):
    route = live.post(GROQ).mock(return_value=completion(msg(BAD_NUMBER)))
    acts = await tick_one(client, dataset)
    assert route.call_count == 2                                    # original + exactly one repair
    assert "57" not in acts[0]["body"] and "down 50%" in acts[0]["body"]


async def test_llm_message_that_ignores_the_hook_is_rejected(live, client, dataset):
    off_hook = "Dr. Bharat, Kya aap chahenge ki main aapke profile ke liye ek naya post draft kar doon aur bhej doon?"
    live.post(GROQ).mock(return_value=completion(msg(off_hook)))
    acts = await tick_one(client, dataset)
    assert "down 50%" in acts[0]["body"]


async def test_429_on_primary_uses_secondary_through_tick(live, client, dataset):
    def responder(request):
        return httpx.Response(429) if json.loads(request.content)["model"].endswith("120b") else completion(msg(GOOD))
    live.post(GROQ).mock(side_effect=responder)
    acts = await tick_one(client, dataset)
    assert acts[0]["body"] == GOOD


async def test_all_models_down_still_returns_valid_grounded_message(live, client, dataset):
    live.post(GROQ).mock(return_value=httpx.Response(503))
    acts = await tick_one(client, dataset)
    assert len(acts) == 1 and "down 50%" in acts[0]["body"]


async def test_deliberately_broken_key_never_errors(live, client, dataset):
    route = live.post(GROQ).mock(return_value=httpx.Response(401, text="invalid api key"))
    ids = []
    for tid in ("trg_004_perf_dip_bharat", "trg_006_festival_diwali", "trg_010_ipl_match_delhi", "trg_018_supply_atorvastatin_recall"):
        t, _ = await push_seed(client, dataset, tid)
        ids.append(t["id"])
    r = await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": ids})
    assert r.status_code == 200 and len(r.json()["actions"]) == 4
    assert route.call_count <= 2 * 4                                # cooling stops the hammering after the first failures


async def test_quota_exhaustion_skips_llm_without_calling(live, client, dataset, monkeypatch):
    monkeypatch.setenv("MODEL_RPM", "2")                            # 1 request per model per minute after margin
    reset_settings()
    route = live.post(GROQ).mock(side_effect=lambda req: completion(msg(GOOD)))
    ids = []
    for tid in ("trg_004_perf_dip_bharat", "trg_006_festival_diwali", "trg_010_ipl_match_delhi"):
        t, _ = await push_seed(client, dataset, tid)
        ids.append(t["id"])
    r = await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": ids})
    assert len(r.json()["actions"]) == 3 and route.call_count <= 2


async def test_slow_llm_cannot_break_the_tick_deadline(live, client, dataset, monkeypatch):
    monkeypatch.setenv("TICK_DEADLINE_S", "7")
    monkeypatch.setenv("LLM_CALL_TIMEOUT_S", "12")
    reset_settings()

    async def slow(request):
        await asyncio.sleep(30)
        return completion(msg(GOOD))
    live.post(GROQ).mock(side_effect=slow)
    t0 = time.monotonic()
    acts = await tick_one(client, dataset)
    assert time.monotonic() - t0 < 7.5
    assert len(acts) == 1 and "down 50%" in acts[0]["body"]


async def test_mock_mode_makes_no_network_calls(client, dataset, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("GROQ_API_KEY", "would-be-used")
    reset_settings()
    with respx.mock(assert_all_called=False) as m:
        route = m.post(GROQ).mock(return_value=completion(msg(GOOD)))
        acts = await tick_one(client, dataset)
    assert len(acts) == 1 and route.call_count == 0
