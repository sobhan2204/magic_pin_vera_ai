"""Template composer quality contract (case-study shape): specific hook, ONE supporting fact, one CTA last, hi-en variants."""
import copy
import re

import pytest

from app.compose import compose_template
from app.humanize import beautify_dates, parse_dt
from app.normalize import normalize_trigger
from app.playbooks import PLAYBOOKS, get_playbook
from app.resolver import build_factsheet
from app.verifier import verify

NOW = parse_dt("2026-04-26T10:35:00Z")
HINDI = re.compile(r"\b(aap|aapke|aapka|aapki|hai|hain|kar|ke|liye|kya|ya|aur|main|hum|ko|se|mein|par|ho|gaye|pichle)\b", re.I)
# kinds whose hook is legitimately number-free (a question, a status, a promise)
NUMBER_FREE_HOOK = {"curious_ask_due", "gbp_unverified", "active_planning_intent", "appointment_tomorrow", "trial_followup",
                    "supply_alert", "chronic_refill_due"}


def build(dataset, t, language=None, now=NOW, mutate=None):
    m = copy.deepcopy(next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"]))
    c = copy.deepcopy(next((x for x in dataset["customers"] if x["customer_id"] == t.get("customer_id")), None))
    if language == "en":
        m["identity"]["languages"] = ["en"]
        if c:
            c["identity"]["language_pref"] = "english"
    if mutate:
        mutate(m, c)
    trig = normalize_trigger(t["id"], 1, t)
    fs = build_factsheet(trig, m, dataset["categories"][m["category_slug"]], c, now)
    return trig, fs


def sentences(body):
    b = re.sub(r"\b(Dr|Mr|Mrs|Ms)\.", r"\1", body)
    return [s for s in re.split(r"(?<=[.!?])\s+", b) if s.strip()]


@pytest.mark.parametrize("language", ["en", None], ids=["english", "hi-en"])
def test_every_seed_message_has_the_case_study_anatomy(dataset, language):
    for t in dataset["trigger_seeds"]:
        trig, fs = build(dataset, t, language)
        pb = get_playbook(trig.kind)
        c = compose_template(trig, fs, [])
        assert c.violations == [] and verify({"body": c.body, "cta": c.cta}, fs, []) == [], (t["id"], c.body)
        sents = sentences(c.body)
        greeting = 1 if fs.send_as == "merchant_on_behalf" else 0
        hooks, supports, ask = 1, len(sents) - greeting - 2, 1
        # hook + at most the playbook's supporting facts (+1 for the reciprocity sentence of curious_ask) + exactly one CTA
        assert 0 <= supports <= max(pb.support_n, 1) + (1 if trig.kind == "curious_ask_due" else 0), (t["id"], sents)
        last = sents[-1]
        assert ("?" in last) or re.search(r"\bReply\b", last), (t["id"], last)          # the ask lands in the LAST sentence
        assert "?" not in " ".join(sents[:-1]) and len(re.findall(r"\bReply\b", c.body)) <= 1   # one CTA, nothing buried
        if trig.kind not in NUMBER_FREE_HOOK:
            assert re.search(r"\d", sents[greeting]), (t["id"], sents[greeting])            # hook carries a number
        assert re.search(r"\d", c.body) or trig.kind in {"curious_ask_due", "gbp_unverified"}


def test_hindi_english_variants_are_real_code_mix_not_just_a_hindi_ask(dataset):
    hooks_with_hindi = {"perf_dip", "perf_spike", "renewal_due", "winback_eligible", "festival_upcoming", "milestone_reached",
                        "review_theme_emerged", "competitor_opened", "dormant_with_vera", "gbp_unverified", "recall_due",
                        "customer_lapsed_hard", "chronic_refill_due", "trial_followup", "wedding_package_followup"}
    seen = set()
    for t in dataset["trigger_seeds"]:
        trig, fs = build(dataset, t, None, now=parse_dt("2026-09-25T09:00:00Z") if t["kind"] == "recall_due" else NOW)
        body = compose_template(trig, fs, []).body
        first = sentences(body)[1 if fs.send_as == "merchant_on_behalf" else 0]
        if fs.language == "hi-en" and trig.kind in hooks_with_hindi:
            assert HINDI.search(first), (trig.kind, first)                                   # the hook itself is code-mixed
            seen.add(trig.kind)
        en_trig, en_fs = build(dataset, t, "en")
        en_body = compose_template(en_trig, en_fs, []).body
        assert not re.search(r"\b(aapke|aapka|hain|kya|karo|dijiye)\b", en_body, re.I), (trig.kind, en_body)   # English stays English
    assert len(seen) >= 10


def test_merchant_facts_name_their_source(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_001_research_digest_dentists")
    _, fs = build(dataset, t, "en")
    assert fs.text("m.perf30").startswith("Your Google profile shows")
    assert fs.text("m.ctr").startswith("Your Google profile shows a click-through rate")
    assert fs.text("m.views_peer").startswith("Your Google profile shows")
    assert fs.text("m.week").startswith("Your Google profile shows")
    assert fs.text("m.cohort").startswith("Your patient records show")
    assert fs.text("m.offers").startswith("Your offer catalog has")
    assert fs.text("m.subscription").startswith("Your magicpin subscription shows")
    assert fs.text("m.review").startswith("Your recent Google reviews show")
    assert fs.text("m.lapsed").startswith("Your customer records show")
    assert fs.text("cat.trend").startswith("Search trends show")
    _, hi = build(dataset, t, None)
    assert hi.get("m.perf30").hi.startswith("Aapke Google profile par")
    assert hi.get("m.lapsed").hi.startswith("Aapke customer records mein")


def test_source_prefix_is_not_repeated_back_to_back(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_004_perf_dip_bharat")
    for lang, second in ((None, "Saath hi "), ("en", "It also shows ")):
        trig, fs = build(dataset, t, lang)
        body = compose_template(trig, fs, []).body
        assert second in body and body.lower().count("google profile") == 1


def test_iso_dates_in_titles_are_prettified():
    assert beautify_dates("effective 2026-12-15 (circular 2026-11-04)") == "effective 15 Dec 2026 (circular 4 Nov 2026)"
    assert beautify_dates("no dates 12-2026") == "no dates 12-2026"


# ---- rationale mentions consent ---------------------------------------------------------------------------------------
def test_rationale_for_customer_messages_mentions_consent(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_003_recall_due_priya")
    trig, fs = build(dataset, t, None, now=parse_dt("2026-09-25T09:00:00Z"))
    r = compose_template(trig, fs, []).rationale
    assert "Consent: customer opted in (recall_reminders, appointment_reminders)" in r and "not named" not in r
    t2 = next(x for x in dataset["triggers"] if x["kind"] == "recall_due" and (x["payload"] or {}).get("placeholder"))
    trig2, fs2 = build(dataset, t2, None)
    r2 = compose_template(trig2, fs2, []).rationale
    assert "customer opted in (promotional_offers)" in r2 and "not named in that scope" in r2 and "lenient consent policy" in r2
    m = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_001_research_digest_dentists")
    trig3, fs3 = build(dataset, m)
    assert "Consent" not in compose_template(trig3, fs3, []).rationale                     # merchant-facing: nothing to say


# ---- relative-time facts: prefer the payload, omit negative or implausible values -------------------------------------------
def _festival(**payload):
    return {"id": "trg_f", "scope": "merchant", "kind": "festival_upcoming", "merchant_id": "m_003_studio11_salon_hyderabad",
            "customer_id": None, "payload": {"festival": "Diwali", "date": "2026-10-31", **payload}, "urgency": 1,
            "suppression_key": "f", "expires_at": "2026-11-02T00:00:00Z"}


def test_payload_days_are_preferred_and_bad_values_omitted(dataset):
    assert "44 days away" in build(dataset, _festival(days_until=44))[1].text("hook")                    # payload beats recomputation
    computed = build(dataset, _festival(), now=parse_dt("2026-10-01T00:00:00Z"))[1].text("hook")
    assert "30 days away" in computed                                                                     # computed when absent
    for bad in (-5, 5000, "soon", None, True):
        hook = build(dataset, _festival(days_until=bad), now=parse_dt("2027-02-01T00:00:00Z"))[1].text("hook")
        assert hook == "Diwali is on 31 Oct" and "days" not in hook, (bad, hook)                          # past date, nonsense payload
    hook = build(dataset, _festival(), now=parse_dt("2026-04-26T00:00:00Z"))[1].text("hook")
    assert "188 days away" in hook


def test_implausible_customer_and_history_values_are_omitted(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_003_recall_due_priya")
    def ancient(m, c):
        c["relationship"]["last_visit"] = "2011-01-01"
    trig, fs = build(dataset, t, "en", now=parse_dt("2026-09-25T09:00:00Z"), mutate=ancient)
    assert "months since" not in " ".join(f.text for f in fs.facts)
    def future(m, c):
        c["relationship"]["last_visit"] = "2027-01-01"
        c["relationship"]["first_visit"] = "2027-01-01"
    trig, fs = build(dataset, t, "en", now=NOW, mutate=future)
    blob = " ".join(f.text for f in fs.facts)
    assert "months since" not in blob and "last visit was on" not in blob and "since Jan 2027" not in blob
    def stale_history(m, c):
        m["conversation_history"] = [{"ts": "2018-01-01T00:00:00Z", "from": "vera", "body": "old"}]
    t2 = {"id": "trg_d", "scope": "merchant", "kind": "dormant_with_vera", "merchant_id": "m_001_drmeera_dentist_delhi",
          "customer_id": None, "payload": {"placeholder": True}, "urgency": 2, "suppression_key": "d", "expires_at": "2026-12-01T00:00:00Z"}
    _, fs2 = build(dataset, t2, "en", mutate=stale_history)
    assert fs2.get("m.dormant") is None                                                                   # 8 years is not a real "days since"


# ---- strongest merchant fact leads when the payload is thin ---------------------------------------------------------------------
def _thin(kind, mid):
    return {"id": f"trg_thin_{kind}", "scope": "merchant", "kind": kind, "merchant_id": mid, "customer_id": None,
            "payload": {"placeholder": True}, "urgency": 2, "suppression_key": kind, "expires_at": "2026-12-01T00:00:00Z"}


def test_thin_payload_leads_with_the_strongest_merchant_fact(dataset):
    mid = "m_001_drmeera_dentist_delhi"
    def lapsed_heavy(m, c):
        m["performance"]["views"], m["performance"]["calls"], m["performance"]["ctr"] = 1800, 12, 0.03          # ~ at peer level
        m["customer_aggregate"].update({"lapsed_180d_plus": 400, "total_unique_ytd": 540})
    trig, fs = build(dataset, _thin("competitor_opened", mid), "en", mutate=lapsed_heavy)
    assert fs.text("hook") == "Your customer records show 400 customers who haven't visited in over 180 days"
    assert fs.get("t.kind") is None and fs.get("hook").source.startswith("thin:")
    def peer_gap(m, c):
        m["performance"]["views"] = 300                                                                          # far below peers
    trig, fs = build(dataset, _thin("competitor_opened", mid), "en", mutate=peer_gap)
    assert fs.text("hook") == "Your Google profile shows 300 views over 30 days, against a peer average of 1,820"
    def review(m, c):
        m["performance"].update({"views": 1800, "calls": 12, "ctr": 0.03})
        m["customer_aggregate"]["lapsed_180d_plus"] = 5
        m["review_themes"] = [{"theme": "wait_time", "sentiment": "neg", "occurrences_30d": 12}]
    trig, fs = build(dataset, _thin("competitor_opened", mid), "en", mutate=review)
    assert fs.text("hook") == "Your recent Google reviews show 12 that mention long waits"
    def only_offer(m, c):
        m["performance"].update({"views": 1820, "calls": 12, "ctr": 0.03})
        m["customer_aggregate"] = {"total_unique_ytd": 540}
        m["review_themes"] = []
        m["signals"] = []
    trig, fs = build(dataset, _thin("competitor_opened", mid), "en", mutate=only_offer)
    assert fs.text("hook").startswith("Your offer catalog has Dental Cleaning @ ₹299 live")


def test_every_thin_trigger_message_leads_with_a_number_or_offer_and_passes_the_verifier(dataset):
    ph = [t for t in dataset["triggers"] if (t["payload"] or {}).get("placeholder") and t["scope"] == "merchant"]
    assert len(ph) >= 40
    for t in ph:
        for lang in ("en", None):
            trig, fs = build(dataset, t, lang)
            c = compose_template(trig, fs, [])
            assert c.violations == [], (t["id"], c.body, c.violations)
            assert fs.facts[0].key == "hook" and fs.facts[0].source != "merchant", t["id"]
            first = sentences(c.body)[0]
            if trig.kind != "curious_ask_due":                                               # its hook is a check-in, by design
                assert re.search(r"\d|offer|live", first), (t["id"], first)


def test_all_playbooks_have_hi_and_en_asks_and_one_support_by_default():
    for kind, pb in PLAYBOOKS.items():
        assert pb.ask_en.strip() and pb.ask_hi.strip(), kind
        assert pb.support_n <= 2, kind
