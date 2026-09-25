"""Opt-in tests that make REAL Groq calls (a handful, ~10k tokens total): pytest -m live

Requires GROQ_API_KEY in the environment. They never run in the default suite.
"""
import os

import pytest

from app.config import get_settings, reset_settings
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.playbooks import get_playbook
from app.resolver import build_factsheet
from app.verifier import verify
from app.writer import write_with_llm
from app.llm.router import Router
import time

pytestmark = pytest.mark.live
NOW = parse_dt("2026-04-26T10:35:00Z")


@pytest.fixture
def live_env(monkeypatch):
    if not os.environ.get("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY not set")
    monkeypatch.setenv("LLM_MODE", "live")
    reset_settings()


@pytest.mark.parametrize("tid", ["trg_001_research_digest_dentists", "trg_004_perf_dip_bharat",
                                 "trg_010_ipl_match_delhi", "trg_018_supply_atorvastatin_recall"])
async def test_real_model_writes_a_verified_message(live_env, dataset, fresh_env, tid):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == tid)
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    trig = normalize_trigger(tid, 1, t)
    fs = build_factsheet(trig, m, dataset["categories"][m["category_slug"]], None, NOW)
    router = Router(fresh_env, get_settings())
    out = await write_with_llm(router, trig, fs, get_playbook(trig.kind), [], time.monotonic() + 20)
    print(out)
    # the LLM may be rejected (returns None -> template fallback); when it is accepted it must be fully grounded
    if out is not None:
        assert verify(out, fs, []) == []
    state = await router.state()
    print(state)
