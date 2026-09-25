#!/usr/bin/env python3
"""Our own full judge-lifecycle harness. Runs against any BOT_URL (local or the Vercel deployment).

    python scripts/full_run.py --bot-url http://localhost:8080
    python scripts/full_run.py --bot-url https://<project>.vercel.app --teardown-first

Phases: warmup (5 categories, 50 merchants, 200 customers -> healthz must say 255) -> 12 simulated 5-minute ticks with
incremental trigger pushes and adaptive injections (category version bump with new digest items, merchant performance bumps,
new customers followed by recall_due 2 minutes later, an unseen trigger kind) -> simulated merchant replies (engaged,
auto-reply, hostile, commitment, off-topic, later).

Hard failures (exit code 1): non-200, malformed JSON/action, response > 25 s, >1 action per merchant per tick, >20 actions per
tick, verifier violations, repeated bodies, adaptive data ignored, replies that break the rules.
Warnings: numbers in a body that are not literally in the pushed contexts (derived numbers such as "4 months" are expected).
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _dataset import load  # noqa: E402
from app.humanize import fmt_num, parse_dt  # noqa: E402
from app.models import Action  # noqa: E402
from app.normalize import normalize_trigger  # noqa: E402
from app.replies.lint import lint_action_body  # noqa: E402
from app.resolver import build_factsheet  # noqa: E402
from app.verifier import extract_numbers, verify  # noqa: E402

T0 = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)
SLOW_S = 25.0


class Harness:
    def __init__(self, url: str, quiet: bool) -> None:
        self.url, self.quiet = url.rstrip("/"), quiet
        self.http = httpx.Client(timeout=35)
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.latencies: list[tuple[str, float]] = []
        self.ctx: dict[tuple[str, str], tuple[int, dict]] = {}       # what we pushed (latest version per id)
        self.bodies: Counter = Counter()
        self.actions: list[dict] = []

    # --- plumbing ----------------------------------------------------------------------------------------------
    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  [FAIL] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def call(self, method: str, path: str, body=None, expect=(200,)) -> tuple[int, dict]:
        t = time.monotonic()
        try:
            r = self.http.request(method, self.url + path, json=body)
        except Exception as e:
            self.fail(f"{method} {path}: {type(e).__name__}: {e}")
            return 0, {}
        dt = time.monotonic() - t
        self.latencies.append((path, dt))
        if dt > SLOW_S:
            self.fail(f"{method} {path} took {dt:.1f}s (> {SLOW_S}s)")
        if r.status_code not in expect:
            self.fail(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.status_code, r.json()
        except ValueError:
            self.fail(f"{method} {path}: response is not JSON: {r.text[:120]!r}")
            return r.status_code, {}

    def push(self, scope: str, cid: str, payload: dict, version: int = 1, when: datetime = T0) -> None:
        code, j = self.call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version, "payload": payload,
                                                     "delivered_at": when.isoformat().replace("+00:00", "Z")}, expect=(200,))
        if code == 200 and j.get("accepted"):
            self.ctx[(scope, cid)] = (version, payload)
        elif code == 200:
            self.fail(f"context {scope}/{cid} v{version} not accepted: {j}")

    # --- checks --------------------------------------------------------------------------------------------------
    def literal_numbers(self, a: dict) -> set[str]:
        blobs = []
        for scope, cid in (("merchant", a["merchant_id"]), ("customer", a.get("customer_id")), ("trigger", a["trigger_id"])):
            if cid and (scope, cid) in self.ctx:
                blobs.append(json.dumps(self.ctx[(scope, cid)][1], ensure_ascii=False))
        m = self.ctx.get(("merchant", a["merchant_id"]))
        if m:
            cat = self.ctx.get(("category", m[1].get("category_slug")))
            if cat:
                blobs.append(json.dumps(cat[1], ensure_ascii=False))
        nums = set()
        for blob in blobs:
            nums |= extract_numbers(blob)
            for f in re.findall(r"\b0\.\d+\b", blob):                       # fractions shown as percentages
                nums |= extract_numbers(str(round(float(f) * 100, 1)))
        return nums

    def check_action(self, a: dict, now: datetime) -> None:
        try:
            Action.model_validate(a)
        except Exception as e:
            self.fail(f"malformed action {a.get('trigger_id')}: {e}")
            return
        if not a["body"].strip():
            self.fail(f"empty body {a['trigger_id']}")
        self.bodies[a["body"]] += 1
        if self.bodies[a["body"]] > 1:
            self.fail(f"repeated body verbatim: {a['body'][:80]}")
        if a["conversation_id"] in {x["conversation_id"] for x in self.actions}:
            self.fail(f"conversation_id reused: {a['conversation_id']}")
        m, tr = self.ctx.get(("merchant", a["merchant_id"])), self.ctx.get(("trigger", a["trigger_id"]))
        if not (m and tr):
            self.fail(f"action for context we never pushed: {a['merchant_id']} / {a['trigger_id']}")
            return
        cat = self.ctx.get(("category", m[1]["category_slug"]))
        cu = self.ctx.get(("customer", a["customer_id"])) if a.get("customer_id") else None
        trig = normalize_trigger(a["trigger_id"], tr[0], tr[1])
        fs = build_factsheet(trig, m[1], cat[1], cu[1] if cu else None, now) if cat else None
        if fs is None:
            self.fail(f"action {a['trigger_id']} but facts cannot be resolved from what we pushed")
            return
        if a["send_as"] != fs.send_as:
            self.fail(f"send_as {a['send_as']} != {fs.send_as} for {a['trigger_id']}")
        v = verify({"body": a["body"], "cta": a["cta"]}, fs, [])
        v = [x for x in v if "duplicate" not in x]
        if v:
            self.fail(f"verifier: {a['trigger_id']}: {v} :: {a['body'][:120]}")
        stray = extract_numbers(a["body"], strip_allowed=True) - self.literal_numbers(a)
        if stray:
            self.warn(f"{a['trigger_id']}: numbers not literally in contexts (derived?): {sorted(stray)}")
        if fs.language == "hi-en" and not re.search(r"\b(aap|hai|kya|kar|main|hum|ke|liye|ya)\b", a["body"].lower()):
            self.fail(f"language preference ignored for {a['trigger_id']}")
        self.actions.append(a)

    def show(self, a: dict) -> None:
        if not self.quiet:
            print(f"    -> [{a['send_as']}] {a['merchant_id'][:26]} / {a['trigger_id'][:34]}\n       {a['body']}\n       why: {a['rationale'][:150]}")

    def tick(self, i: int, now: datetime, ids: list[str]) -> list[dict]:
        code, j = self.call("POST", "/v1/tick", {"now": now.isoformat().replace("+00:00", "Z"), "available_triggers": ids})
        acts = j.get("actions", []) if code == 200 else []
        if not isinstance(acts, list):
            self.fail("tick: actions is not a list")
            return []
        if len(acts) > 20:
            self.fail(f"tick {i}: {len(acts)} actions (> 20)")
        per_merchant = Counter(a.get("merchant_id") for a in acts)
        for mid, n in per_merchant.items():
            if n > 1:
                self.fail(f"tick {i}: {n} actions for merchant {mid}")
        for a in acts:
            self.check_action(a, now)
        return acts

    def reply(self, conv: str, mid: str, cid, msg: str, turn: int, role: str = "merchant") -> dict:
        _, j = self.call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": cid, "from_role": role,
                                               "message": msg, "received_at": T0.isoformat().replace("+00:00", "Z"),
                                               "turn_number": turn})
        if j.get("action") not in ("send", "wait", "end"):
            self.fail(f"reply: bad action {j}")
        if j.get("action") == "send" and not (j.get("body") or "").strip():
            self.fail(f"reply: send with empty body for {msg!r}")
        if j.get("action") == "wait" and not isinstance(j.get("wait_seconds"), int):
            self.fail(f"reply: wait without wait_seconds for {msg!r}")
        if not j.get("rationale"):
            self.fail(f"reply: missing rationale for {msg!r}")
        return j

    def persona_replies(self, acts: list[dict], tick_no: int) -> None:
        personas = ["engaged", "auto", "commit", "hostile", "offtopic", "later", "question"]
        for n, a in enumerate(acts):
            p = personas[(n + tick_no) % len(personas)]
            mid, cid, conv = a["merchant_id"], a.get("customer_id"), a["conversation_id"]
            role = "customer" if cid else "merchant"
            if p == "engaged":
                r = self.reply(conv, mid, cid, "Yes please send the details", 2, role)
                if r["action"] != "send":
                    self.fail(f"engaged reply should get an answer, got {r}")
            elif p == "commit":
                r = self.reply(conv, mid, cid, "Ok lets do it. Whats next?", 2, role)
                if r["action"] == "send" and lint_action_body(r["body"]) and not cid:
                    self.fail(f"commitment answered with qualifying/no-action text: {r['body']}")
            elif p == "auto":
                canned = "Thank you for contacting us! Our team will respond shortly."
                r1 = self.reply(f"{conv}_a1", mid, cid, canned, 2, role)
                r2 = self.reply(f"{conv}_a2", mid, cid, canned, 3, role)
                if r2["action"] != "end":
                    self.fail(f"auto-reply repeated across conversations was not ended: {r2}")
            elif p == "hostile":
                r = self.reply(conv, mid, cid, "This is useless. Why are you bothering me", 2, role)
                if r["action"] == "send" and "sorry" not in r["body"].lower():
                    self.warn(f"hostile reply without apology: {r['body'][:80]}")
                r = self.reply(conv, mid, cid, "Btw can you also help me with my GST filing?", 3, role)
                if r["action"] not in ("send", "end"):
                    self.fail(f"off-topic after hostility should stay on mission or end, got {r}")
            elif p == "offtopic":
                self.reply(conv, mid, cid, "Can you help me with a home loan?", 2, role)
            elif p == "later":
                r = self.reply(conv, mid, cid, "busy right now, call later", 2, role)
                if r["action"] != "wait":
                    self.fail(f"'busy' should wait, got {r}")
            else:
                self.reply(conv, mid, cid, "how much does this cost?", 2, role)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-url", required=True)
    ap.add_argument("--ticks", type=int, default=12)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--teardown-first", action="store_true", help="POST /v1/teardown before starting (clean slate)")
    ap.add_argument("--no-replies", action="store_true")
    args = ap.parse_args()

    h = Harness(args.bot_url, args.quiet)
    data = load()
    cats, merchants, customers, triggers = data["categories"], data["merchants"], data["customers"], data["triggers"]

    print("== Warmup ==")
    if args.teardown_first:
        h.call("POST", "/v1/teardown")
    code, j = h.call("GET", "/v1/healthz")
    print(f"healthz: {j}")
    _, meta = h.call("GET", "/v1/metadata")
    for k in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        if k not in meta:
            h.fail(f"metadata missing {k}")
    for slug, c in cats.items():
        h.push("category", slug, c)
    for m in merchants:
        h.push("merchant", m["merchant_id"], m)
    for c in customers:
        h.push("customer", c["customer_id"], c)
    time.sleep(0.5)
    _, j = h.call("GET", "/v1/healthz")
    loaded = j.get("contexts_loaded", {})
    print(f"contexts_loaded after warmup: {loaded}")
    if (loaded.get("category"), loaded.get("merchant"), loaded.get("customer")) != (len(cats), len(merchants), len(customers)):
        h.fail(f"warmup counts wrong: {loaded} (expected {len(cats)}/{len(merchants)}/{len(customers)})")
    code, again = h.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 1,
                                                 "payload": cats["dentists"]}, expect=(200, 409))
    if code == 409 or (code == 200 and again.get("accepted")):
        print(f"re-push same version -> HTTP {code} {again.get('reason', 'accepted (idempotent no-op)')}")
    code, stale = h.call("POST", "/v1/context", {"scope": "category", "context_id": "dentists", "version": 0,
                                                 "payload": cats["dentists"]}, expect=(409,))
    if stale.get("reason") != "stale_version":
        h.fail(f"stale version answer wrong: {stale}")

    print("== Test window ==")
    pending = list(triggers)                        # pushed incrementally, ~8 per tick
    pushed_ids: list[str] = []
    inj_expect: list[tuple[str, str, str]] = []     # (trigger_id, needle, what)
    new_customers: dict[str, tuple[dict, dict]] = {}
    per_tick = max(1, len(pending) // max(args.ticks - 2, 1))
    by_mid = {m["merchant_id"]: m for m in merchants}
    per_cat: dict[str, list[dict]] = defaultdict(list)
    for m in merchants:
        per_cat[m["category_slug"]].append(m)

    for i in range(args.ticks):
        now = T0 + timedelta(minutes=5 * i)
        print(f"-- tick {i} @ {now:%H:%M} --")
        # incremental triggers
        for t in pending[:per_tick]:
            h.push("trigger", t["id"], t, when=now)
            pushed_ids.append(t["id"])
        pending = pending[per_tick:]

        if i == 3:                                   # new digest items arrive as a new category version
            for slug, c in cats.items():
                c2 = copy.deepcopy(c)
                for k in range(1, 6):
                    c2["digest"].append({"id": f"d_INJ_{slug}_{k}", "kind": "research",
                                         "title": f"Injected {slug} finding {k}: turnaround cut by {10 + k} percent in a 300-site audit",
                                         "source": f"Injected Journal 2026, p.{k}", "trial_n": 300 + k,
                                         "summary": f"A 300-site audit found turnaround cut by {10 + k} percent."})
                cats[slug] = c2
                h.push("category", slug, c2, version=2, when=now)
                m = per_cat[slug][-1]
                tid = f"trg_inj_digest_{slug}"
                h.push("trigger", tid, {"id": tid, "scope": "merchant", "kind": "research_digest", "source": "external",
                                        "merchant_id": m["merchant_id"], "customer_id": None,
                                        "payload": {"category": slug, "top_item_id": f"d_INJ_{slug}_1"}, "urgency": 4,
                                        "suppression_key": f"inj:{slug}", "expires_at": "2026-12-01T00:00:00Z"}, when=now)
                pushed_ids.append(tid)
                inj_expect.append((tid, f"Injected Journal 2026, p.1", f"new digest item for {slug}"))
        if i == 4:                                   # performance snapshots change for 10 merchants
            for m in merchants[40:50]:
                m2 = copy.deepcopy(by_mid[m["merchant_id"]])
                m2["performance"]["views"] = int(m2["performance"].get("views", 1000)) + 1234
                by_mid[m2["merchant_id"]] = m2
                h.push("merchant", m2["merchant_id"], m2, version=2, when=now)
            for m in merchants[40:43]:               # renewal pushes that must use the bumped numbers
                tid = f"trg_inj_renewal_{m['merchant_id'][:5]}"
                h.push("trigger", tid, {"id": tid, "scope": "merchant", "kind": "renewal_due", "source": "internal",
                                        "merchant_id": m["merchant_id"], "customer_id": None,
                                        "payload": {"days_remaining": 9, "plan": "Pro", "renewal_amount": 4999}, "urgency": 4,
                                        "suppression_key": f"inj_renew:{m['merchant_id']}", "expires_at": "2026-12-01T00:00:00Z"}, when=now)
                pushed_ids.append(tid)
                inj_expect.append((tid, fmt_num(by_mid[m["merchant_id"]]["performance"]["views"]), "updated performance numbers"))
        if i == 6:                                   # new customers for 5 merchants ...
            for k, m in enumerate(merchants[20:25]):
                cid = f"c_inj_{k}"
                cu = {"customer_id": cid, "merchant_id": m["merchant_id"],
                      "identity": {"name": f"Newcust{k}", "language_pref": "english"},
                      "relationship": {"first_visit": "2026-01-01", "last_visit": "2026-02-10", "visits_total": 2},
                      "state": "lapsed_soft", "preferences": {"channel": "whatsapp"},
                      "consent": {"opted_in_at": "2026-01-01", "scope": ["recall_reminders"]}}
                h.push("customer", cid, cu, when=now)
                new_customers[cid] = (cu, m)
        if i == 7:                                   # ... and their recall_due trigger ~2 minutes later
            for cid, (cu, m) in new_customers.items():
                tid = f"trg_inj_recall_{cid}"
                h.push("trigger", tid, {"id": tid, "scope": "customer", "kind": "recall_due", "source": "internal",
                                        "merchant_id": m["merchant_id"], "customer_id": cid,
                                        "payload": {"service_due": "regular_checkup"}, "urgency": 3,
                                        "suppression_key": f"inj_recall:{cid}", "expires_at": "2026-12-01T00:00:00Z"}, when=now)
                pushed_ids.append(tid)
                inj_expect.append((tid, cu["identity"]["name"], "new customer used"))
        if i == 8:                                   # a trigger kind nobody has seen
            m = merchants[30]
            h.push("trigger", "trg_inj_unseen", {"id": "trg_inj_unseen", "scope": "merchant", "kind": "quantum_event",
                                                 "source": "external", "merchant_id": m["merchant_id"], "customer_id": None,
                                                 "payload": {"note": "anniversary", "years": 8}, "urgency": 3,
                                                 "suppression_key": "inj:unseen", "expires_at": "2026-12-01T00:00:00Z"}, when=now)
            pushed_ids.append("trg_inj_unseen")
            inj_expect.append(("trg_inj_unseen", "8", "unseen trigger kind handled"))

        acts = h.tick(i, now, list(pushed_ids))
        print(f"   {len(acts)} action(s)")
        for a in acts:
            h.show(a)
        if not args.no_replies:
            h.persona_replies(acts[:8], i)

    print("== Adaptation checks ==")
    sent = {a["trigger_id"]: a for a in h.actions}
    for tid, needle, what in inj_expect:
        a = sent.get(tid)
        if a is None:
            h.warn(f"{what}: no action for {tid} (suppressed by restraint/opt-out is possible)")
        elif needle not in a["body"]:
            h.fail(f"{what}: body for {tid} does not use the injected data ({needle!r}): {a['body'][:140]}")
        else:
            print(f"  ok  {what}: {tid}")

    print("== Summary ==")
    lat = sorted(d for _, d in h.latencies)
    p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] if lat else 0
    print(f"actions sent: {len(h.actions)}  unique bodies: {len(h.bodies)}  requests: {len(lat)}  "
          f"latency p50={p(.5):.2f}s p95={p(.95):.2f}s max={lat[-1] if lat else 0:.2f}s")
    print(f"actions by template: {dict(Counter(a['template_name'] for a in h.actions))}")
    for w in h.warnings[:15]:
        print(f"  [warn] {w}")
    if len(h.warnings) > 15:
        print(f"  ... {len(h.warnings) - 15} more warnings")
    if h.failures:
        print(f"\nFAILED: {len(h.failures)} hard failure(s)")
        return 1
    print("\nPASS: zero hard failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
