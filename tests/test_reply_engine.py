"""Phase 3: reply engine behaviour tables, merchant-level state, customer flows, action-mode lint."""
import pytest

from app.replies.handler import _T, _render
from app.replies.lint import BANNED, lint_action_body
from app.replies.state import ckey, load_state, mkey, save_state
from conftest import NOW_ISO, push, push_seed

MID = "m_001_drmeera_dentist_delhi"
CID = "c_001_priya_for_m001"
PENDING = {"kind": "research_digest", "deliverable": "the abstract and a patient-ready WhatsApp draft",
           "hook": "JIDA Oct 2026, p.14 carries a new study on 3-month recall", "trigger_id": "t"}


async def reply(client, conv, msg, turn=2, mid=MID, cid=None, role="merchant"):
    r = await client.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                             "from_role": role, "message": msg, "received_at": NOW_ISO, "turn_number": turn})
    assert r.status_code == 200
    return r.json()


async def seed_pending(store, mid=MID, **extra):
    st = await load_state(store, mkey(mid, ""))
    st["pending"] = {**PENDING, **extra}
    await save_state(store, mkey(mid, ""), st)


# (message, expected action, substrings of which one must appear, substrings that must not appear)
TABLE = [
    # commitment -> action mode
    ("Ok lets do it. Whats next?", "send", ["next", "draft", "here's"], ["would you", "do you", "?"]),
    ("yes", "send", ["next", "sending"], ["would you", "?"]),
    ("Yes please", "send", ["next", "sending"], ["how about", "?"]),
    ("go ahead", "send", ["next", "sending"], ["what if"]),
    ("haan ji kar do", "send", ["next step"], ["would you"]),
    ("chalega theek hai", "send", ["next step"], ["do you"]),
    ("I want to join magicpin", "send", ["next"], ["are you sure"]),
    ("Mujhe magicpin judna hai", "send", ["next step"], ["can you tell"]),
    ("sure, send it", "send", ["next"], ["?"]),
    ("proceed", "send", ["next"], ["?"]),
    ("ok let's do it", "send", ["next"], ["?"]),
    # opt-out
    ("Stop messaging me. This is useless spam.", "end", [], []),
    ("not interested", "end", [], []),
    ("band karo", "end", [], []),
    ("yes stop", "end", [], []),
    # later / busy
    ("busy right now", "wait", [], []),
    ("baad mein baat karte hain", "wait", [], []),
    ("not now, maybe next week", "wait", [], []),
    # hostile (no stop) -> apologise, stay open
    ("this is useless", "send", ["sorry"], []),
    ("bakwas hai ye", "send", ["sorry"], []),
    # off topic, nothing pending -> polite decline
    ("can you also help with my GST filing", "send", ["outside"], ["reply yes"]),
    ("I need a loan", "send", ["outside"], []),
    ("Btw what's the weather in Delhi", "send", ["outside"], []),
    # questions / other -> helpful, no pending offer invented
    ("how much does it cost?", "send", ["happy to help"], ["reply yes"]),
    ("what does the study say?", "send", ["happy to help"], []),
    ("hmm interesting", "send", ["happy to help"], []),
    ("kya ye free hai?", "send", ["zaroor"], []),
]


@pytest.mark.parametrize("msg,action,any_of,none_of", TABLE)
async def test_reply_table_without_pending(client, msg, action, any_of, none_of):
    out = await reply(client, "conv_t", msg)
    assert out["action"] == action and out["rationale"]
    if action == "send":
        assert out["body"].strip()
        low = out["body"].lower()
        if any_of:
            assert any(x in low for x in any_of), out["body"]
        assert not any(x in low for x in none_of), out["body"]
    if action == "wait":
        assert out["wait_seconds"] in (3600, 86400)


PENDING_TABLE = [
    ("yes please", ["abstract"]),
    ("what does it say exactly?", ["jida", "abstract"]),
    ("GST kaise file karun", ["outside", "abstract"]),
    ("this is useless", ["sorry", "abstract"]),
]


@pytest.mark.parametrize("msg,expected", PENDING_TABLE)
async def test_reply_table_with_pending_offer(client, fresh_env, msg, expected):
    await seed_pending(fresh_env)
    out = await reply(client, "conv_p", msg)
    assert out["action"] == "send"
    low = out["body"].lower()
    assert any(x in low for x in expected), out["body"]


