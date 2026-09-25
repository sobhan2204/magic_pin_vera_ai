from app.normalize import normalize_trigger


def test_top_level_shape():
    raw = {"id": "trg_1", "scope": "customer", "kind": "recall_due", "source": "internal",
           "merchant_id": "m1", "customer_id": "c1", "payload": {"due_date": "2026-11-12"},
           "urgency": 3, "suppression_key": "recall:c1", "expires_at": "2026-11-30T00:00:00Z"}
    t = normalize_trigger("trg_1", 2, raw)
    assert (t.id, t.version, t.kind, t.scope) == ("trg_1", 2, "recall_due", "customer")
    assert (t.merchant_id, t.customer_id, t.urgency, t.suppression_key) == ("m1", "c1", 3, "recall:c1")
    assert t.payload == {"due_date": "2026-11-12"} and t.expires_at.year == 2026


def test_ids_inside_payload_shape():
    raw = {"kind": "perf_dip", "payload": {"merchant_id": "m9", "customer_id": None, "metric": "calls"}}
    t = normalize_trigger("trg_x", 1, raw)
    assert t.merchant_id == "m9" and t.customer_id is None and t.scope == "merchant"
    assert t.urgency == 2                                       # default
    assert t.suppression_key == "perf_dip:m9:-:trg_x"           # default key
    assert t.expires_at is None


def test_flattened_shape_and_unknown_kind():
    t = normalize_trigger("t", 1, {"kind": "brand_new_kind", "merchant_id": "m1", "foo": 1})
    assert t.kind == "brand_new_kind" and t.payload == {"foo": 1}


def test_urgency_clamped_and_garbage_tolerated():
    assert normalize_trigger("t", 1, {"kind": "k", "urgency": 99}).urgency == 5
    assert normalize_trigger("t", 1, {"kind": "k", "urgency": 0}).urgency == 1
    assert normalize_trigger("t", 1, {"kind": "k", "urgency": "abc"}).urgency == 2
    t = normalize_trigger("t", 1, "not a dict")
    assert t.kind == "generic" and t.merchant_id is None


def test_customer_scope_inferred_from_customer_id():
    t = normalize_trigger("t", 1, {"kind": "recall_due", "merchant_id": "m", "customer_id": "c"})
    assert t.scope == "customer"
