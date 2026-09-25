import copy

from conftest import NOW_ISO, push, push_seed

REQUIRED = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"}


async def tick(client, ids, now=NOW_ISO):
    r = await client.post("/v1/tick", json={"now": now, "available_triggers": ids})
    assert r.status_code == 200
    return r.json()["actions"]


async def test_action_shape_and_contents(client, dataset):
    t, m = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    acts = await tick(client, [t["id"]])
    assert len(acts) == 1
    a = acts[0]
    assert set(a) == REQUIRED
    assert a["conversation_id"] == f"conv_{m['merchant_id']}_{t['id']}"
    assert a["send_as"] == "vera" and a["customer_id"] is None
    assert a["template_name"] == "vera_research_digest_v1" and len(a["template_params"]) == 3
    assert a["suppression_key"] == "research:dentists:2026-W17"
    assert "research digest trigger" in a["rationale"] and "JIDA Oct 2026, p.14" in a["rationale"]
    assert a["body"].startswith("Dr. Meera") or "Dr. Meera" in a["body"]


async def test_same_trigger_never_sent_twice(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    assert len(await tick(client, [t["id"]])) == 1
    assert await tick(client, [t["id"]]) == []
    assert await tick(client, [t["id"], t["id"]]) == []


async def test_concurrent_ticks_send_once(client, dataset):
    import asyncio
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")
    results = await asyncio.gather(*(tick(client, [t["id"]]) for _ in range(5)))
    assert sum(len(r) for r in results) == 1


async def test_tick_months_after_expires_at_still_sends(client, dataset):
    """available_triggers is the judge's statement that a trigger is active: expires_at only affects ranking."""
    t, _ = await push_seed(client, dataset, "trg_001_research_digest_dentists")          # expires 2026-05-03
    acts = await tick(client, [t["id"]], now="2026-09-25T09:00:00Z")                      # ~5 months later
    assert len(acts) == 1 and acts[0]["trigger_id"] == t["id"] and acts[0]["body"].strip()


async def test_every_seed_trigger_still_sends_long_after_expiry(client, dataset):
    for m in dataset["merchants"]:
        pass
    ids = []
    for slug, cat in dataset["categories"].items():
        await push(client, "category", slug, cat)
    seen_merchants = set()
    for t in dataset["trigger_seeds"]:
        if t["merchant_id"] in seen_merchants:
            continue
        seen_merchants.add(t["merchant_id"])
        m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
        await push(client, "merchant", m["merchant_id"], m)
        if t.get("customer_id"):
            await push(client, "customer", t["customer_id"], next(c for c in dataset["customers"] if c["customer_id"] == t["customer_id"]))
        await push(client, "trigger", t["id"], t)
        ids.append(t["id"])
    acts = await tick(client, ids, now="2027-03-01T09:00:00Z")                            # ~10 months after most expiries
    assert {a["trigger_id"] for a in acts} == set(ids)
    for a in acts:                                                                        # no negative / silly relative-time facts
        assert "-" not in " ".join(w for w in a["body"].split() if w.rstrip("dayswkmonth").lstrip("-").isdigit() and w.startswith("-"))
        assert "days ago days" not in a["body"] and "-1 day" not in a["body"]


async def test_missing_contexts_yield_no_action(client, dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_001_research_digest_dentists")
    await push(client, "trigger", t["id"], t)                      # no merchant / category pushed
    assert await tick(client, [t["id"]]) == []


async def test_customer_consent_is_enforced(client, dataset):
    t, _ = await push_seed(client, dataset, "trg_003_recall_due_priya")
    ok = await tick(client, [t["id"]], now="2026-09-25T09:00:00Z")
    assert len(ok) == 1 and ok[0]["customer_id"] == "c_001_priya_for_m001"
    assert ok[0]["conversation_id"].endswith("_c_001_priya_for_m001")

    t2 = copy.deepcopy(t)
    t2["id"], t2["suppression_key"] = "trg_new_recall", "recall:other"
    cust = next(c for c in dataset["customers"] if c["customer_id"] == "c_001_priya_for_m001")
    cust = {**cust, "consent": {"opted_in_at": "2025-11-04", "scope": ["promotional_offers"]}}
    await push(client, "customer", cust["customer_id"], cust, version=2)
    t2["customer_id"] = cust["customer_id"]
    await push(client, "trigger", t2["id"], t2)
    assert await tick(client, [t2["id"]], now="2026-09-25T09:00:00Z") == []


async def test_missing_customer_yields_no_action(client, dataset):
    t = next(x for x in dataset["trigger_seeds"] if x["id"] == "trg_003_recall_due_priya")
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == t["merchant_id"])
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    await push(client, "merchant", m["merchant_id"], m)
    await push(client, "trigger", t["id"], t)
    assert await tick(client, [t["id"]], now="2026-09-25T09:00:00Z") == []


async def test_one_action_per_merchant_per_tick(client, dataset):
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "merchant", m["merchant_id"], m)
    ids = ["trg_001_research_digest_dentists", "trg_002_compliance_dci_radiograph", "trg_022_cde_webinar_dentists",
           "trg_023_competitor_opened_dentist"]
    for tid in ids:
        await push(client, "trigger", tid, next(x for x in dataset["trigger_seeds"] if x["id"] == tid))
    acts = await tick(client, ids)
    assert len(acts) == 1
    assert acts[0]["trigger_id"] == "trg_002_compliance_dci_radiograph"     # urgency 4 beats the rest


