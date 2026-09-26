"""Regression tests for problems found in the live run against Vercel (each test is named after the numbered item)."""
import copy

from app.replies.state import ckey, load_state, mkey
from conftest import NOW_ISO, push, push_seed

MID = "m_001_drmeera_dentist_delhi"


async def tick(client, ids, now=NOW_ISO):
    r = await client.post("/v1/tick", json={"now": now, "available_triggers": ids})
    assert r.status_code == 200
    return r.json()["actions"]


def customer(cid, name="Ishaan", scope=("recall_reminders",)):
    return {"customer_id": cid, "merchant_id": MID, "identity": {"name": name, "language_pref": "english"},
            "relationship": {"last_visit": "2026-02-01", "first_visit": "2025-09-01", "visits_total": 3}, "state": "lapsed_soft",
            "consent": {"opted_in_at": "2025-09-01", "scope": list(scope)}}


def recall(tid, cid, urgency=3):
    return {"id": tid, "scope": "customer", "kind": "recall_due", "merchant_id": MID, "customer_id": cid,
            "payload": {"service_due": "6_month_cleaning"}, "urgency": urgency, "suppression_key": f"recall:{tid}",
            "expires_at": "2027-01-01T00:00:00Z"}


# ---- item 6: customer sends are not blocked by the MERCHANT's open conversation --------------------------------------------
async def test_item6_customer_recall_sent_while_merchant_conversation_is_open(client, dataset, fresh_env):
    t, m = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    first = await tick(client, [t["id"]])
    assert len(first) == 1 and first[0]["customer_id"] is None                      # merchant now has an unanswered nudge
    assert (await load_state(fresh_env, mkey(MID, "")))["unanswered"] == 1

    await push(client, "customer", "c_new_recall", customer("c_new_recall"))
    await push(client, "trigger", "trg_recall_new", recall("trg_recall_new", "c_new_recall"))
    acts = await tick(client, ["trg_recall_new"], now="2026-04-26T10:40:00Z")
    assert len(acts) == 1 and acts[0]["customer_id"] == "c_new_recall" and acts[0]["send_as"] == "merchant_on_behalf"
    # sending to the customer did NOT count as a second unanswered nudge to the merchant
    assert (await load_state(fresh_env, mkey(MID, "")))["unanswered"] == 1
    assert (await load_state(fresh_env, ckey("c_new_recall")))["unanswered"] == 1

    # merchant-facing restraint still applies to the merchant ...
    twin = {**t, "id": "trg_twin_m", "suppression_key": "research:twin_m", "urgency": 2}
    await push(client, "trigger", "trg_twin_m", twin)
    assert await tick(client, ["trg_twin_m"], now="2026-04-26T10:45:00Z") == []
    # ... and customer-facing restraint applies to that customer's own open conversation
    await push(client, "trigger", "trg_recall_again", recall("trg_recall_again", "c_new_recall", urgency=2))
    assert await tick(client, ["trg_recall_again"], now="2026-04-26T10:50:00Z") == []
    # but a different customer is unaffected
    await push(client, "customer", "c_other", customer("c_other", "Riya"))
    await push(client, "trigger", "trg_recall_other", recall("trg_recall_other", "c_other"))
    other = await tick(client, ["trg_recall_other"], now="2026-04-26T10:55:00Z")
    assert len(other) == 1 and other[0]["customer_id"] == "c_other"


