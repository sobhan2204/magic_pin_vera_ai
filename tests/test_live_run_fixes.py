"""Regression tests for live-run items 2-5, 7, 8 (items 1 and 6 live in test_regressions_live_run.py).
Fixtures are the exact failing bodies/triggers from the live run."""
import copy
import re

from app.compose import compose_template
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.playbooks import kind_fits_category
from app.policy import check_policy
from app.verifier import _SNAKE, CATEGORY_FORBIDDEN, GENERIC_PHRASES, polish, repeated_facts, verify
from test_template_quality import NOW, build

M42 = "m_042_pooja_gym_pune"
DENTIST = "m_001_drmeera_dentist_delhi"


def thin(kind, mid, **extra):
    return {"id": f"thin_{kind}_{mid}", "scope": "merchant", "kind": kind, "merchant_id": mid, "customer_id": None,
            "payload": {"metric_or_topic": "x", "placeholder": True}, "urgency": 2, "suppression_key": f"{kind}:{mid}",
            "expires_at": "2026-12-01T00:00:00Z", **extra}


def compose(dataset, t, language=None, mutate=None):
    trig, fs = build(dataset, t, language, now=NOW, mutate=mutate)
    return trig, fs, compose_template(trig, fs, [])


def some_fs(dataset, category="salons", language="en"):
    m = next(x for x in dataset["merchants"] if x["category_slug"] == category)
    return build(dataset, thin("perf_dip", m["merchant_id"]), language)[1]


def ok(body, cta="open_ended"):
    return {"body": body, "cta": cta}


# ---- item 2: no generic filler sentences ---------------------------------------------------------------------------------------
def test_item2_generic_phrases_are_rejected_by_the_verifier(dataset):
    fs = some_fs(dataset)
    for phrase in ("Ek festival aa raha hai.", "A festival is coming up.", "Here's something worth a look.",
                   "A new competitor has opened near you.", "Recent reviews are showing a recurring theme.",
                   "You're close to a new milestone on your profile."):
        body = f"{fs.salutation}, aapke Google profile par 30 din mein 22 calls hain. {phrase} Kya main dekh loon ki kya badla hai?"
        assert any("generic filler" in v for v in verify(ok(body), fs, [])), phrase
    assert GENERIC_PHRASES.search("Ek festival aa raha hai")


def test_item2_thin_triggers_lead_with_a_concrete_merchant_fact_never_a_kind_sentence(dataset):
    kinds = ["festival_upcoming", "competitor_opened", "milestone_reached", "review_theme_emerged", "perf_dip", "category_seasonal"]
    for m in dataset["merchants"][:12]:
        for kind in kinds:
            trig, fs, c = compose(dataset, thin(kind, m["merchant_id"]))
            assert fs.get("t.kind") is None
            assert re.search(r"\d", fs.text("hook")), (kind, fs.text("hook"))
            assert not GENERIC_PHRASES.search(c.body), c.body
            assert c.violations == [], (kind, c.body, c.violations)


# ---- item 3: renewal situations ---------------------------------------------------------------------------------------------------
def renewal(dataset, days, status="active"):
    t = {"id": "r", "scope": "merchant", "kind": "renewal_due", "merchant_id": M42, "customer_id": None,
         "payload": {"days_remaining": days, "plan": "Pro", "renewal_amount": 4999}, "urgency": 4,
         "suppression_key": "renew", "expires_at": "2026-12-01T00:00:00Z"}

    def mut(m, c):
        m["subscription"] = {"status": status, "plan": "Pro", "days_remaining": max(days, 0),
                             **({"days_since_expiry": -days} if status == "expired" else {})}
    return compose(dataset, t, None, mut)


def test_item3_far_from_expiry_is_a_value_message_not_a_renewal_push(dataset):
    trig, fs, c = renewal(dataset, 85)
    assert fs.kind == "renewal_value"
    assert not re.search(r"renew|4,999|Reply YES|\b85\b", c.body, re.I), c.body
    assert "value check-in" in c.rationale and "renewal value" in c.rationale
    assert c.violations == []


