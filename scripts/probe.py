#!/usr/bin/env python3
"""Check your real services from .env: Redis latency, and one real writer call per configured LLM target.

    python -X utf8 scripts/probe.py              # both
    python -X utf8 scripts/probe.py --redis      # only Redis
    python -X utf8 scripts/probe.py --llm        # only the LLM targets (spends ~3k tokens per target)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import get_settings, load_dotenv  # noqa: E402

os.environ.pop("VERA_NO_DOTENV", None)
load_dotenv()

from _dataset import load  # noqa: E402
from app.humanize import parse_dt  # noqa: E402
from app.llm.client import LLMError, chat_completion  # noqa: E402
from app.llm.router import build_targets  # noqa: E402
from app.normalize import normalize_trigger  # noqa: E402
from app.playbooks import get_playbook  # noqa: E402
from app.prompts import MAX_TOKENS, MESSAGE_SCHEMA, build_writer_messages, parse_output  # noqa: E402
from app.resolver import build_factsheet  # noqa: E402
from app.verifier import verify  # noqa: E402
from app.writer import check  # noqa: E402


async def probe_redis() -> None:
    from app.store.redis_store import RedisStore
    s = get_settings()
    st = RedisStore(s.upstash_url, s.upstash_token, "vera_probe:")
    print(f"== Redis ({s.upstash_url.split('//')[-1].split('.')[0]}...) ==")
    lat: dict[str, list[float]] = {}

    async def timed(name, coro):
        t = time.perf_counter()
        r = await coro
        lat.setdefault(name, []).append(time.perf_counter() - t)
        return r

    for i in range(5):
        await timed("lua put_context", st.put_context("merchant", "probe", i + 1, {"n": i}))
        await timed("hmget get_context", st.get_context("merchant", "probe"))
        await timed("pipeline x10 mget_contexts", st.mget_contexts("merchant", ["probe"] * 10))
        await timed("hgetall health", st.health())
        await timed("set_nx", st.set_nx(f"probe:{i}", "1", 30))
    t = time.perf_counter()
    await asyncio.gather(*(st.get_context("merchant", "probe") for _ in range(20)))
    lat["20 parallel hmget (wall)"] = [time.perf_counter() - t]
    for k, v in lat.items():
        print(f"  {k:<30} avg {sum(v) / len(v) * 1000:6.0f} ms   max {max(v) * 1000:6.0f} ms")
    await st.wipe()


async def probe_llm() -> None:
    s = get_settings()
    data = load()
    t = next(x for x in data["triggers"] if x["id"] == "trg_004_perf_dip_bharat")
    m = next(x for x in data["merchants"] if x["merchant_id"] == t["merchant_id"])
    trig = normalize_trigger(t["id"], 1, t)
    fs = build_factsheet(trig, m, data["categories"][m["category_slug"]], None, parse_dt("2026-04-26T10:35:00Z"))
    messages = build_writer_messages(fs, get_playbook(trig.kind), trig)
    print(f"== LLM targets (prompt ~{sum(len(x['content']) for x in messages) // 4} tokens) ==")
    for tg in build_targets(s):
        t0 = time.perf_counter()
        try:
            r = await chat_completion(base_url=tg.base_url, api_key=tg.api_key, model=tg.model, messages=messages,
                                      max_tokens=MAX_TOKENS, seed=s.llm_seed, timeout_s=30, schema=MESSAGE_SCHEMA,
                                      json_mode=tg.json_mode, reasoning_effort=tg.reasoning_effort)
        except LLMError as e:
            print(f"  [{tg.name}] {tg.model}: FAILED {e}")
            continue
        dt = time.perf_counter() - t0
        try:
            out = parse_output(r.text)
            problems = check(out, fs, [])
        except Exception as e:
            out, problems = None, [f"unparseable: {e}"]
        print(f"  [{tg.name}] {tg.model}: {dt:.2f}s  tokens prompt={r.prompt_tokens} completion={r.completion_tokens}")
        print(f"      rate-limit headers: {r.headers}")
        print(f"      body: {out['body'] if out else r.text[:300]!r}")
        print(f"      verifier: {'PASS' if not problems else problems}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redis", action="store_true")
    ap.add_argument("--llm", action="store_true")
    a = ap.parse_args()
    both = not (a.redis or a.llm)
    if a.redis or both:
        await probe_redis()
    if a.llm or both:
        await probe_llm()


asyncio.run(main())