async def test_restraint_after_unanswered_send(client, dataset):
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "merchant", m["merchant_id"], m)
    for tid in ("trg_001_research_digest_dentists", "trg_022_cde_webinar_dentists"):
        await push(client, "trigger", tid, next(x for x in dataset["trigger_seeds"] if x["id"] == tid))
    assert len(await tick(client, ["trg_022_cde_webinar_dentists"])) == 1
    assert await tick(client, ["trg_001_research_digest_dentists"]) == []   # awaiting reply, urgency 2
    # merchant replies -> conversation is no longer open
    await client.post("/v1/reply", json={"conversation_id": "conv_x", "merchant_id": m["merchant_id"], "from_role": "merchant",
                                         "message": "interesting, tell me more", "turn_number": 2})
    assert len(await tick(client, ["trg_001_research_digest_dentists"], now="2026-04-26T13:00:00Z")) == 1


async def test_cap_20_and_one_per_merchant_at_scale(client, dataset):
    for slug, cat in dataset["categories"].items():
        await push(client, "category", slug, cat)
    ids = []
    for i, m in enumerate(dataset["merchants"][:40]):
        await push(client, "merchant", m["merchant_id"], m)
        tid = f"trg_scale_{i:02d}"
        ids.append(tid)
        await push(client, "trigger", tid, {"kind": "perf_dip", "merchant_id": m["merchant_id"], "payload": {},
                                            "urgency": 2, "suppression_key": f"s:{i}", "expires_at": "2026-12-01T00:00:00Z"})
    acts = await tick(client, ids)
    assert len(acts) == 20 and len({a["merchant_id"] for a in acts}) == 20
    assert len({a["conversation_id"] for a in acts}) == 20


async def test_shared_suppression_key_across_merchants_does_not_block(client, dataset):
    cat = dataset["categories"]["dentists"]
    await push(client, "category", "dentists", cat)
    dentists = [m for m in dataset["merchants"] if m["category_slug"] == "dentists"][:3]
    ids = []
    for i, m in enumerate(dentists):
        await push(client, "merchant", m["merchant_id"], m)
        tid = f"trg_r{i}"
        ids.append(tid)
        await push(client, "trigger", tid, {"kind": "research_digest", "merchant_id": m["merchant_id"],
                                            "payload": {"top_item_id": "d_2026W17_jida_fluoride"}, "urgency": 2,
                                            "suppression_key": "research:dentists:2026-W17", "expires_at": "2026-12-01T00:00:00Z"})
    assert len(await tick(client, ids)) == 3