def test_item3_within_30_days_is_a_renewal_push(dataset):
    trig, fs, c = renewal(dataset, 9)
    assert fs.kind == "renewal_due" and "9 din" in c.body and "₹4,999" in c.body and "Reply YES" in c.body
    assert renewal(dataset, 30)[1].kind == "renewal_due"
    assert renewal(dataset, 31)[1].kind == "renewal_value"


def test_item3_expired_plan_gets_winback_framing(dataset):
    trig, fs, c = renewal(dataset, -20, status="expired")
    assert fs.kind == "winback_eligible" and "20" in c.body and "expire" in c.body
    assert "renewal" not in c.body.lower() and "winback eligible" in c.rationale


def test_item3_no_english_fragment_in_the_hindi_subscription_sentence(dataset):
    for mid in (M42, DENTIST):
        def mut(m, c):
            m["subscription"] = {"status": "active", "plan": "Pro", "days_remaining": 12}
        trig, fs, c = compose(dataset, thin("renewal_due", mid), None, mut)
        assert "hisaab se your" not in c.body and "ke hisaab se" not in c.body, c.body
        if fs.language == "hi-en":
            assert not re.search(r"\byour\b", c.body), c.body


def test_item3_far_renewal_loses_to_another_trigger_of_the_same_merchant(dataset):
    from app.decision import decide
    far = {"id": "far", "scope": "merchant", "kind": "renewal_due", "merchant_id": M42, "customer_id": None,
           "payload": {"days_remaining": 200, "plan": "Pro"}, "urgency": 4, "suppression_key": "far", "expires_at": "2026-12-01T00:00:00Z"}
    other = thin("perf_dip", M42, urgency=2)
    other["id"] = "other"
    t_far, fs_far = build(dataset, far, None)
    t_other, fs_other = build(dataset, other, None)
    out = decide([(t_far, fs_far, {}), (t_other, fs_other, {})], parse_dt("2026-04-26T10:35:00Z"), 5)
    assert [d.trigger_id for d in out] == ["other"]


# ---- item 4: category-fit wording for customer-facing kinds -------------------------------------------------------------
def cust_trigger(dataset, kind, category):
    cats = {m["merchant_id"]: m["category_slug"] for m in dataset["merchants"]}
    c = next(x for x in dataset["customers"] if cats[x["merchant_id"]] == category)
    return {"id": f"cust_{kind}_{category}", "scope": "customer", "kind": kind, "merchant_id": c["merchant_id"],
            "customer_id": c["customer_id"], "payload": {"placeholder": True}, "urgency": 3, "suppression_key": f"{kind}:{category}",
            "expires_at": "2026-12-01T00:00:00Z"}


def test_item4_recall_uses_the_businesss_own_words(dataset):
    expect = {"pharmacies": ("refill", "medicine"), "gyms": ("workout", "session"), "salons": ("appointment",),
              "restaurants": ("visit",), "dentists": ("check-up",)}
    for cat, wanted in expect.items():
        for lang in ("en", None):
            trig, fs, c = compose(dataset, cust_trigger(dataset, "recall_due", cat), lang)
            assert any(w in c.body.lower() for w in wanted), (cat, c.body)
            assert c.violations == []
            for bad in CATEGORY_FORBIDDEN[cat]:
                assert bad not in c.body.lower(), (cat, bad, c.body)
    body = compose(dataset, cust_trigger(dataset, "recall_due", "pharmacies"), "en")[2].body
    assert "check-up" not in body and "slots" not in body


def gate(dataset, kind, cat):
    t = cust_trigger(dataset, kind, cat)
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    cu = next(x for x in dataset["customers"] if x["customer_id"] == t["customer_id"])
    return check_policy(normalize_trigger(t["id"], 1, t), m, dataset["categories"][cat], cu, {}, {}, NOW, False)


def test_item4_kinds_that_do_not_fit_the_business_are_a_no_op(dataset):
    for kind, cat in (("chronic_refill_due", "dentists"), ("chronic_refill_due", "gyms"), ("chronic_refill_due", "restaurants"),
                      ("trial_followup", "pharmacies"), ("appointment_tomorrow", "pharmacies"), ("wedding_package_followup", "gyms")):
        assert gate(dataset, kind, cat) == (False, "kind_category_mismatch"), (kind, cat)
    assert gate(dataset, "chronic_refill_due", "pharmacies")[0]
    assert gate(dataset, "trial_followup", "gyms")[0]


