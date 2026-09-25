"""Prompt shape/size, draft-cache determinism, and the LLM reply path."""
import json
import re
import time

import httpx
import pytest
import respx

from app.compose import ComposeEnv, compose_message, draft_cache_key
from app.config import get_settings, reset_settings
from app.humanize import parse_dt
from app.llm.client import LLMResult
from app.llm.router import Router
from app.normalize import normalize_trigger
from app.playbooks import get_playbook
from app.prompts import (MAX_FACTS, _EX_CUSTOMER, _EX_MERCHANT, build_repair_messages, build_reply_messages,
                         build_writer_messages, select_facts)
from app.replies.state import load_state, mkey, save_state
from app.resolver import build_factsheet
from conftest import ROOT, NOW_ISO
from test_llm_router import GROQ, completion, msg

NOW = parse_dt("2026-04-26T10:35:00Z")
MID = "m_001_drmeera_dentist_delhi"


def sheet(dataset, tid, now=NOW):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == tid)
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    c = next((x for x in dataset["customers"] if x["customer_id"] == t.get("customer_id")), None)
    trig = normalize_trigger(tid, 1, t)
    return trig, build_factsheet(trig, m, dataset["categories"][m["category_slug"]], c, now)


# ---- prompts ------------------------------------------------------------------------------------------------------
def test_writer_prompt_is_compact_and_closed_world_for_every_seed_trigger(dataset):
    for t in dataset["trigger_seeds"]:
        trig, fs = sheet(dataset, t["id"])
        msgs = build_writer_messages(fs, get_playbook(trig.kind), trig)
        chars = sum(len(m["content"]) for m in msgs)
        assert chars / 3.5 <= 1500, (t["id"], chars)                      # spec: <= ~1,500 tokens
        system, user = msgs[0]["content"], msgs[1]["content"]
        assert "Use ONLY" in system.replace("use ONLY", "Use ONLY") or "ONLY the numbered FACTS" in system
        assert "FACTS" in user and "F1 (lead)" in user and user.count("STYLE EXAMPLE") == 1
        assert user.count("\nF") <= MAX_FACTS + 2                       # facts + the example's inline F1/F2
        assert (fs.salutation in user) and "Objective:" in user and "CTA:" in user


def test_prompt_carries_voice_taboos_language_and_customer_framing(dataset):
    trig, fs = sheet(dataset, "trg_001_research_digest_dentists")
    user = build_writer_messages(fs, get_playbook(trig.kind), trig)[1]["content"]
    assert "NEVER use: guaranteed" in user and "peer_clinical" in user and "Hindi-English" in user
    assert 'start the message with it): "Dr. Meera"' in user
    trig, fs = sheet(dataset, "trg_003_recall_due_priya", parse_dt("2026-09-25T09:00:00Z"))
    user = build_writer_messages(fs, get_playbook(trig.kind), trig)[1]["content"]
    assert 'Open exactly with: "Hi Priya, Dr. Meera\'s Dental Clinic here."' in user
    assert "Reply 1 for Wed, 2 for Thu" in user and "multi_choice_slot" in user


def test_facts_are_capped_lead_first_and_include_only_real_facts(dataset):
    trig, fs = sheet(dataset, "trg_001_research_digest_dentists")
    chosen = select_facts(fs, get_playbook(trig.kind))
    assert chosen[0].key == "hook" and len(chosen) <= MAX_FACTS
    assert {f.id for f in chosen} <= fs.ids()


def test_style_examples_are_original_not_copied_from_case_studies():
    cases = (ROOT / "examples" / "case-studies.md").read_text(encoding="utf-8").lower()
    bodies = [b for _, b in _EX_MERCHANT.values()] + [_EX_CUSTOMER[1]]
    for body in bodies:
        words_ = re.findall(r"[a-z0-9₹%]+", body.lower())
        for i in range(len(words_) - 5):                                  # no 6-word run may appear in the case studies
            run = " ".join(words_[i:i + 6])
            assert run not in " ".join(re.findall(r"[a-z0-9₹%]+", cases)), run


def test_repair_prompt_lists_exact_violations_and_asks_for_variety_on_duplicates():
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    r = build_repair_messages(base, {"body": "b"}, ["number '57' is not in the facts", "body is a duplicate/near-duplicate of a previous message"])
    assert r[:2] == base and r[2]["role"] == "assistant" and "Fix ONLY these problems" in r[3]["content"]
    assert "'57'" in r[3]["content"] and "lead with a different fact" in r[3]["content"]


