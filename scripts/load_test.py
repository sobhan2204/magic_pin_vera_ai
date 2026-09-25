#!/usr/bin/env python3
"""Steady-rate load test (default: the judge's 10 requests/second) against a running bot.

    python scripts/load_test.py --bot-url https://<project>.vercel.app --rps 10 --minutes 2

Mix: healthz 30%, idempotent context re-push 20%, empty tick 10%, reply 40%. It reports p50/p95/max latency and any non-200.
It never creates conversations that would send messages, so it is safe against a real deployment (run /v1/teardown after).
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import statistics
import time
from collections import Counter

import httpx

CTX = {"scope": "category", "context_id": "load_cat", "version": 1, "payload": {"slug": "load_cat"}}
REPLY = {"conversation_id": "conv_load_{n}", "merchant_id": "m_load", "customer_id": None, "from_role": "merchant",
         "message": "how much does it cost?", "received_at": "2026-04-26T10:42:00Z", "turn_number": 2}


async def one(client: httpx.AsyncClient, base: str, n: int, results: list) -> None:
    kind = ("health", "health", "health", "ctx", "ctx", "tick", "reply", "reply", "reply", "reply")[n % 10]
    t = time.monotonic()
    try:
        if kind == "health":
            r = await client.get(f"{base}/v1/healthz")
        elif kind == "ctx":
            r = await client.post(f"{base}/v1/context", json=CTX)
        elif kind == "tick":
            r = await client.post(f"{base}/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": []})
        else:
            r = await client.post(f"{base}/v1/reply", json={**REPLY, "conversation_id": f"conv_load_{n}"})
        results.append((kind, r.status_code, time.monotonic() - t))
    except Exception as e:
        results.append((kind, type(e).__name__, time.monotonic() - t))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-url", required=True)
    ap.add_argument("--rps", type=float, default=10)
    ap.add_argument("--minutes", type=float, default=1)
    a = ap.parse_args()
    base = a.bot_url.rstrip("/")
    total = int(a.rps * a.minutes * 60)
    results: list = []
    async with httpx.AsyncClient(timeout=35, limits=httpx.Limits(max_connections=100)) as client:
        tasks = []
        start = time.monotonic()
        for n in itertools.islice(itertools.count(), total):
            tasks.append(asyncio.create_task(one(client, base, n, results)))
            await asyncio.sleep(max(0.0, start + (n + 1) / a.rps - time.monotonic()))
        await asyncio.gather(*tasks)
    lat = sorted(r[2] for r in results)
    codes = Counter((r[0], r[1]) for r in results)
    print(f"requests={len(results)} target_rps={a.rps} duration={time.monotonic() - start:.1f}s")
    print(f"latency p50={statistics.median(lat):.3f}s p95={lat[int(len(lat) * .95) - 1]:.3f}s max={lat[-1]:.3f}s")
    for k, v in sorted(codes.items(), key=str):
        print(f"  {k[0]:<7} {k[1]}: {v}")
    bad = [r for r in results if r[1] != 200]
    slow = [r for r in results if r[2] > 25]
    print(f"non-200: {len(bad)}   slower than 25s: {len(slow)}")
    return 1 if bad or slow else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
