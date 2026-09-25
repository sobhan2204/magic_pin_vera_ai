#!/usr/bin/env python3
"""Score the bot's real messages with magicpin's own judge prompt (judge_simulator.LLMScorer).

    python -X utf8 scripts/score_sample.py --bot-url http://localhost:8080 --judge-model qwen/qwen3.8-27b

Pushes only what it needs (categories + the sampled merchants/customers/triggers), ticks in simulated time (2026-04-26, so
nothing is expired), then asks a *judge* LLM to score every action on the five official dimensions.
Use a judge model that is NOT one of the bot's models so the bot's quota is untouched (JUDGE key defaults to GROQ_API_KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app.config import load_dotenv  # noqa: E402

os.environ.pop("VERA_NO_DOTENV", None)
load_dotenv()
from _dataset import load  # noqa: E402
import judge_simulator as js  # noqa: E402

T0 = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)
iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731


class HttpxJudge(js.LLMProvider):
    """OpenAI-compatible judge over httpx. (judge_simulator.GroqProvider uses urllib, whose default user agent Groq's edge
    rejects with HTTP 403.)"""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.groq.com/openai/v1") -> None:
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip("/")

    def name(self) -> str:
        return f"httpx ({self.model})"

    def complete(self, prompt: str, system: str = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        for attempt in range(6):
            r = httpx.post(f"{self.base_url}/chat/completions", timeout=90, headers={"Authorization": f"Bearer {self.api_key}"},
                           json={"model": self.model, "messages": msgs, "temperature": 0.2, "max_tokens": 2500})
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        raise RuntimeError("judge rate limited")


def pick(data: dict, which: str, per_kind: int) -> list[dict]:
    seeds = {t["id"] for t in load_seed_ids()}
    chosen, per, used_m = [], defaultdict(int), set()
    for t in data["triggers"]:
        is_seed = t["id"] in seeds
        if (which == "seeds" and not is_seed) or (which == "generated" and is_seed):
            continue
        if per[t["kind"]] >= per_kind or t["merchant_id"] in used_m:
            continue
        per[t["kind"]] += 1
        used_m.add(t["merchant_id"])
        chosen.append(t)
    return chosen


def load_seed_ids() -> list[dict]:
    import json
    return json.loads((ROOT / "dataset" / "triggers_seed.json").read_text(encoding="utf-8"))["triggers"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-url", required=True)
    ap.add_argument("--judge-model", default="qwen/qwen3.8-27b")
    ap.add_argument("--which", choices=["seeds", "generated", "both"], default="both")
    ap.add_argument("--per-kind", type=int, default=1)
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--teardown-first", action="store_true")
    a = ap.parse_args()
    key = os.environ.get("JUDGE_API_KEY") or os.environ.get("GROQ_API_KEY", "")
    if not key:
        print("set JUDGE_API_KEY or GROQ_API_KEY")
        return 2

    data = load()
    cats = data["categories"]
    mer = {m["merchant_id"]: m for m in data["merchants"]}
    cus = {c["customer_id"]: c for c in data["customers"]}
    trig = {t["id"]: t for t in data["triggers"]}
    sample = pick(data, a.which, a.per_kind)[: a.max]
    http = httpx.Client(timeout=60, base_url=a.bot_url.rstrip("/"))
    if a.teardown_first:
        http.post("/v1/teardown")
    print(f"pushing {len(sample)} triggers (+ merchants, customers, 5 categories) ...")
    for slug, c in cats.items():
        http.post("/v1/context", json={"scope": "category", "context_id": slug, "version": 1, "payload": c, "delivered_at": iso(T0)})
    for t in sample:
        m = mer[t["merchant_id"]]
        http.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m, "delivered_at": iso(T0)})
        if t.get("customer_id"):
            http.post("/v1/context", json={"scope": "customer", "context_id": t["customer_id"], "version": 1,
                                           "payload": cus[t["customer_id"]], "delivered_at": iso(T0)})
        http.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t, "delivered_at": iso(T0)})

    actions: list[dict] = []
    for i in range(0, len(sample), 20):
        r = http.post("/v1/tick", json={"now": iso(T0 + timedelta(minutes=5 * (i // 20))),
                                        "available_triggers": [t["id"] for t in sample[i:i + 20]]})
        actions += r.json().get("actions", [])
    print(f"bot returned {len(actions)} actions for {len(sample)} triggers")

    judge = HttpxJudge(key, a.judge_model)
    scorer = js.LLMScorer(judge, None)
    rows = []
    for act in actions:
        t = trig[act["trigger_id"]]
        m = mer[act["merchant_id"]]
        s = scorer.score(act, cats[m["category_slug"]], m, t, cus.get(act.get("customer_id")))
        fallback = s.hint.startswith("LLM scoring failed")
        rows.append((t["kind"], act, s, fallback))
        flag = " (JUDGE FAILED - heuristic)" if fallback else ""
        print(f"\n[{t['kind']}] {'placeholder' if (t.get('payload') or {}).get('placeholder') else 'real payload'}  "
              f"total={s.total}/50  spec={s.specificity} cat={s.category_fit} merch={s.merchant_fit} why={s.decision_quality} "
              f"eng={s.engagement_compulsion}{flag}\n   {act['body']}\n   hint: {s.hint}")
        time.sleep(3)

    ok = [r for r in rows if not r[3]]
    if ok:
        n = len(ok)
        avg = lambda f: sum(f(r[2]) for r in ok) / n  # noqa: E731
        print("\n== Averages over", n, "judged messages ==")
        for name, f in (("specificity", lambda s: s.specificity), ("category fit", lambda s: s.category_fit),
                        ("merchant fit", lambda s: s.merchant_fit), ("decision quality", lambda s: s.decision_quality),
                        ("engagement", lambda s: s.engagement_compulsion), ("TOTAL /50", lambda s: s.total)):
            print(f"  {name:<17} {avg(f):5.1f}")
        by = defaultdict(list)
        for k, _, s, _ in ok:
            by[k].append(s.total)
        print("  lowest kinds:", sorted(((sum(v) / len(v), k) for k, v in by.items()))[:6])
        print("SUMMARY", json.dumps({"n": n, "actions": len(actions), "triggers": len(sample), "spec": avg(lambda s: s.specificity),
                                     "cat": avg(lambda s: s.category_fit), "merch": avg(lambda s: s.merchant_fit),
                                     "why": avg(lambda s: s.decision_quality), "eng": avg(lambda s: s.engagement_compulsion),
                                     "total": avg(lambda s: s.total)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