# ---- action-mode lint ---------------------------------------------------------------------------------
def test_every_commit_template_passes_lint():
    for kind in ("commit", "confirmed"):
        for lang in ("en", "hi-en"):
            for customer in (False, True):
                used = []
                while (body := _render(kind, lang, customer, "the draft", used)) is not None:
                    assert lint_action_body(body) == [], body
                    used.append(body)
                assert used


def test_lint_rejects_qualifying_and_actionless_bodies():
    for phrase in BANNED:
        assert lint_action_body(f"Done. {phrase.capitalize()} want it?")
    assert lint_action_body("Sounds interesting.")                 # no action word
    assert lint_action_body("Done, sending. Ready to go?")         # trailing question
    assert lint_action_body("Done. Here's the draft. Reply CONFIRM.") == []


async def test_commit_uses_pending_offer_and_two_step_confirm(client, fresh_env):
    await seed_pending(fresh_env)
    a = await reply(client, "conv_c", "yes please send it")
    assert "abstract" in a["body"] and "CONFIRM" in a["body"] and a["cta"] == "binary_yes_stop"
    b = await reply(client, "conv_c", "CONFIRM", 3)
    assert b["action"] == "send" and "confirmed" in b["body"].lower() and b["cta"] == "none"
    c = await reply(client, "conv_c", "yes", 4)
    assert c["action"] == "wait"                                    # already done: no third identical message


# ---- merchant-level (not conversation-level) state ------------------------------------------------------
async def test_hostility_counted_across_conversations(client):
    assert (await reply(client, "conv_h1", "this is a scam", 2))["action"] == "send"
    assert (await reply(client, "conv_h2", "what a waste", 2))["action"] == "end"


async def test_auto_reply_streak_resets_after_a_genuine_reply(client):
    auto = "Thank you for contacting us! Our team will respond shortly."
    assert (await reply(client, "a1", auto))["action"] == "send"
    assert (await reply(client, "a2", "yes please tell me more"))["action"] == "send"      # real human
    assert (await reply(client, "a3", auto))["action"] == "send"                            # streak restarted
    assert (await reply(client, "a4", auto))["action"] == "end"


async def test_repeated_unknown_canned_text_is_detected_by_hash(client):
    canned = "We appreciate your interest in our clinic and value your feedback"
    assert (await reply(client, "x1", canned))["action"] == "send"      # first sighting: looks like a person
    second = await reply(client, "x2", canned)                            # same text again on another conversation
    assert second["action"] == "send" and "auto" in second["body"].lower()
    assert (await reply(client, "x3", canned))["action"] == "end"


async def test_re_engagement_after_a_non_opt_out_end(client):
    auto = "Thank you for contacting us! Our team will respond shortly."
    await reply(client, "r1", auto)
    assert (await reply(client, "r2", auto))["action"] == "end"
    again = await reply(client, "r3", "hi, what does this cost?", 2)
    assert again["action"] == "send" and again["body"].strip()


