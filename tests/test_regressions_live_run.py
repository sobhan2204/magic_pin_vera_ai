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
