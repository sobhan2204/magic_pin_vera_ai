"""The template fallback must itself pass the verifier for every trigger in the expanded dataset."""
import pytest

from app.compose import compose_template as compose_message
from app.fallback import compose_fallback, variant_for
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.playbooks import PLAYBOOKS, get_playbook
from app.resolver import build_factsheet
from app.verifier import verify

NOWS = [parse_dt("2026-04-26T10:35:00Z"), parse_dt("2026-09-25T09:00:00Z")]


def _fs(dataset, t, now):
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    c = next((x for x in dataset["customers"] if x["customer_id"] == t.get("customer_id")), None)
    return normalize_trigger(t["id"], 1, t), build_factsheet(
        normalize_trigger(t["id"], 1, t), m, dataset["categories"][m["category_slug"]], c, now)


@pytest.mark.parametrize("now", NOWS, ids=["dataset-time", "real-time"])
def test_fallback_passes_verifier_for_every_expanded_trigger(dataset, now):
    composed = 0
    for t in dataset["triggers"]:
        trig, fs = _fs(dataset, t, now)
        if fs is None:                                # digest-backed kind with a placeholder payload: missing join
            assert trig.kind in {"research_digest", "regulation_change", "cde_opportunity"}
            continue
        c = compose_message(trig, fs, [])
        assert c.violations == [], (t["id"], c.body, c.violations)
        assert fs.send_as == ("merchant_on_behalf" if trig.scope == "customer" else "vera")
        assert c.body.strip() and c.cta
        composed += 1
    assert composed >= 85


def test_every_seed_kind_has_a_playbook(dataset):
    for t in dataset["trigger_seeds"]:
        assert t["kind"] in PLAYBOOKS, t["kind"]
    assert get_playbook("never_seen_kind").kind == "generic"


def test_seed_triggers_all_compose_with_real_facts(dataset):
    for t in dataset["trigger_seeds"]:
        trig, fs = _fs(dataset, t, NOWS[0])
        assert fs is not None, t["id"]
        c = compose_message(trig, fs, [])
        assert c.violations == [], (t["id"], c.body, c.violations)
        assert "_" not in c.body and "{" not in c.body


def test_research_digest_body_reads_like_the_case_study(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_001_research_digest_dentists")
    trig, fs = _fs(dataset, t, NOWS[0])
    body = compose_message(trig, fs, []).body
    assert body.startswith(("Dr. Meera", "Quick one, Dr. Meera"))
    assert "JIDA Oct 2026, p.14" in body and "38%" in body and "124" in body


def test_recall_message_is_customer_facing_with_real_slots(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_003_recall_due_priya")
    trig, fs = _fs(dataset, t, NOWS[1])
    c = compose_message(trig, fs, [])
    assert fs.send_as == "merchant_on_behalf" and c.cta == "multi_choice_slot"
    assert c.body.startswith("Hi Priya, Dr. Meera's Dental Clinic here.")
    assert "Wed 5 Nov, 6pm" in c.body and "Thu 6 Nov, 5pm" in c.body and "₹299" in c.body
    assert "Reply 1 for Wed, 2 for Thu" in c.body or "Reply 1 for Wed, 2 for Thu" in c.body.replace("chahein toh ", "")


def test_seasonal_dip_reframes_as_expected(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_014_seasonal_acquisition_dip_powerhouse")
    trig, fs = _fs(dataset, t, NOWS[0])
    body = compose_message(trig, fs, []).body
    assert "expected seasonal dip" in body and "30%" in body


def test_ipl_advice_follows_is_weeknight(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["kind"] == "ipl_match_today")
    trig, fs = _fs(dataset, t, NOWS[0])
    assert "not a weeknight" in fs.text("t.advice") and "tonight, 7:30pm" in fs.text("hook")
    t2 = {**t, "payload": {**t["payload"], "is_weeknight": True}}
    _, fs2 = _fs(dataset, t2, NOWS[0])
    assert "weeknight, so a match-night combo" in fs2.text("t.advice")


def test_variants_are_deterministic_and_differ_across_triggers(dataset):
    t = dataset["trigger_seeds"][3]
    trig, fs = _fs(dataset, t, NOWS[0])
    pb = get_playbook(trig.kind)
    assert compose_fallback(fs, pb, trig.id) == compose_fallback(fs, pb, trig.id)
    bodies = {compose_fallback(fs, pb, trig.id, variant=i)["body"] for i in range(3)}
    assert len(bodies) == 3
    assert len({variant_for(f"trg_{i}") for i in range(30)}) == 3


def test_no_jargon_in_any_fact(dataset):
    for t in dataset["triggers"]:
        _, fs = _fs(dataset, t, NOWS[0])
        for f in (fs.facts if fs else []):
            assert "_" not in f.text, (t["id"], f.text)