def test_item4_verifier_rejects_category_forbidden_words(dataset):
    bodies = {"dentists": "Hi Ishaan, Bright Smile here. Your regular refill is due. Reply YES and we'll share the earliest slots.",
              "gyms": "Hi Ishaan, FitLab here. It's time for your regular check-up. Reply YES and we'll share the earliest slots.",
              "pharmacies": "Hi Ishaan, Wellness Cart here. It's time for your regular check-up. Reply YES and we'll share slots.",
              "salons": "Hi Ishaan, Glow here. It's time for your regular check-up. Reply YES and we'll share the earliest slots."}
    for cat, body in bodies.items():
        fs = compose(dataset, cust_trigger(dataset, "recall_due", cat), "en")[1]
        assert any("does not fit" in v for v in verify(ok(body, "binary_yes_stop"), fs, [])), cat


# ---- item 5: unknown kinds never leak raw payload --------------------------------------------------------------------------------
def test_item5_unknown_kind_payload_is_humanized(dataset):
    t = {"id": "t_anniv", "scope": "merchant", "kind": "anniversary", "merchant_id": DENTIST, "customer_id": None,
         "payload": {"note": "anniversary", "years": 8, "opened_on_iso": "2018-04-01T00:00:00Z", "shop_id": "abc_12"},
         "urgency": 2, "suppression_key": "anniv", "expires_at": "2026-12-01T00:00:00Z"}
    for lang in ("en", None):
        trig, fs, c = compose(dataset, t, lang)
        assert ";" not in c.body and "note:" not in c.body.lower() and "_" not in c.body and "years 8" not in c.body, c.body
        assert "8 years" in c.body and "anniversary" in c.body.lower(), c.body
        assert c.violations == []
    fs = some_fs(dataset)
    bad = f"{fs.salutation}, here's something worth a look: note: anniversary; years 8. Kya main dekh loon ki kya badla hai?"
    assert verify(ok(bad), fs, [])
    assert any("semicolon" in v for v in verify(ok(f"{fs.salutation}, note anniversary; years 8 on your profile today. Want a look?"), fs, []))


# ---- item 7: never state the same fact twice -----------------------------------------------------------------------------------
def test_item7_repeated_facts_are_detected(dataset):
    dup = ("Dr. Meera, JIDA Oct 2026, p.14 carries a new study: turnaround cut by 11 percent in a 300-site audit. "
           "A 300-site audit found turnaround cut by 11 percent.")
    assert repeated_facts(dup)
    assert repeated_facts("Aapke Google profile par 30 din mein 2,945 views hain, jabki peer average 2,400 hai. "
                          "Saath hi 30 din mein 2,945 views hain.")
    assert not repeated_facts("Your calls are down 50% over 7 days. Your click-through rate is 1.8% against a peer average of 3%.")
    fs = some_fs(dataset)
    fs.salutation = "Dr. Meera"
    assert any("stated twice" in v or "repeated" in v for v in verify(ok(dup + " Want me to draft a note?"), fs, []))


def test_item7_digest_summary_that_restates_the_title_is_dropped(dataset):
    from app.resolver import build_factsheet
    cat = copy.deepcopy(dataset["categories"]["dentists"])
    cat["digest"] = [{"id": "d_dup", "kind": "research", "title": "Turnaround cut by 11 percent in a 300-site audit", "source": "JIDA",
                      "summary": "A 300-site audit found turnaround cut by 11 percent."}]
    t = {"id": "t_dup", "scope": "merchant", "kind": "research_digest", "merchant_id": DENTIST, "customer_id": None,
         "payload": {"top_item_id": "d_dup"}, "urgency": 2, "suppression_key": "dup", "expires_at": "2026-12-01T00:00:00Z"}
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == DENTIST)
    trig = normalize_trigger("t_dup", 1, t)
    fs = build_factsheet(trig, m, cat, None, NOW)
    assert fs.get("t.summary") is None
    body = compose_template(trig, fs, []).body
    assert body.count("300") == 1 and not repeated_facts(body), body


