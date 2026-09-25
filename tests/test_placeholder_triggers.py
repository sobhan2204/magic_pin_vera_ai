"""The generated triggers carry only {"placeholder": true}: messages must still lead with a real, trigger-relevant fact."""
import copy

from app.compose import compose_template
from app.humanize import parse_dt
from app.normalize import normalize_trigger
from app.resolver import build_factsheet, make_salutation

NOW = parse_dt("2026-04-26T10:35:00Z")
VACUOUS = ("quick profile check-in", "quick update on your profile", "one thing stood out")


def sheet(dataset, t, now=NOW, mutate=None):
    m = copy.deepcopy(next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"]))
    c = next((x for x in dataset["customers"] if x["customer_id"] == t.get("customer_id")), None)
    if mutate:
        mutate(m)
    trig = normalize_trigger(t["id"], 1, t)
    return trig, build_factsheet(trig, m, dataset["categories"][m["category_slug"]], c, now)


def placeholders(dataset):
    return [t for t in dataset["triggers"] if (t.get("payload") or {}).get("placeholder")]


def test_no_placeholder_trigger_leads_with_a_vacuous_hook_and_all_compose(dataset):
    ph = placeholders(dataset)
    assert len(ph) >= 60
    for t in ph:
        trig, fs = sheet(dataset, t)
        assert fs is not None, t["id"]                      # digest kinds now default to the category's latest item
        assert fs.facts[0].key == "hook" and fs.facts[0].id == "F1"
        assert [f.id for f in fs.facts] == [f"F{i}" for i in range(1, len(fs.facts) + 1)]
        c = compose_template(trig, fs, [])
        assert c.violations == [], (t["id"], c.body, c.violations)
        assert not any(v in c.body.lower() for v in VACUOUS), (t["id"], c.body)
        assert "Dr. Dr." not in c.body


def test_salutation_does_not_double_the_honorific():
    m = {"category_slug": "dentists", "identity": {"owner_first_name": "Dr. Rajan", "name": "City Dental Clinic"}}
    assert make_salutation("dentists", m) == "Dr. Rajan"
    m["identity"]["owner_first_name"] = "Rajan"
    assert make_salutation("dentists", m) == "Dr. Rajan"
    m["identity"]["owner_first_name"] = "dr Rajan"
    assert make_salutation("dentists", m) == "dr Rajan"


def _t(kind, merchant_id, **extra):
    return {"id": f"trg_ph_{kind}", "scope": "merchant", "kind": kind, "merchant_id": merchant_id, "customer_id": None,
            "payload": {"placeholder": True}, "urgency": 2, "suppression_key": f"ph:{kind}", "expires_at": "2026-12-01T00:00:00Z", **extra}


def test_perf_dip_uses_real_negative_data_and_never_claims_a_number_it_lacks(dataset):
    mid = "m_002_bharat_dentist_mumbai"                       # calls -50%, views -22% in delta_7d
    _, fs = sheet(dataset, _t("perf_dip", mid))
    assert fs.text("hook") == "Your Google profile shows calls down 50% over the last 7 days"
    def flat(m):
        m["performance"]["delta_7d"] = {"views_pct": 0.05, "calls_pct": 0.02}
        m["performance"]["views"], m["performance"]["calls"] = 5000, 500       # also above peers
    _, fs2 = sheet(dataset, _t("perf_dip", mid), mutate=flat)
    assert fs2.text("hook").startswith("Your Google profile shows 500 calls")        # strongest merchant fact (peer gap)
    assert fs2.text("t.kind") == "Your profile numbers have dipped recently"         # the trigger is the one supporting sentence


def test_renewal_and_winback_use_the_subscription_facts(dataset):
    _, fs = sheet(dataset, _t("renewal_due", "m_002_bharat_dentist_mumbai"))
    assert fs.text("hook") == "Your magicpin subscription shows your Pro plan has 12 days left"
    _, fs = sheet(dataset, _t("winback_eligible", "m_004_glamour_salon_pune"))
    assert fs.text("hook") == "Your magicpin subscription shows your plan expired 38 days ago"


def test_dormant_uses_conversation_history_or_signal(dataset):
    _, fs = sheet(dataset, _t("dormant_with_vera", "m_004_glamour_salon_pune"))       # last message 2026-03-19 -> 37 days
    assert fs.text("hook") == "Our chat history shows it's been 37 days since we last spoke"
    _, fs = sheet(dataset, _t("dormant_with_vera", "m_002_bharat_dentist_mumbai"), now=parse_dt("2026-04-26T10:00:00Z"))
    assert "since we last spoke" in fs.text("hook")


def test_digest_kinds_default_to_latest_item_only_when_no_id_is_given(dataset):
    _, fs = sheet(dataset, _t("research_digest", "m_001_drmeera_dentist_delhi"))
    assert "carries a new study" in fs.text("hook") and "JIDA" in fs.text("hook")
    _, fs = sheet(dataset, _t("regulation_change", "m_001_drmeera_dentist_delhi"))
    assert "radiograph" in fs.text("hook")
    _, fs = sheet(dataset, _t("supply_alert", "m_009_apollo_pharmacy_jaipur"))
    assert "atorvastatin" in fs.text("hook").lower() or "recall" in fs.text("hook").lower()
    bad = _t("research_digest", "m_001_drmeera_dentist_delhi")
    bad["payload"] = {"top_item_id": "does_not_exist"}
    assert sheet(dataset, bad)[1] is None                                        # explicit but unknown id: still a missing join


def test_gbp_uses_the_merchants_own_verified_flag(dataset):
    _, fs = sheet(dataset, _t("gbp_unverified", "m_002_bharat_dentist_mumbai"))      # Bharat: verified false
    assert "not verified yet" in fs.text("hook")
    _, fs = sheet(dataset, _t("gbp_unverified", "m_001_drmeera_dentist_delhi"))      # Meera: verified true -> generic path
    assert "not verified" not in (fs.text("hook") or "")


def test_seasonal_kinds_use_the_categorys_seasonal_note(dataset):
    _, fs = sheet(dataset, _t("festival_upcoming", "m_007_powerhouse_gym_bangalore"))
    assert "seasonal pattern for Apr-Jun" in fs.text("cat.season")                    # available as a fact ...
    assert fs.text("hook") != fs.text("cat.season") and fs.get("t.kind")               # ... but the lead is the strongest merchant fact


# ---- consent mode ---------------------------------------------------------------------------------------------------
def test_lenient_consent_accepts_any_active_scope_strict_does_not():
    from app.normalize import normalize_trigger as nt
    from app.policy import check_policy
    promo = {"consent": {"opted_in_at": "2026-01-01", "scope": ["promotional_offers"]}}
    t = nt("t", 1, {"kind": "recall_due", "scope": "customer", "merchant_id": "m", "customer_id": "c", "expires_at": "2026-12-01T00:00:00Z"})
    args = (t, {"category_slug": "x"}, {"slug": "x"}, promo, {}, {}, NOW, False)
    assert check_policy(*args, consent_mode="strict")[1] == "consent_scope_mismatch"
    assert check_policy(*args) == (True, "ok")                                   # lenient is the default now
    assert check_policy(*args, consent_mode="lenient") == (True, "ok")
    none = {"consent": {"opted_in_at": None, "scope": []}}
    assert check_policy(t, {"category_slug": "x"}, {"slug": "x"}, none, {}, {}, NOW, False, consent_mode="lenient")[1] == "no_consent"


async def test_consent_mode_env_reaches_the_tick(client, dataset, monkeypatch):
    from app.config import reset_settings
    from conftest import NOW_ISO, push
    reset_settings()                                                            # default consent mode: lenient
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "merchant", m["merchant_id"], m)
    await push(client, "customer", "c_promo", {"customer_id": "c_promo", "merchant_id": m["merchant_id"],
                                              "identity": {"name": "Ira", "language_pref": "en"}, "relationship": {"last_visit": "2026-01-01"},
                                              "consent": {"opted_in_at": "2026-01-01", "scope": ["promotional_offers"]}})
    await push(client, "trigger", "trg_promo", {"kind": "recall_due", "scope": "customer", "merchant_id": m["merchant_id"],
                                               "customer_id": "c_promo", "payload": {"placeholder": True}, "urgency": 3,
                                               "suppression_key": "promo:1", "expires_at": "2026-12-01T00:00:00Z"})
    acts = (await client.post("/v1/tick", json={"now": NOW_ISO, "available_triggers": ["trg_promo"]})).json()["actions"]
    assert len(acts) == 1 and acts[0]["customer_id"] == "c_promo"