async def test_category_version_bump_new_digest_item_is_used(client, dataset):
    """Adaptive injection: the digest item is resolved from the LATEST category version at decision time."""
    cat = copy.deepcopy(dataset["categories"]["dentists"])
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "category", "dentists", cat, version=1)
    await push(client, "merchant", m["merchant_id"], m)
    trig = {"kind": "research_digest", "merchant_id": m["merchant_id"], "payload": {"top_item_id": "d_NEW_item"},
            "urgency": 2, "suppression_key": "research:new", "expires_at": "2026-12-01T00:00:00Z"}
    await push(client, "trigger", "trg_new", trig)
    assert await tick(client, ["trg_new"]) == []                    # item not known yet -> missing join
    cat["digest"].append({"id": "d_NEW_item", "kind": "research", "title": "Zirconia crowns fracture 12% less in molars",
                          "source": "IJDR Nov 2026, p.3", "trial_n": 640, "summary": "A 640-patient study found fewer fractures."})
    await push(client, "category", "dentists", cat, version=2)
    acts = await tick(client, ["trg_new"])
    assert len(acts) == 1 and "IJDR Nov 2026, p.3" in acts[0]["body"] and "12%" in acts[0]["body"]


async def test_merchant_performance_bump_changes_numbers(client, dataset):
    m = copy.deepcopy(next(x for x in dataset["merchants"] if x["merchant_id"] == "m_003_studio11_salon_hyderabad"))
    await push(client, "category", "salons", dataset["categories"]["salons"])
    await push(client, "merchant", m["merchant_id"], m, version=1)
    m["performance"]["views"] = 7777
    await push(client, "merchant", m["merchant_id"], m, version=2)
    tid = "trg_renew"
    await push(client, "trigger", tid, {"kind": "renewal_due", "merchant_id": m["merchant_id"],
                                        "payload": {"days_remaining": 9, "plan": "Pro", "renewal_amount": 4999},
                                        "urgency": 4, "suppression_key": "renew:x", "expires_at": "2026-12-01T00:00:00Z"})
    acts = await tick(client, [tid])
    assert "7,777" in acts[0]["body"] and "₹4,999" in acts[0]["body"]


async def test_unseen_trigger_kind_is_handled_by_generic_playbook(client, dataset):
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    await push(client, "merchant", m["merchant_id"], m)
    await push(client, "trigger", "trg_unseen", {"kind": "quantum_event", "merchant_id": m["merchant_id"],
                                                 "payload": {"note": "clinic anniversary", "years": 8}, "urgency": 3,
                                                 "suppression_key": "q:1", "expires_at": "2026-12-01T00:00:00Z"})
    acts = await tick(client, ["trg_unseen"])
    assert len(acts) == 1 and acts[0]["template_name"] == "vera_quantum_event_v1" and "8" in acts[0]["body"]


async def test_new_customer_followed_by_recall_trigger(client, dataset):
    """Judge pushes a brand-new customer, then a recall_due trigger ~2 minutes later."""
    m = next(x for x in dataset["merchants"] if x["merchant_id"] == "m_001_drmeera_dentist_delhi")
    await push(client, "category", "dentists", dataset["categories"]["dentists"])
    await push(client, "merchant", m["merchant_id"], m)
    cust = {"customer_id": "c_new_1", "merchant_id": m["merchant_id"],
            "identity": {"name": "Ishaan", "language_pref": "english"},
            "relationship": {"last_visit": "2026-03-01", "visits_total": 2}, "state": "lapsed_soft",
            "consent": {"opted_in_at": "2026-01-01", "scope": ["recall_reminders"]}}
    await push(client, "customer", "c_new_1", cust)
    await push(client, "trigger", "trg_new_recall", {"kind": "recall_due", "scope": "customer", "merchant_id": m["merchant_id"],
                                                     "customer_id": "c_new_1", "payload": {"service_due": "6_month_cleaning"},
                                                     "urgency": 3, "suppression_key": "recall:c_new_1", "expires_at": "2026-12-01T00:00:00Z"})
    acts = await tick(client, ["trg_new_recall"])
    assert len(acts) == 1 and acts[0]["send_as"] == "merchant_on_behalf"
    assert acts[0]["body"].startswith("Hi Ishaan,") and "1 months" not in acts[0]["body"]