# ---- item 8: salutation formatting -------------------------------------------------------------------------------------------------
def test_item8_polish_fixes_salutation_punctuation_and_capital_after_it(dataset):
    fs = some_fs(dataset)
    fs.salutation = "Anjali"
    assert polish({"body": "Anjali. Aapka plan 38 din pehle expire ho gaya."}, fs)["body"] == "Anjali, aapka plan 38 din pehle expire ho gaya."
    fs.salutation = "Dr. Bharat"
    assert polish({"body": "Dr. Bharat, Your Pro plan has 12 days left."}, fs)["body"] == "Dr. Bharat, your Pro plan has 12 days left."
    assert polish({"body": "Dr. Bharat. Your Pro plan has 12 days left."}, fs)["body"] == "Dr. Bharat, your Pro plan has 12 days left."
    assert polish({"body": "Dr. Bharat, JIDA Oct 2026 carries a new study."}, fs)["body"] == "Dr. Bharat, JIDA Oct 2026 carries a new study."
    fs.salutation = "Anjali"
    assert any("full stop" in v for v in verify(ok("Anjali. Aapka plan 38 din pehle expire ho gaya. Kya main dekh loon?"), fs, []))
    assert any("capital" in v for v in verify(ok("Anjali, Your plan has 12 days left. Want a check?"), fs, []))


def test_item8_english_fragment_inside_a_hindi_sentence_is_rejected(dataset):
    fs = build(dataset, thin("perf_dip", "m_002_bharat_dentist_mumbai"), None)[1]
    assert fs.language == "hi-en"
    bad = f"{fs.salutation}, aapke magicpin subscription ke hisaab se your Pro plan mein 12 din bache hain. Kya main dekh loon ki kya badla hai?"
    assert any("English template fragment" in v for v in verify(ok(bad), fs, []))


# ---- every trigger of the expanded dataset --------------------------------------------------------------------------------------------
def test_every_expanded_trigger_composes_clean(dataset):
    composed = skipped = 0
    for t in dataset["triggers"]:
        m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
        if t["scope"] == "customer" and not kind_fits_category(t["kind"], m["category_slug"]):
            skipped += 1
            continue
        trig, fs = build(dataset, t)
        if fs is None:
            continue
        c = compose_template(trig, fs, [])
        body = c.body
        assert c.violations == [] and verify(ok(body, c.cta), fs, []) == [], (t["id"], body)
        assert not GENERIC_PHRASES.search(body), (t["id"], body)
        assert not _SNAKE.search(body) and ";" not in body, (t["id"], body)
        assert not repeated_facts(body), (t["id"], body)
        if fs.send_as == "merchant_on_behalf":
            facts = " ".join(f.text for f in fs.facts).lower()
            for w in CATEGORY_FORBIDDEN.get(m["category_slug"], ()):
                assert w not in body.lower() or w in facts, (t["id"], w, body)
        assert re.match(rf"^(Hi )?{re.escape(fs.salutation)}(?:,| —) |^Quick one, {re.escape(fs.salutation)}:", body), (t["id"], body)
        composed += 1
    assert composed >= 80 and skipped >= 1


def test_item3_renewal_with_no_subscription_data_is_never_a_renewal_push(dataset):
    """Live run: trg_091 (no plan/days anywhere) said 'Reply YES ... renewal set kar doon' with no renewal fact in the body."""
    mid = next(m["merchant_id"] for m in dataset["merchants"] if m["category_slug"] == "pharmacies")

    def no_sub(m, c):
        m.pop("subscription", None)
    trig, fs, c = compose(dataset, thin("renewal_due", mid, urgency=4), None, no_sub)
    assert fs.kind == "renewal_value" and "renewal" not in c.body.lower() and "value check-in" in c.rationale, c.body


def test_item3_value_message_does_not_talk_about_the_plan_term(dataset):
    def far(m, c):
        m["subscription"] = {"status": "active", "plan": "Pro", "days_remaining": 211}
    m13 = next(m["merchant_id"] for m in dataset["merchants"] if m["category_slug"] == "restaurants")
    trig, fs, c = compose(dataset, thin("renewal_due", m13, urgency=4), None, far)
    assert fs.kind == "renewal_value" and "211" not in c.body and "plan" not in c.body.lower(), c.body
