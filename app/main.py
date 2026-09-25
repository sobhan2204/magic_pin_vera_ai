"""FastAPI app. Every route guarantees a valid response shape, even on internal failure."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import MAX_CONTEXT_BYTES, get_settings, load_dotenv
from .models import ReplyOut
from .replies.handler import handle_reply
from .store import get_store
from .store.base import SCOPES
from .tick import run_tick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vera.api")

load_dotenv()                      # local runs only; no-op on Vercel and under pytest

app = FastAPI(title="Vera merchant bot", docs_url=None, redoc_url=None, openapi_url=None)

SAFE_REPLY = {"action": "wait", "wait_seconds": 1800,
              "rationale": "Temporary problem on our side; holding this turn instead of sending something unsafe."}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _read_json(request: Request) -> tuple[Optional[Any], int]:
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8")) if raw else None, len(raw)
    except (ValueError, UnicodeDecodeError):
        return None, len(raw)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    rid = uuid.uuid4().hex[:8]
    log.exception("unhandled error rid=%s path=%s", rid, request.url.path)
    path = request.url.path
    if path.endswith("/tick"):
        return JSONResponse({"actions": []})
    if path.endswith("/reply"):
        return JSONResponse(SAFE_REPLY)
    if path.endswith("/context"):
        return JSONResponse({"accepted": False, "reason": "malformed", "details": f"internal error {rid}"}, status_code=400)
    if path.endswith("/healthz"):
        return JSONResponse({"status": "degraded", "uptime_seconds": 0,
                             "contexts_loaded": {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}})
    return JSONResponse({"ok": False, "error": f"internal error {rid}"})


# ---------------------------------------------------------------------------------------------
@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    data, size = await _read_json(request)
    if size > MAX_CONTEXT_BYTES:
        return JSONResponse({"accepted": False, "reason": "payload_too_large",
                             "details": f"{size} bytes exceeds {MAX_CONTEXT_BYTES}"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "body must be a JSON object"}, status_code=400)
    scope, cid, version, payload = data.get("scope"), data.get("context_id"), data.get("version"), data.get("payload")
    if scope not in SCOPES:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {list(SCOPES)}"},
                            status_code=400)
    if not isinstance(cid, str) or not cid.strip():
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "context_id is required"}, status_code=400)
    if isinstance(version, bool) or not isinstance(version, (int, float, str)):
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "version must be an integer"}, status_code=400)
    try:
        version = int(version)
    except (TypeError, ValueError):
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "version must be an integer"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "payload must be an object"}, status_code=400)

    try:
        status, current = await get_store().put_context(scope, cid, version, payload)
    except Exception:
        log.exception("context store failure")
        return JSONResponse({"accepted": False, "reason": "malformed", "details": "storage temporarily unavailable"},
                            status_code=400)
    if status == "stale":
        return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": current}, status_code=409)
    body = {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": _now_iso()}
    if status == "same":
        body["duplicate"] = True                          # idempotent no-op: same (context_id, version) pushed again
    return JSONResponse(body)


@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    data, _ = await _read_json(request)
    settings = get_settings()
    if not isinstance(data, dict):
        return JSONResponse({"actions": []})
    try:
        actions = await asyncio.wait_for(run_tick(get_store(), settings, data), timeout=settings.tick_deadline_s + 4)
    except asyncio.TimeoutError:
        log.error("tick exceeded %.1fs deadline", settings.tick_deadline_s)
        actions = []
    except Exception:
        log.exception("tick failed; returning no actions")
        actions = []
    return JSONResponse({"actions": actions})


@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    data, _ = await _read_json(request)
    settings = get_settings()
    if not isinstance(data, dict):
        return JSONResponse(SAFE_REPLY)
    try:
        out: ReplyOut = await asyncio.wait_for(handle_reply(get_store(), settings, data), timeout=settings.reply_deadline_s)
        return JSONResponse(out.to_wire())
    except Exception:
        log.exception("reply failed; returning safe wait")
        return JSONResponse(SAFE_REPLY)


@app.get("/v1/healthz")
async def healthz() -> JSONResponse:
    zero = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    try:
        counts, boot = await asyncio.wait_for(get_store().health(), timeout=3)
        uptime = int(time.time() - boot) if boot else 0
        return JSONResponse({"status": "ok", "uptime_seconds": max(uptime, 0), "contexts_loaded": {**zero, **counts}})
    except Exception:
        log.exception("healthz store failure")
        return JSONResponse({"status": "degraded", "uptime_seconds": 0, "contexts_loaded": zero})


@app.get("/v1/metadata")
async def metadata() -> JSONResponse:
    s = get_settings()
    return JSONResponse({
        "team_name": s.team_name, "team_members": s.team_members, "model": s.model_label,
        "approach": "deterministic decide (policy, dedup, playbooks) -> closed-fact-sheet LLM writer -> verifier -> template fallback",
        "contact_email": s.contact_email, "version": s.bot_version, "submitted_at": s.submitted_at,
    })


@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    try:
        await get_store().wipe()
        return JSONResponse({"ok": True})
    except Exception:
        log.exception("teardown failed")
        return JSONResponse({"ok": False})


@app.get("/v1/_debug")
async def debug(token: str = "") -> JSONResponse:
    s = get_settings()
    if not s.debug_token or token != s.debug_token:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    store = get_store()
    counts, boot = await store.health()
    from .llm.router import Router
    return JSONResponse({"counts": counts, "boot_ts": boot, "last_actions": await store.list_range("dbg:actions"),
                         "rate_limits": await Router(store, s).state()})