# ---- content quality fixes found by scoring with the official judge prompt ---------------------------------------------
def test_planning_message_carries_an_outline_not_an_empty_promise(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_016_kids_yoga_program_drafting")
    trig, fs = sheet(dataset, t)
    body = compose_template(trig, fs, []).body
    assert "starter outline" in body and "kids yoga summer camp" in body and "First Month @ ₹499" in body
    assert "I've put together" not in body and "maine" not in body.lower()          # no claim that a draft already exists


def test_curious_ask_is_anchored_in_the_merchants_own_offers(dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_008_curious_ask_studio11")
    trig, fs = sheet(dataset, t)
    body = compose_template(trig, fs, []).body
    assert "Haircut @ ₹99" in body and "Hair Spa @ ₹499" in body


def test_kind_level_statements_do_not_drag_in_unrelated_numbers(dataset):
    from app.prompts import select_facts
    from app.playbooks import get_playbook
    m = next(x for x in dataset["merchants"] if x["category_slug"] == "salons" and not x.get("review_themes"))
    t = _t("review_theme_emerged", m["merchant_id"])
    trig, fs = sheet(dataset, t)
    assert fs.get("t.kind").text == "Recent reviews are showing a recurring theme"     # the trigger is the supporting fact
    assert fs.get("hook").source.startswith("merchant.")                            # ... and the lead is the strongest merchant fact
    body = compose_template(trig, fs, []).body
    assert "theme" in body and "themes" in body                           # kind statement + honest 'pull the themes' ask
    assert [f.key for f in select_facts(fs, get_playbook(trig.kind))][:2] == ["hook", "t.kind"]


def test_speculation_lint_allows_words_the_facts_use(dataset):
    from app.writer import check
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_018_supply_atorvastatin_recall")
    trig, fs = sheet(dataset, t)
    body = compose_template(trig, fs, []).body.replace("covers", "covers the affected")
    assert not any("speculative" in v for v in check({"body": body, "cta": "open_ended", "facts_used": []}, fs, []))


# ---- customer-facing messages must never see merchant-internal facts ------------------------------------------------
def test_customer_facing_fact_sheets_contain_only_customer_safe_facts(dataset):
    from app.playbooks import get_playbook
    from app.prompts import build_writer_messages
    n = 0
    for t in dataset["triggers"]:
        if t["scope"] != "customer":
            continue
        trig, fs = sheet(dataset, t)
        n += 1
        for f in fs.facts:
            assert f.key == "hook" or f.key.startswith(("t.", "c.")) or f.key == "m.offer_price", (t["id"], f.key)
        blob = " ".join(f.text for f in fs.facts).lower()
        for leak in ("plan has", "peer average", "click-through", "direction requests", "retention", "your profile", "subscription"):
            assert leak not in blob, (t["id"], leak, blob)
        user = build_writer_messages(fs, get_playbook(trig.kind), trig)[1]["content"]
        assert "peer average" not in user and "Pro plan" not in user
        assert compose_template(trig, fs, []).violations == []
    assert n >= 25


def test_placeholder_customer_messages_use_the_customers_own_facts(dataset):
    t = next(x for x in dataset["triggers"] if x["kind"] == "customer_lapsed_soft" and (x["payload"] or {}).get("placeholder"))
    trig, fs = sheet(dataset, t)
    body = compose_template(trig, fs, []).body
    cust = next(c for c in dataset["customers"] if c["customer_id"] == t["customer_id"])
    assert cust["identity"]["name"].split()[0] in body
    assert "last visit" in body.lower() or "visited" in body.lower()
    assert not any(w in body.lower() for w in ("plan", "peer", "profile"))