# ---- draft cache: determinism ---------------------------------------------------------------------------------------
async def test_draft_cache_returns_identical_message_without_calling_the_llm_again(dataset, fresh_env, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    trig, fs = sheet(dataset, "trg_004_perf_dip_bharat")
    calls = []
    good = ("Dr. Bharat, your calls are down 50% over the last 7 days (your usual is 12). "
            "Kya main dekh loon ki kya badla hai aur ek fix suggest kar doon?")

    async def fake_chat(**kw):
        calls.append(kw)
        text = good if len(calls) == 1 else good.replace("Kya main dekh loon", "Kya main check kar loon")
        return LLMResult(text=msg(text), prompt_tokens=300, completion_tokens=60, model=kw["model"])

    router = Router(fresh_env, get_settings(), chat=fake_chat)
    key = draft_cache_key(trig, {"m": 1, "cat": 1}, "2026-04-26")
    env = lambda: ComposeEnv(store=fresh_env, settings=get_settings(), router=router, deadline_at=time.monotonic() + 10, cache_key=key)
    a = await compose_message(env(), trig, fs, [])
    b = await compose_message(env(), trig, fs, [])
    assert a.body == b.body == good and len(calls) == 1 and a.via == "llm" and b.via == "cache:llm"
    # new merchant version / new day / new prompt version -> new key -> fresh composition
    assert key != draft_cache_key(trig, {"m": 2, "cat": 1}, "2026-04-26")
    assert key != draft_cache_key(trig, {"m": 1, "cat": 1}, "2026-04-27")
    assert key != draft_cache_key(trig, {"m": 1, "cat": 2}, "2026-04-26")


async def test_cache_is_not_reused_if_it_would_now_be_a_duplicate(dataset, fresh_env, monkeypatch):
    monkeypatch.setenv("LLM_MODE", "mock")
    reset_settings()
    trig, fs = sheet(dataset, "trg_004_perf_dip_bharat")
    router = Router(fresh_env, get_settings())
    key = draft_cache_key(trig, {"m": 1, "cat": 1}, "d")
    mk = lambda: ComposeEnv(store=fresh_env, settings=get_settings(), router=router, deadline_at=time.monotonic() + 5, cache_key=key)
    first = await compose_message(mk(), trig, fs, [])
    again = await compose_message(mk(), trig, fs, [first.body])
    assert again.body != first.body or again.violations                   # never silently repeats a sent body


# ---- LLM reply path ---------------------------------------------------------------------------------------------------
@pytest.fixture
def live_reply(monkeypatch, fresh_env):
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _reply(client, text, conv="conv_q", turn=2):
    r = await client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": MID, "customer_id": None,
                                             "from_role": "merchant", "message": text, "received_at": NOW_ISO, "turn_number": turn})
    assert r.status_code == 200
    return r.json()


async def _seed(client, dataset, fresh_env):
    from conftest import push
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    await push(client, "merchant", MID, next(m for m in dataset["merchants"] if m["merchant_id"] == MID))
    st = await load_state(fresh_env, mkey(MID, ""))
    st["pending"] = {"kind": "research_digest", "deliverable": "the abstract", "hook": "a new JIDA study", "trigger_id": "t"}
    await save_state(fresh_env, mkey(MID, ""), st)


async def test_question_gets_a_grounded_llm_answer(live_reply, client, dataset, fresh_env):
    await _seed(client, dataset, fresh_env)
    answer = "Your click-through rate is 2.1% against a peer average of 3%, so there is room to grow. Want me to draft a fresh Google post?"
    route = live_reply.post(GROQ).mock(return_value=completion(msg(answer)))
    out = await _reply(client, "How is my click-through doing?")
    assert out["action"] == "send" and out["body"] == answer and route.call_count == 1
    prompt = json.loads(route.calls[0].request.content)["messages"][1]["content"]
    assert "Them: How is my click-through doing?" in prompt and "FACTS:" in prompt and "2.1%" in prompt


async def test_ungrounded_llm_answer_is_rejected_for_the_deterministic_reply(live_reply, client, dataset, fresh_env):
    await _seed(client, dataset, fresh_env)
    live_reply.post(GROQ).mock(return_value=completion(msg("It will cost you ₹9,999 per month and guarantees 300 new patients.")))
    out = await _reply(client, "What will this cost me?")
    assert out["action"] == "send" and "9,999" not in out["body"] and "300" not in out["body"]
    assert "Reply YES" in out["body"]


async def test_reply_survives_llm_outage_and_timeouts(live_reply, client, dataset, fresh_env):
    await _seed(client, dataset, fresh_env)
    live_reply.post(GROQ).mock(side_effect=httpx.ConnectError("down"))
    out = await _reply(client, "what does the study say?")
    assert out["action"] == "send" and out["body"].strip()


async def test_llm_is_not_used_for_opt_out_hostile_or_commitment(live_reply, client, dataset, fresh_env):
    await _seed(client, dataset, fresh_env)
    route = live_reply.post(GROQ).mock(return_value=completion(msg("should never be used at all here")))
    for text, conv in (("yes please go ahead", "c1"), ("this is useless", "c2"), ("stop messaging me", "c3"),
                       ("can you help with my GST", "c4")):
        await _reply(client, text, conv)
    assert route.call_count == 0
