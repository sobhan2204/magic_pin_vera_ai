from app.decision import decide, score_trigger
from app.humanize import parse_dt
from app.models import Fact, FactSheet
from app.normalize import normalize_trigger
from app.policy import check_policy, event_key

NOW = parse_dt("2026-04-26T10:35:00Z")
M = {"merchant_id": "m1", "category_slug": "dentists"}
CAT = {"slug": "dentists"}
CUST = {"customer_id": "c1", "consent": {"opted_in_at": "2025-01-01", "scope": ["recall_reminders"]}}


def trig(kind="perf_dip", **kw):
    raw = {"kind": kind, "merchant_id": "m1", "payload": {}, "urgency": 2, "expires_at": "2026-06-01T00:00:00Z", **kw}
    return normalize_trigger(kw.get("id", "t1"), kw.pop("version", 1) if False else 1, raw)


def allow(t, merchant=M, cat=CAT, cust=None, ms=None, cs=None, sent=False):
    return check_policy(t, merchant, cat, cust, ms or {}, cs or {}, NOW, sent)


def test_basic_allow_and_missing_joins():
    assert allow(trig()) == (True, "ok")
    assert allow(trig(), merchant=None)[1] == "missing_merchant"
    assert allow(trig(), cat=None)[1] == "missing_category"
    assert allow(normalize_trigger("t", 1, {"kind": "perf_dip"}))[1] == "missing_merchant"


def test_expiry_never_blocks_and_already_sent_does():
    assert allow(trig(expires_at="2026-04-01T00:00:00Z")) == (True, "ok")           # expired triggers are ranked last, not dropped
    assert allow(trig(), sent=True)[1] == "already_sent"


def test_opt_out_suppresses():
    assert allow(trig(), ms={"opted_out": True})[1] == "merchant_opted_out"
    t = trig("recall_due", customer_id="c1", scope="customer")
    assert allow(t, cust=CUST, cs={"opted_out": True})[1] == "customer_opted_out"


def test_customer_consent_rules():
    t = trig("recall_due", customer_id="c1", scope="customer")
    assert allow(t, cust=CUST) == (True, "ok")
    assert allow(t, cust=None)[1] == "missing_customer"
    assert allow(t, cust={"consent": {"opted_in_at": None, "scope": []}})[1] == "no_consent"
    wrong = {"consent": {"opted_in_at": "2025-01-01", "scope": ["promotional_offers"]}}
    assert allow(t, cust=wrong) == (True, "ok")                                       # lenient default: any active opt-in
    assert check_policy(t, M, CAT, wrong, {}, {}, NOW, False, consent_mode="strict")[1] == "consent_scope_mismatch"
    unknown_kind = trig("brand_new", customer_id="c1", scope="customer")
    assert allow(unknown_kind, cust=wrong) == (True, "ok")             # unknown mapping -> any active consent
    assert allow(unknown_kind, cust={"consent": {"scope": []}})[1] == "no_consent"


def test_restraint_rules():
    fresh = {"unanswered": 1, "last_proactive_ts": "2026-04-26T09:00:00Z"}
    assert allow(trig(urgency=2), ms=fresh)[1] == "awaiting_reply"
    assert allow(trig(urgency=4), ms=fresh) == (True, "ok")             # urgent breaks through
    old = {"unanswered": 1, "last_proactive_ts": "2026-04-20T09:00:00Z"}
    assert allow(trig(urgency=2), ms=old) == (True, "ok")               # not an open conversation any more
    two = {"unanswered": 2, "last_proactive_ts": "2026-04-01T09:00:00Z"}
    assert allow(trig(urgency=3), ms=two)[1] == "awaiting_reply"
    assert allow(trig(urgency=5), ms=two) == (True, "ok")


def test_event_key_is_per_recipient():
    a = normalize_trigger("t1", 1, {"kind": "research_digest", "merchant_id": "m1", "suppression_key": "research:dentists:W17"})
    b = normalize_trigger("t2", 1, {"kind": "research_digest", "merchant_id": "m2", "suppression_key": "research:dentists:W17"})
    assert event_key(a) != event_key(b)


def fs():
    return FactSheet(facts=[Fact("F1", "hook", "x")], salutation="s", language="en", send_as="vera",
                     category_slug="d", voice={}, allowed_entities=set(), active_offers=[], kind="k")


def cand(tid, merchant, urgency, **kw):
    t = normalize_trigger(tid, 1, {"kind": "perf_dip", "merchant_id": merchant, "urgency": urgency, **kw})
    return (t, fs(), {})


def test_one_per_merchant_highest_score_wins_ties_by_id():
    ds = decide([cand("b", "m1", 3), cand("a", "m1", 3), cand("c", "m1", 5), cand("z", "m2", 1)], NOW, 20)
    assert [(d.merchant_id, d.trigger_id) for d in ds] == [("m1", "c"), ("m2", "z")]
    ds = decide([cand("b", "m1", 3), cand("a", "m1", 3)], NOW, 20)
    assert ds[0].trigger_id == "a"                                      # deterministic tie-break


def test_cap_of_20():
    ds = decide([cand(f"t{i:02d}", f"m{i}", 2) for i in range(45)], NOW, 20)
    assert len(ds) == 20 and len({d.merchant_id for d in ds}) == 20


def test_scoring_prefers_urgent_expiring_fresh_and_penalises_recent_sends():
    base = trig(urgency=3, expires_at="2026-12-01T00:00:00Z")
    urgent = trig(urgency=4, expires_at="2026-12-01T00:00:00Z")
    soon = trig(urgency=3, expires_at="2026-04-27T00:00:00Z")
    assert score_trigger(urgent, NOW, {}) > score_trigger(base, NOW, {})
    assert score_trigger(soon, NOW, {}) > score_trigger(base, NOW, {})
    recent = {"last_proactive_ts": "2026-04-26T09:00:00Z"}
    assert score_trigger(base, NOW, recent) < score_trigger(base, NOW, {})


def test_expired_triggers_rank_below_live_ones_but_are_still_returned():
    live = trig(urgency=2, expires_at="2026-12-01T00:00:00Z")
    stale = trig(urgency=2, expires_at="2025-01-01T00:00:00Z")
    assert score_trigger(stale, NOW, {}) < score_trigger(live, NOW, {})
    ds = decide([cand("old", "m1", 2, expires_at="2025-01-01T00:00:00Z"), cand("new", "m2", 2, expires_at="2026-12-01T00:00:00Z")], NOW, 20)
    assert [d.trigger_id for d in ds] == ["new", "old"]