async def test_item6_urgent_merchant_triggers_still_break_through(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    await tick(client, [t["id"]])
    urgent = {**t, "id": "trg_urgent_m", "suppression_key": "research:urgent_m", "urgency": 4}
    await push(client, "trigger", "trg_urgent_m", urgent)
    assert len(await tick(client, ["trg_urgent_m"], now="2026-04-26T11:00:00Z")) == 1


# ---- item 1: an updated merchant field must be the supporting fact ------------------------------------------------------------
M42 = "m_042_pooja_gym_pune"


def m42(dataset, views=None):
    m = copy.deepcopy(next(x for x in dataset["merchants"] if x["merchant_id"] == M42))
    if views:
        m["performance"]["views"] = views
    return m


def renewal_trigger(tid="trg_inj_renewal_m_042"):
    return {"id": tid, "scope": "merchant", "kind": "renewal_due", "source": "internal", "merchant_id": M42, "customer_id": None,
            "payload": {"days_remaining": 9, "plan": "Pro", "renewal_amount": 4999}, "urgency": 4,
            "suppression_key": f"inj_renew:{M42}", "expires_at": "2026-12-01T00:00:00Z"}


async def test_item1_store_keeps_previous_merchant_payload(fresh_env, dataset):
    await fresh_env.put_context("merchant", M42, 1, m42(dataset))
    assert (await fresh_env.mget_previous("merchant", [M42])) == [None]
    await fresh_env.put_context("merchant", M42, 2, m42(dataset, 6743))
    assert (await fresh_env.mget_previous("merchant", [M42]))[0]["performance"]["views"] == 5509
    await fresh_env.put_context("merchant", M42, 3, m42(dataset, 7000))
    assert (await fresh_env.mget_previous("merchant", [M42]))[0]["performance"]["views"] == 6743     # only the immediate predecessor
    await fresh_env.put_context("merchant", M42, 3, m42(dataset, 1))                                  # same version: no change
    assert (await fresh_env.mget_previous("merchant", [M42]))[0]["performance"]["views"] == 6743
    assert (await fresh_env.mget_previous("category", ["x"])) == [None]


async def test_item1_m042_renewal_uses_the_updated_views_not_ctr(client, dataset):
    for slug, cat in dataset["categories"].items():
        await push(client, "category", slug, cat)
    await push(client, "merchant", M42, m42(dataset), version=1)
    await push(client, "merchant", M42, m42(dataset, 6743), version=2)
    await push(client, "trigger", "trg_inj_renewal_m_042", renewal_trigger())
    acts = await tick(client, ["trg_inj_renewal_m_042"])
    assert len(acts) == 1
    body = acts[0]["body"]
    assert "6,743" in body and "5,509" in body                                   # the new value, and what it moved from
    assert "5.9%" not in body and "click-through" not in body.lower()            # the unchanged CTR must not be the support


async def test_item1_changed_facts_lead_in_facts_and_prompt(dataset):
    from app.humanize import parse_dt
    from app.normalize import normalize_trigger
    from app.playbooks import get_playbook
    from app.prompts import select_facts
    from app.resolver import build_factsheet
    trig = normalize_trigger("t", 1, renewal_trigger("t"))
    cat = dataset["categories"]["gyms"]
    fs = build_factsheet(trig, m42(dataset, 6743), cat, None, parse_dt("2026-04-26T10:35:00Z"), prev_merchant=m42(dataset))
    assert fs.text("m.changed") == "Your Google profile now shows 6,743 views over 30 days, up 22.4% from 5,509"
    assert "pehle 5,509 the" in fs.get("m.changed").hi
    keys = [f.key for f in select_facts(fs, get_playbook("renewal_due"))]
    assert keys[:2] == ["hook", "m.changed"]                                     # the LLM sees the change right after the hook
    fs2 = build_factsheet(trig, m42(dataset), cat, None, parse_dt("2026-04-26T10:35:00Z"), prev_merchant=m42(dataset))
    assert fs2.get("m.changed") is None                                          # nothing changed -> no fact
    fs3 = build_factsheet(trig, m42(dataset, 6743), cat, None, parse_dt("2026-04-26T10:35:00Z"))
    assert fs3.get("m.changed") is None                                          # first version -> no fact


async def test_item1_llm_draft_that_ignores_the_updated_field_is_rejected(client, dataset, monkeypatch):
    import json
    import httpx
    import respx
    from app.config import reset_settings
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reset_settings()
    ctr_draft = ("Pooja, aapka Pro plan sirf 9 din baaki hai aur renewal ₹4,999 hai. Aapka click-through rate 5.9% hai, "
                 "jo peer average 4.5% se upar hai. Aap Reply YES kar dijiye, main renewal set kar doon.")
    with respx.mock(assert_all_called=False) as router:
        route = router.post("https://api.groq.com/openai/v1/chat/completions").mock(return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"body": ctr_draft, "cta": "binary_yes_stop", "facts_used": ["F1"],
                                                            "rationale_note": ""})}}], "usage": {"prompt_tokens": 500, "completion_tokens": 80}}))
        for slug, cat in dataset["categories"].items():
            await push(client, "category", slug, cat)
        await push(client, "merchant", M42, m42(dataset), version=1)
        await push(client, "merchant", M42, m42(dataset, 6743), version=2)
        await push(client, "trigger", "trg_inj_renewal_m_042", renewal_trigger())
        acts = await tick(client, ["trg_inj_renewal_m_042"])
    assert route.call_count == 2                                                 # first draft rejected, one repair, then the template
    assert "6,743" in acts[0]["body"] and "5.9%" not in acts[0]["body"]
