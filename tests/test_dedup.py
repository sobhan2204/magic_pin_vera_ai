import copy

from app.compose import compose_template as compose_message, build_rationale
from app.dedup import (action_fingerprint, alternate_hook_keys, hook_identity, jaccard, normalize_body, promote_hook,
                       too_similar)
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.playbooks import get_playbook
from app.resolver import build_factsheet
from app.verifier import verify_rationale
from conftest import NOW_ISO, push, push_seed

NOW = parse_dt(NOW_ISO)


def _fs(dataset, tid):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == tid)
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    trig = normalize_trigger(tid, 1, t)
    return trig, build_factsheet(trig, m, dataset["categories"][m["category_slug"]], None, NOW)


# ---- layer 3: message similarity ---------------------------------------------------------------
def test_normalization_ignores_case_punctuation_and_emoji():
    assert normalize_body("Hi Priya! 🦷 It's ₹299.") == ["hi", "priya", "it", "s", "299"]
    assert jaccard("Reply YES", "reply, yes!") == 1.0


def test_similarity_threshold():
    a = "Dr. Meera, JIDA Oct 2026 carries a new study on 3-month recall. Want the abstract?"
    assert too_similar(a, [a]) == a                                     # exact
    assert too_similar(a.replace("Want", "Shall I send"), [a]) == a      # near duplicate
    assert too_similar("Your calls are down 50% this week, want a quick fix?", [a]) is None
    assert too_similar(a, []) is None


# ---- layer 2: action identity --------------------------------------------------------------------
def test_fingerprint_distinguishes_recipient_hook_and_cta():
    base = action_fingerprint("m1", None, "trigger.x", {"38"}, "open_ended")
    assert base == action_fingerprint("m1", None, "trigger.x", {"38"}, "open_ended")
    assert base != action_fingerprint("m2", None, "trigger.x", {"38"}, "open_ended")
    assert base != action_fingerprint("m1", "c1", "trigger.x", {"38"}, "open_ended")
    assert base != action_fingerprint("m1", None, "trigger.y", {"38"}, "open_ended")
    assert base != action_fingerprint("m1", None, "trigger.x", {"39"}, "open_ended")
    assert base != action_fingerprint("m1", None, "trigger.x", {"38"}, "binary_yes_stop")


def test_hook_identity_falls_back_to_text_when_no_numbers(dataset):
    _, fs = _fs(dataset, "trg_008_curious_ask_studio11")
    src, atoms = hook_identity(fs.get("hook"))
    assert atoms and src == "trigger.curious_ask"


def test_promote_hook_keeps_why_now_as_support(dataset):
    trig, fs = _fs(dataset, "trg_004_perf_dip_bharat")
    keys = alternate_hook_keys(fs, get_playbook(trig.kind).support_keys)
    assert keys and "hook" not in keys
    alt = promote_hook(fs, keys[0])
    assert alt.get("hook").text == fs.get(keys[0]).text
    assert alt.get("t.prev_hook").text == fs.get("hook").text
    assert alt.facts[0].id == fs.get(keys[0]).id and len(alt.facts) == len(fs.facts)
    body = compose_message(trig, alt, []).body
    assert "50%" in body                                  # original trigger still explained
    assert promote_hook(fs, "hook") is None and promote_hook(fs, "nope") is None


# ---- rationale ------------------------------------------------------------------------------------
def test_rationale_names_trigger_hook_and_objective(dataset):
    trig, fs = _fs(dataset, "trg_001_research_digest_dentists")
    c = compose_message(trig, fs, [])
    assert c.rationale.startswith("research digest trigger (new digest item this week; urgency 2/5)")
    assert "JIDA Oct 2026, p.14 carries a new study" in c.rationale and "CTA: open_ended" in c.rationale
    assert verify_rationale(c.rationale, c.body, fs, "research digest") == []


def test_rationale_verifier_catches_mismatches(dataset):
    trig, fs = _fs(dataset, "trg_001_research_digest_dentists")
    body = compose_message(trig, fs, []).body
    assert verify_rationale("Sends a generic nudge", body, fs, "research digest")
    v = verify_rationale("research digest trigger; cuts caries 57% more", body, fs, "research digest")
    assert any("57" in x for x in v)
    assert verify_rationale("research digest trigger (urgency 2/5); 38% lower caries", body, fs, "research digest") == []


# ---- end to end: all three layers through /v1/tick ----------------------------------------------------
async def _tick(client, ids, now=NOW_ISO):
    return (await client.post("/v1/tick", json={"now": now, "available_triggers": ids})).json()["actions"]


async def test_layer1_event_reservation_across_recipients(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    assert len(await _tick(client, [t["id"]])) == 1
    assert await _tick(client, [t["id"]]) == []


async def test_layer2_same_content_via_new_trigger_is_not_resent_verbatim(client, dataset):
    t, m = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    first = await _tick(client, [t["id"]])
    assert len(first) == 1
    twin = copy.deepcopy(t)
    twin.update({"id": "trg_twin", "suppression_key": "research:twin", "urgency": 4})
    await push(client, "trigger", "trg_twin", twin)
    second = await _tick(client, ["trg_twin"], now="2026-04-26T10:40:00Z")
    from app.dedup import jaccard as j
    for a in second:                                                     # either dropped, or clearly different wording
        assert a["body"] != first[0]["body"] and j(a["body"], first[0]["body"]) < 0.7


async def test_layer2_blocks_identical_action_when_no_alternate_exists(client, dataset, fresh_env):
    t, m = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    first = (await _tick(client, [t["id"]]))[0]
    twin = {**t, "id": "trg_twin2", "suppression_key": "research:twin2", "urgency": 4}
    await push(client, "trigger", "trg_twin2", twin)
    # merchant replied in between so restraint does not hide the dedup behaviour
    await client.post("/v1/reply", json={"conversation_id": first["conversation_id"], "merchant_id": m["merchant_id"],
                                         "from_role": "merchant", "message": "ok noted", "turn_number": 2})
    acts = await _tick(client, ["trg_twin2"], now="2026-04-26T12:00:00Z")
    bodies = await fresh_env.list_range(f"sent:bodies:{m['merchant_id']}")
    assert len(bodies) == len(set(bodies))                               # never an exact repeat, whatever happened
    assert len(acts) <= 1


async def test_layer3_conversation_bodies_never_repeat_across_many_triggers(client, dataset, fresh_env):
    await push(client, "category", "salons", dataset["categories"]["salons"])
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_003_studio11_salon_hyderabad")
    await push(client, "merchant", m["merchant_id"], m)
    seen = []
    for i in range(6):
        await push(client, "trigger", f"trg_dip{i}", {"kind": "perf_dip", "merchant_id": m["merchant_id"], "payload": {},
                                                      "urgency": 4, "suppression_key": f"dip:{i}",
                                                      "expires_at": "2026-12-01T00:00:00Z"})
        acts = await _tick(client, [f"trg_dip{i}"], now=f"2026-04-26T1{i}:00:00Z")
        seen += [a["body"] for a in acts]
    assert len(seen) == len(set(seen))
    from app.dedup import jaccard as j
    assert all(j(a, b) < 0.7 for i, a in enumerate(seen) for b in seen[:i])
