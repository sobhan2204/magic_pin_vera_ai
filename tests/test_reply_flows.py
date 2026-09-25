import re

from conftest import NOW_ISO, push, push_seed

MID = "m_001_drmeera_dentist_delhi"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about", "are you sure", "just to confirm whether"]
ACTIONING = ["done", "here's", "here is", "sending", "drafted", "draft", "confirm", "next", "live", "scheduled", "booked"]


async def reply(client, conv, msg, turn=2, mid=MID, cid=None, role="merchant"):
    r = await client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                             "from_role": role, "message": msg, "received_at": NOW_ISO,
                                             "turn_number": turn})
    assert r.status_code == 200
    return r.json()


async def test_auto_reply_hell_across_four_conversation_ids(client):
    msg = "Thank you for contacting us! Our team will respond shortly."
    actions = []
    for i in range(1, 5):
        actions.append(await reply(client, f"conv_auto_{i}", msg, i + 1))
    assert actions[0]["action"] == "send" and "auto" in actions[0]["body"].lower()   # one owner-directed line
    assert actions[1]["action"] == "end"                                              # second sighting: stop
    assert all(a["action"] == "end" for a in actions[1:])
    assert "repeatedly" in actions[1]["rationale"]


async def test_intent_transition_switches_to_action_mode(client):
    a = await reply(client, "conv_intent_1", "Ok lets do it. Whats next?", 2)
    assert a["action"] == "send"
    low = a["body"].lower()
    assert any(w in low for w in ACTIONING) and not any(q in low for q in QUALIFYING)
    assert "?" not in a["body"]


async def test_action_mode_uses_pending_offer_from_tick(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    acts = (await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": [t["id"]]})).json()["actions"]
    conv = acts[0]["conversation_id"]
    a = await reply(client, conv, "Yes please send the abstract", 2)
    assert "abstract" in a["body"].lower() and "patient" in a["body"].lower()


async def test_hostile_with_stop_ends_and_suppresses(client, dataset):
    a = await reply(client, "conv_hostile", "Stop messaging me. This is useless spam.", 2)
    assert a["action"] == "end"
    again = await reply(client, "conv_hostile_2", "hello?", 2)          # opted out: stays silent
    assert again["action"] == "end"


async def test_hostile_then_gst_keeps_conversation_open(client):
    a = await reply(client, "c1", "Why are you bothering me. This is useless.", 2)
    assert a["action"] == "send" and "sorry" in a["body"].lower()
    b = await reply(client, "c1", "Btw can you also help me with my GST filing this month?", 3)
    assert b["action"] == "send" and "outside" in b["body"].lower()
    assert b["body"] != a["body"]


async def test_hostility_repeated_ends(client):
    await reply(client, "c1", "this is a scam", 2)
    b = await reply(client, "c1", "you are idiots", 3)
    assert b["action"] == "end"


async def test_later_waits(client):
    a = await reply(client, "c1", "not now, maybe next week", 2)
    assert a == {"action": "wait", "wait_seconds": 86400, "rationale": a["rationale"]}
    assert (await reply(client, "c2", "I'm busy, call later", 2))["wait_seconds"] == 3600


async def test_turn_cap(client):
    a = await reply(client, "c1", "tell me more about it", 5)
    assert a["action"] == "send" and a["cta"] == "none"
    assert (await reply(client, "c1", "and more?", 6))["action"] == "end"


async def test_never_repeats_a_body_in_the_same_conversation(client):
    seen = set()
    for i in range(3):
        a = await reply(client, "c_rep", "What does it cost?", 2 + i)
        if a["action"] == "send":
            assert a["body"] not in seen
            seen.add(a["body"])
        else:
            assert a["action"] == "wait"


async def test_send_never_has_empty_body(client):
    for msg in ["yes", "?", "hmm", "ok", "GST kaise bharun", "tell me", "stop", "kal"]:
        a = await reply(client, f"c_{msg}", msg, 2)
        if a["action"] == "send":
            assert a["body"].strip()
        assert a["rationale"]


async def test_customer_slot_selection_confirms_exact_slot(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_003_recall_due_priya")
    acts = (await client.post("/v1/tick", json={"now": "2026-09-25T09:00:00Z", "available_triggers": [t["id"]]})).json()["actions"]
    assert acts and acts[0]["send_as"] == "merchant_on_behalf"
    a = await reply(client, acts[0]["conversation_id"], "2", 2, mid=MID, cid="c_001_priya_for_m001", role="customer")
    assert a["action"] == "send" and "Thu 6 Nov, 5pm" in a["body"]


async def test_hindi_merchant_gets_code_mixed_replies(client, dataset):
    await push(client, "merchant", MID, next(m for m in dataset["merchants"] if m["merchant_id"] == MID))
    a = await reply(client, "conv_hi", "Ok lets do it", 2)
    assert re.search(r"\b(main|aap|kar|doon)\b", a["body"].lower())
    low = a["body"].lower()
    assert any(w in low for w in ACTIONING) and not any(q in low for q in QUALIFYING)
