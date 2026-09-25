"""Phase 6: batch composition (several decisions per LLM call) - opt-in via LLM_BATCH_SIZE."""
import json

import httpx
import pytest
import respx

from app.compose import compose_template
from app.config import reset_settings
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.playbooks import get_playbook
from app.prompts import BATCH_IDS, build_batch_messages, build_writer_messages, parse_batch
from app.resolver import build_factsheet
from conftest import NOW_ISO, push_seed
from test_llm_router import GROQ, completion

IDS = ["trg_004_perf_dip_bharat", "trg_010_ipl_match_delhi", "trg_014_seasonal_acquisition_dip_powerhouse"]
NOW = parse_dt(NOW_ISO)


def sheets(dataset):
    out = []
    for tid in IDS:
        t = next(x for x in dataset["trigger_seeds"] if x["id"] == tid)
        m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
        trig = normalize_trigger(tid, 1, t)
        out.append((trig, build_factsheet(trig, m, dataset["categories"][m["category_slug"]], None, NOW)))
    return out


def llm_body(trig, fs):
    """A valid, distinguishable 'LLM' draft: the template text with 'Also,' opening the final (ask) sentence."""
    head, ask = compose_template(trig, fs, []).body.rsplit(". ", 1)
    return f"{head}. Also, {ask[0].lower() + ask[1:]}"


@pytest.fixture
def live(monkeypatch, fresh_env):
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    with respx.mock(assert_all_called=False) as router:
        yield router


async def run_tick(client, dataset):
    for tid in IDS:
        await push_seed(client, dataset, tid)
    r = await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": IDS})
    assert r.status_code == 200
    return {a["trigger_id"]: a for a in r.json()["actions"]}


def test_batch_prompt_is_cheaper_per_message_than_single_prompts(dataset):
    pairs = [(fs, get_playbook(t.kind)) for t, fs in sheets(dataset)]
    single = sum(sum(len(m["content"]) for m in build_writer_messages(fs, pb, t)) for (t, _), (fs, pb) in zip(sheets(dataset), pairs))
    batch = sum(len(m["content"]) for m in build_batch_messages(pairs))
    assert batch < 0.75 * single
    user = build_batch_messages(pairs)[1]["content"]
    assert all(f"=== MESSAGE {BATCH_IDS[i]} ===" in user for i in range(3)) and user.count("STYLE EXAMPLE") == 1


def test_parse_batch():
    ok = parse_batch(json.dumps({"messages": [{"id": "a", "body": "hello there", "cta": "open_ended", "facts_used": ["F1"]},
                                              {"id": "B", "body": "", "cta": "open_ended", "facts_used": []}]}))
    assert list(ok) == ["A"] and ok["A"]["body"] == "hello there"
    for bad in ("", "nope", '{"messages": []}', '{"messages": [{"id": "A"}]}', "[]"):
        with pytest.raises(ValueError):
            parse_batch(bad)


async def test_one_call_serves_the_whole_batch(live, client, dataset, monkeypatch):
    monkeypatch.setenv("LLM_BATCH_SIZE", "3")
    reset_settings()
    parts = sheets(dataset)

    def responder(request):
        sent = json.loads(request.content)
        assert sent["response_format"]["json_schema"]["schema"]["required"] == ["messages"]
        user = sent["messages"][1]["content"]
        # the decisions are batched in score order: answer whichever ids were asked for, in that order
        order = [t for t, _ in sorted(parts, key=lambda p: user.index(p[1].text("hook")))]
        msgs = [{"id": BATCH_IDS[i], "body": llm_body(*next(p for p in parts if p[0].id == t.id)), "cta": "open_ended", "facts_used": ["F1"]}
                for i, t in enumerate(order)]
        return completion(json.dumps({"messages": msgs}), prompt=1500, completion_tokens=300)

    route = live.post(GROQ).mock(side_effect=responder)
    acts = await run_tick(client, dataset)
    assert route.call_count == 1 and len(acts) == 3
    assert all(". Also, " in a["body"] for a in acts.values())


async def test_a_bad_item_falls_back_to_template_without_hurting_the_others(live, client, dataset, monkeypatch):
    monkeypatch.setenv("LLM_BATCH_SIZE", "3")
    reset_settings()
    parts = {t.id: (t, fs) for t, fs in sheets(dataset)}

    def responder(request):
        user = json.loads(request.content)["messages"][1]["content"]
        blocks = sorted(parts.values(), key=lambda p: user.index(p[1].text("hook")))
        msgs = []
        for i, (t, fs) in enumerate(blocks):
            body = llm_body(t, fs)
            if i == 1:
                body = body.replace(". Also,", ". That is 57% worse than peers.")          # hallucinated number
            msgs.append({"id": BATCH_IDS[i], "body": body, "cta": "open_ended", "facts_used": ["F1"]})
        return completion(json.dumps({"messages": msgs}))

    route = live.post(GROQ).mock(side_effect=responder)
    acts = await run_tick(client, dataset)
    assert route.call_count == 1 and len(acts) == 3
    plain = [a for a in acts.values() if ". Also, " not in a["body"]]
    assert len(plain) == 1 and "57" not in plain[0]["body"]


async def test_batch_failure_uses_templates_for_everyone(live, client, dataset, monkeypatch):
    monkeypatch.setenv("LLM_BATCH_SIZE", "4")
    reset_settings()
    live.post(GROQ).mock(return_value=httpx.Response(500))
    acts = await run_tick(client, dataset)
    assert len(acts) == 3 and all(a["body"].strip() for a in acts.values())


async def test_batches_are_split_by_size(live, client, dataset, monkeypatch):
    monkeypatch.setenv("LLM_BATCH_SIZE", "2")
    reset_settings()
    route = live.post(GROQ).mock(return_value=httpx.Response(500))
    await run_tick(client, dataset)
    assert route.call_count >= 2                    # ceil(3/2) groups (500s also trigger failover attempts)


async def test_default_is_one_call_per_message(live, client, dataset):
    route = live.post(GROQ).mock(return_value=completion("{}"))       # unusable output -> 1-2 attempts each, templates used
    acts = await run_tick(client, dataset)
    assert len(acts) == 3 and route.call_count >= 3
