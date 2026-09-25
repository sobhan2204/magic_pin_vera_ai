import copy

import pytest

from app.humanize import (fmt_date, fmt_money, fmt_num, fmt_pct, fmt_time, humanize_signal, humanize_trend,
                          parse_dt, plural, words)
from app.normalize import normalize_trigger
from app.resolver import build_factsheet, make_salutation

NOW = parse_dt("2026-04-26T10:35:00Z")


def fs_for(dataset, trigger_id, now=NOW, mutate=None, cat_mutate=None):
    t = copy.deepcopy(next(x for x in dataset["trigger_seeds"] if x["id"] == trigger_id))
    m = copy.deepcopy(next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"]))
    cat = copy.deepcopy(dataset["categories"][m["category_slug"]])
    c = next((copy.deepcopy(x) for x in dataset["customers"] if x["customer_id"] == t.get("customer_id")), None)
    if mutate:
        mutate(t, m, c)
    if cat_mutate:
        cat_mutate(cat)
    return build_factsheet(normalize_trigger(t["id"], 1, t), m, cat, c, now)


# ---- humanize -------------------------------------------------------------------------------
def test_number_formatting():
    assert fmt_num(2410) == "2,410" and fmt_num(120000) == "1,20,000" and fmt_num(999) == "999"
    assert fmt_money(1499) == "₹1,499" and fmt_money(299) == "₹299"
    assert fmt_pct(0.5) == "50%" and fmt_pct(0.021) == "2.1%" and fmt_pct(0.030) == "3%" and fmt_pct(-0.3) == "30%"
    assert plural(1, "day") == "1 day" and plural(12, "day") == "12 days"


def test_dates_and_words():
    dt = parse_dt("2026-11-05T18:00:00+05:30")
    assert fmt_date(dt) == "5 Nov" and fmt_time(dt) == "6pm"
    assert fmt_time(parse_dt("2026-04-26T19:30:00+05:30")) == "7:30pm"
    assert words("high_risk_adults") == "high-risk adults" and words("6_month_cleaning") == "6-month cleaning"
    assert parse_dt("garbage") is None and parse_dt(None) is None


def test_signal_and_trend_humanizing_never_leaks_codes():
    assert humanize_signal("stale_posts:22d") == "your last Google post was 22 days ago"
    assert humanize_signal("ctr_below_peer_median") == "your click-through rate is below the peer median"
    assert humanize_signal("totally_unknown_code") is None
    assert humanize_trend("ORS_demand_+40") == "ORS demand up 40%"
    assert humanize_trend("cold_cough_demand_-60") == "cold cough demand down 60%"


# ---- salutation / language / send_as ----------------------------------------------------------
def test_salutation_rules(dataset):
    dent = {"category_slug": "dentists", "identity": {"owner_first_name": "Meera", "name": "X"}}
    salon = {"category_slug": "salons", "identity": {"owner_first_name": "Lakshmi", "name": "Studio11"}}
    nameless = {"category_slug": "gyms", "identity": {"name": "PowerHouse Fitness"}}
    assert make_salutation("dentists", dent) == "Dr. Meera"
    assert make_salutation("salons", salon) == "Lakshmi"
    assert make_salutation("gyms", nameless) == "PowerHouse Fitness"


def test_language_and_send_as(dataset):
    fs = fs_for(dataset, "trg_001_research_digest_dentists")
    assert (fs.language, fs.send_as, fs.salutation) == ("hi-en", "vera", "Dr. Meera")
    fs = fs_for(dataset, "trg_007_bridal_followup_kavya")               # customer prefers english
    assert (fs.language, fs.send_as, fs.salutation) == ("en", "merchant_on_behalf", "Kavya")
    fs = fs_for(dataset, "trg_003_recall_due_priya")                    # customer 'hi-en mix'
    assert fs.language == "hi-en" and fs.slots == ["Wed 5 Nov, 6pm", "Thu 6 Nov, 5pm"]
    fs = fs_for(dataset, "trg_017_kids_yoga_trial_followup_karthik")    # 'ta-en mix' -> English
    assert fs.language == "en" and fs.salutation == "Sumitra"           # parent is addressed


def test_language_falls_back_to_english_without_hindi(dataset):
    def no_hindi(t, m, c):
        m["identity"]["languages"] = ["en", "te"]
    assert fs_for(dataset, "trg_006_festival_diwali", mutate=no_hindi).language == "en"


# ---- derived facts ----------------------------------------------------------------------------
def test_derived_facts_are_computed_in_code(dataset):
    fs = fs_for(dataset, "trg_002_compliance_dci_radiograph")
    assert "232 days to go" in fs.text("hook")                          # 15 Dec minus 26 Apr
    fs = fs_for(dataset, "trg_012_milestone_mylari")
    assert "just 5 away from 150" in fs.text("hook")
    fs = fs_for(dataset, "trg_004_perf_dip_bharat")
    assert fs.text("hook").startswith("Your calls are down 50% over the last 7 days")
    assert fs.text("m.ctr") == "Your click-through rate is 1.8% against a peer average of 3%"
    fs = fs_for(dataset, "trg_003_recall_due_priya", now=parse_dt("2026-09-25T09:00:00Z"))
    assert "It's been 4 months since your last visit" in fs.text("hook")
    fs = fs_for(dataset, "trg_007_bridal_followup_kavya", now=parse_dt("2026-04-26T10:00:00Z"))
    assert "195 days to go" in fs.text("hook") and "30-day skin prep program" in fs.text("hook")


def test_relative_facts_follow_the_tick_clock(dataset):
    a = fs_for(dataset, "trg_006_festival_diwali", now=parse_dt("2026-04-26T10:00:00Z")).text("hook")
    b = fs_for(dataset, "trg_006_festival_diwali", now=parse_dt("2026-10-27T10:00:00Z")).text("hook")
    assert "187 days away" in a and "3 days away" in b                  # payload's stale days_until is ignored


def test_peer_comparison_and_merchant_facts(dataset):
    fs = fs_for(dataset, "trg_001_research_digest_dentists")
    assert fs.text("m.views_peer") == "Your 30-day views are 2,410 against a peer average of 1,820"
    assert fs.text("m.calls_peer") == "Your 30-day calls are 18 against a peer average of 12"
    assert fs.text("m.cohort") == "You have 124 high-risk adult patients on your roster"
    assert fs.text("m.week") == "This week your views are up 18%"
    assert fs.text("m.history").startswith("When we last spoke you said")
    assert fs.active_offers == ["Dental Cleaning @ ₹299"]               # expired offer excluded
    assert "Deep Cleaning" not in " ".join(f.text for f in fs.facts)


def test_seasonal_beat_and_trend_use_the_tick_month(dataset):
    fs = fs_for(dataset, "trg_001_research_digest_dentists", now=parse_dt("2026-04-26T00:00:00Z"))
    assert "Apr-Jun" in fs.text("cat.season") and "pediatric appointments" in fs.text("cat.season")
    fs = fs_for(dataset, "trg_001_research_digest_dentists", now=parse_dt("2026-12-05T00:00:00Z"))
    assert "Nov-Feb" in fs.text("cat.season")                           # wrap-around range
    assert fs.text("cat.trend") == "Searches for “clear aligners delhi” are up 62% year on year"


def test_offer_ideas_only_when_no_active_offer(dataset):
    fs = fs_for(dataset, "trg_004_perf_dip_bharat")                     # Bharat has no offers
    assert "Popular offers in your category include Dental Cleaning @ ₹299" in fs.text("cat.offers")
    assert fs_for(dataset, "trg_001_research_digest_dentists").get("cat.offers") is None


def test_customer_facts(dataset):
    fs = fs_for(dataset, "trg_003_recall_due_priya")
    assert fs.text("c.pref") == "You prefer weekday evening slots"
    assert fs.text("c.services") == "You've had cleaning and whitening with us before"
    assert fs.text("m.offer_price") == "Dental Cleaning @ ₹299 is on right now"


# ---- joins and freshness ------------------------------------------------------------------------
def test_digest_item_resolves_from_the_supplied_category_version(dataset):
    def add(cat):
        cat["digest"].append({"id": "d_new", "kind": "research", "title": "New finding", "source": "JIDA Nov 2026, p.2"})
    def point(t, m, c):
        t["payload"]["top_item_id"] = "d_new"
    assert fs_for(dataset, "trg_001_research_digest_dentists", mutate=point) is None          # not in old version
    fs = fs_for(dataset, "trg_001_research_digest_dentists", mutate=point, cat_mutate=add)
    assert "JIDA Nov 2026, p.2 carries a new study: New finding" == fs.text("hook")


def test_inline_top_item_shape_is_supported(dataset):
    def inline(t, m, c):
        t["payload"] = {"category": "dentists", "top_item": {"title": "Inline title", "source": "DCI 2026", "kind": "compliance"}}
    fs = fs_for(dataset, "trg_001_research_digest_dentists", mutate=inline)
    assert "Inline title" in fs.text("hook")


@pytest.mark.parametrize("tid", ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph", "trg_022_cde_webinar_dentists"])
def test_digest_backed_kinds_without_item_are_missing_joins(dataset, tid):
    def clear(t, m, c):
        t["payload"] = {"category": "dentists", "top_item_id": "nope", "digest_item_id": "nope"}
    assert fs_for(dataset, tid, mutate=clear) is None


def test_facts_have_unique_ids_atoms_and_no_jargon(dataset):
    for t in dataset["trigger_seeds"]:
        fs = fs_for(dataset, t["id"])
        assert [f.id for f in fs.facts] == [f"F{i}" for i in range(1, len(fs.facts) + 1)]
        assert fs.facts[0].key == "hook"                                # trigger fact always first
        for f in fs.facts:
            assert "_" not in f.text and "{" not in f.text and f.text.strip()


def test_allowed_entities_include_names_offers_and_competitors(dataset):
    fs = fs_for(dataset, "trg_023_competitor_opened_dentist")
    assert {"Smile Studio", "Dr. Meera's Dental Clinic", "Lajpat Nagar", "Dental Cleaning @ ₹199"} <= fs.allowed_entities