async def test_opt_out_is_permanent_and_suppresses_ticks(client, dataset):
    t, m = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    assert (await reply(client, "o1", "please stop"))["action"] == "end"
    assert (await reply(client, "o2", "yes ok let's do it"))["action"] == "end"
    acts = (await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": [t["id"]]})).json()["actions"]
    assert acts == []


async def test_a_merchants_opt_out_does_not_block_another_merchant(client):
    await reply(client, "o1", "stop", mid=MID)
    assert (await reply(client, "o2", "yes", mid="m_002_bharat_dentist_mumbai"))["action"] == "send"


# ---- turn cap ---------------------------------------------------------------------------------------------
async def test_turn_cap_with_and_without_pending(client, fresh_env):
    await seed_pending(fresh_env)
    five = await reply(client, "t1", "tell me more please", 5)
    assert five["action"] == "send" and five["cta"] == "none"
    assert (await reply(client, "t1", "and?", 6))["action"] == "end"
    assert (await reply(client, "t2", "yes go ahead", 5))["action"] == "send"       # a commitment still gets action at turn 5


# ---- language ---------------------------------------------------------------------------------------------
async def test_language_switches_per_turn(client, dataset):
    m = {**next(x for x in dataset["merchants"] if x["merchant_id"] == MID)}
    m["identity"] = {**m["identity"], "languages": ["en"]}
    await push(client, "merchant", MID, m)
    en = await reply(client, "l1", "what does this cost?")
    hi = await reply(client, "l2", "kya ye free hai, mujhe bata dijiye")
    assert "Zaroor" not in en["body"] and "Zaroor" in hi["body"]


# ---- customer replies ---------------------------------------------------------------------------------------
async def recall_conv(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_003_recall_due_priya")
    acts = (await client.post("/v1/tick", json={"now": "2026-09-25T09:00:00Z", "available_triggers": [t["id"]]})).json()["actions"]
    assert acts and acts[0]["send_as"] == "merchant_on_behalf"
    return acts[0]["conversation_id"]


@pytest.mark.parametrize("msg,slot", [("1", "Wed 5 Nov, 6pm"), ("2", "Thu 6 Nov, 5pm"), ("Thursday works", "Thu 6 Nov, 5pm"),
                                       ("wed please", "Wed 5 Nov, 6pm"), ("2 pls", "Thu 6 Nov, 5pm")])
async def test_customer_slot_selection_books_exact_slot(client, dataset, msg, slot):
    conv = await recall_conv(client, dataset)
    out = await reply(client, conv, msg, 2, cid=CID, role="customer")
    assert out["action"] == "send" and f"Booked: {slot}" in out["body"] and out["cta"] == "none"


async def test_customer_yes_without_choice_gets_slot_menu(client, dataset):
    conv = await recall_conv(client, dataset)
    out = await reply(client, conv, "yes", 2, cid=CID, role="customer")
    assert out["action"] == "send" and "Reply 1 for Wed, 2 for Thu" in out["body"] and out["cta"] == "multi_choice_slot"


async def test_customer_opt_out_blocks_only_that_customer(client, dataset, fresh_env):
    conv = await recall_conv(client, dataset)
    out = await reply(client, conv, "please stop messaging me", 2, cid=CID, role="customer")
    assert out["action"] == "end"
    assert (await load_state(fresh_env, ckey(CID)))["opted_out"]
    assert (await reply(client, "m_conv", "yes", 2))["action"] == "send"           # merchant unaffected
    t2 = {"kind": "recall_due", "scope": "customer", "merchant_id": MID, "customer_id": CID, "payload": {},
          "urgency": 3, "suppression_key": "recall:again", "expires_at": "2026-12-01T00:00:00Z"}
    await push(client, "trigger", "trg_again", t2)
    acts = (await client.post("/v1/tick", json={"now": "2026-09-26T09:00:00Z", "available_triggers": ["trg_again"]})).json()["actions"]
    assert acts == []                                                                # that customer is suppressed


async def test_customer_replies_speak_as_the_clinic(client, dataset):
    conv = await recall_conv(client, dataset)
    auto = await reply(client, conv, "Thank you for contacting us, we will get back to you", 2, cid=CID, role="customer")
    body = auto["body"]
    assert auto["action"] == "send" and ("we'll" in body or "hum" in body)          # conv is hi-en for Priya
    assert "I'll" not in body and " main " not in body.lower()


async def test_customer_hostile_then_end(client, dataset):
    conv = await recall_conv(client, dataset)
    assert (await reply(client, conv, "this is a scam", 2, cid=CID, role="customer"))["action"] == "send"
    assert (await reply(client, conv, "you idiots", 3, cid=CID, role="customer"))["action"] == "end"


# ---- invariants ------------------------------------------------------------------------------------------------
async def test_bodies_never_repeat_and_never_contain_placeholders(client, fresh_env):
    await seed_pending(fresh_env)
    seen = []
    for i, msg in enumerate(["what is it?", "and the cost?", "hmm ok", "tell me more", "really?"]):
        out = await reply(client, "conv_inv", msg, 2 + i % 3)
        if out["action"] == "send":
            assert out["body"] not in seen and "{" not in out["body"] and "_" not in out["body"]
            seen.append(out["body"])
