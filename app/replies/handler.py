"""/v1/reply orchestration (Phase 1: deterministic rules only; LLM question path arrives in Phase 4)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from ..config import Settings
from ..humanize import parse_dt
from ..models import ReplyOut
from ..store.base import Store
from .classifier import classify, wait_seconds
from .state import ckey, load_state, message_hash, mkey, save_state

log = logging.getLogger("vera.reply")
CONV_TTL_S = 3 * 24 * 3600

# {ill} -> "I'll"/"we'll" ; {d} -> deliverable
_T = {
    "hostile": {
        "en": ["Sorry about that. I'll keep it short: if it helps, {ill} prepare {d}, otherwise {i_will} stay quiet until you message me.",
               "Sorry, I hear you. {Ill} only reach out when there's something specific. Just say the word and {ill} prepare {d}."],
        "hi": ["Sorry, aapko disturb kiya. Chahein toh main {d} taiyaar kar doon, warna aap khud message karein tab tak hum chup rahenge."],
    },
    "auto": {
        "en": ["Looks like an automated reply, no problem. When the owner sees this, just reply YES and {ill} prepare {d}.",
               "That looks like an auto-reply. Whenever the owner is back, a quick YES here and {ill} prepare {d}."],
        "hi": ["Lagta hai ye ek auto-reply hai. Jab owner dekhein, bas Reply YES kar dijiye aur main {d} taiyaar kar doon."],
    },
    "commit": {
        "en": ["Done, moving ahead. Here's the next step: {ill} prepare {d} and send it here for your approval. Reply CONFIRM and {ill} get it moving.",
               "Sending it now. Next up: {d}, shared here shortly for your approval. Reply CONFIRM to lock it in."],
        "hi": ["Theek hai, aage badhte hain. Next step: main {d} taiyaar karke yahin bhej doon. Aap Reply CONFIRM kar dijiye."],
    },
    "off": {
        "en": ["That one is outside what I can help with, so it's best left to the right specialist. Back to where we were: reply YES and {ill} prepare {d}.",
               "I'll leave that to the right expert. Coming back to our plan: reply YES and {ill} prepare {d}."],
        "hi": ["Ye mere kaam ke bahar hai, iske liye sahi expert se baat kijiye. Hum jahan the wahin wapas: Reply YES kar dijiye aur main {d} taiyaar kar doon."],
    },
    "ask": {
        "en": ["Happy to help. Where we are: {ctx}. Reply YES and {ill} prepare {d}, or tell me what you'd like changed.",
               "Sure. Quick recap: {ctx}. Reply YES and {ill} prepare {d}."],
        "hi": ["Zaroor. Abhi hum yahan hain: {ctx}. Aap Reply YES kar dijiye aur main {d} taiyaar kar doon."],
    },
    "close": {
        "en": ["Thanks for your time. I'll leave it here for now, and you can message me whenever you want to pick this up."],
        "hi": ["Aapke time ke liye shukriya. Abhi yahin rokte hain, jab bhi aage badhna ho aap message kar dijiye."],
    },
}
_SLOT_RX = re.compile(r"^\s*([12])\b|\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", re.I)


def _render(kind: str, lang: str, customer: bool, d: str, used: list[str], ctx: str = "") -> Optional[str]:
    opts = _T[kind]["hi" if lang == "hi-en" else "en"] + (_T[kind]["en"] if lang == "hi-en" else [])
    ill = "we'll" if customer else "I'll"
    for tpl in opts:
        body = tpl.format(ill=ill, Ill=ill.capitalize(), i_will="we will" if customer else "I will", d=d, ctx=ctx)
        if body not in used:
            return body
    return None


async def handle_reply(store: Store, settings: Settings, req: dict) -> ReplyOut:
    conv_id = str(req.get("conversation_id") or "conv_unknown")
    merchant_id = req.get("merchant_id") or None
    customer_id = req.get("customer_id") or None
    role = req.get("from_role") or "merchant"
    message = req.get("message") if isinstance(req.get("message"), str) else ""
    try:
        turn = int(req.get("turn_number") or 1)
    except (TypeError, ValueError):
        turn = 1
    customer = role == "customer" and bool(customer_id)
    skey = ckey(customer_id) if customer else mkey(merchant_id, conv_id)

    state = await load_state(store, skey)
    conv = await store.get_json(f"conv:{conv_id}") or {}
    if conv.get("customer_id") and not customer and role == "customer":
        customer = True
    lang = conv.get("language")
    if not lang and merchant_id and not customer:
        m = await store.get_context("merchant", merchant_id)
        langs = [str(x).lower() for x in ((m[1].get("identity") or {}).get("languages") or [])] if m else []
        lang = "hi-en" if "hi" in langs else "en"
    lang = lang or "en"

    pending = state.get("pending") or {}
    d = pending.get("deliverable") or "the draft"
    ctx = pending.get("hook") or "your profile update"
    used = [t["body"] for t in conv.get("turns", []) if t.get("from") == "bot"]
    used += await store.list_range(f"sent:bodies:{merchant_id}") if merchant_id else []

    h = message_hash(message)
    seen_before = h in state["seen_hashes"]
    cls = classify(message, seen_before)
    state["unanswered"] = 0
    state["seen_hashes"] = state["seen_hashes"] + [h]

    if state.get("opted_out") and cls != "opt_out":
        out = ReplyOut(action="end", rationale="Merchant opted out earlier; staying silent.")
        await _persist(store, skey, state, conv_id, conv, message, role, None)
        return out

    out: ReplyOut
    if cls == "opt_out":
        state["opted_out"] = True
        out = ReplyOut(action="end", rationale="Merchant opted out; exiting gracefully and suppressing future sends.")
    elif cls == "hostile":
        state["hostile_count"] += 1
        body = _render("hostile", lang, customer, d, used) if state["hostile_count"] < 2 else None
        out = (ReplyOut(action="send", body=body, cta="none",
                        rationale="Merchant is frustrated; one short non-defensive acknowledgement, conversation kept open.")
               if body else ReplyOut(action="end", rationale="Hostility repeated; exiting to respect the merchant."))
    elif cls == "auto_reply":
        state["auto_count"] += 1
        body = _render("auto", lang, customer, d, used) if state["auto_count"] < 2 else None
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop",
                        rationale="Canned auto-reply detected; one explicit prompt aimed at the owner.")
               if body else ReplyOut(action="end",
                                     rationale="Auto-reply detected repeatedly; exiting to avoid wasting turns."))
    elif customer and conv.get("slots") and _SLOT_RX.search(message) and cls in ("commitment", "other", "question"):
        slot = _pick_slot(conv["slots"], message)
        out = ReplyOut(action="send", cta="none", body=f"Booked: {slot}. Done, see you then! Reply CHANGE if you need another time.",
                       rationale=f"Customer chose the {slot} slot; confirming the exact slot from the offer.")
    elif cls == "commitment":
        body = _render("commit", lang, customer, d, used)
        state["pending"] = {**pending, "stage": "confirm"}
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop",
                        rationale="Merchant committed; switching from pitch to action and delivering the next step.")
               if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Already sent this confirmation; giving it time."))
    elif cls == "later":
        out = ReplyOut(action="wait", wait_seconds=wait_seconds(message),
                       rationale="Merchant asked for time; backing off and not chasing.")
    elif turn >= 6:
        out = ReplyOut(action="end", rationale="Conversation has run its course; closing.")
    elif turn == 5:
        body = _render("close", lang, customer, d, used)
        out = (ReplyOut(action="send", body=body, cta="none", rationale="Turn cap reached; short closing line.")
               if body else ReplyOut(action="end", rationale="Turn cap reached; closing."))
    elif cls == "off_topic":
        body = _render("off", lang, customer, d, used)
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop",
                        rationale="Out-of-scope ask politely declined; redirected to the pending item with one ask.")
               if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Nothing new to add; waiting."))
    else:
        body = _render("ask", lang, customer, d, used, ctx=ctx.rstrip(".")) if pending else None
        if body is None and not pending:
            body = ("Happy to help. Tell me what you'd like to sort first on your profile: offers, posts or customer messages."
                    if lang != "hi-en" else
                    "Zaroor. Bataiye aap sabse pehle kya theek karwana chahte hain: offers, posts ya customer messages.")
            if body in used:
                body = None
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop" if pending else "open_ended",
                        rationale="Answered from the pending item and pointed to the single next step.")
               if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Already replied with this; waiting for the merchant."))

    await _persist(store, skey, state, conv_id, conv, message, role, out)
    return out


def _pick_slot(slots: list[str], message: str) -> str:
    m = re.match(r"^\s*([12])\b", message)
    if m and int(m.group(1)) <= len(slots):
        return slots[int(m.group(1)) - 1]
    low = message.lower()
    for s in slots:
        if s.split()[0].lower().rstrip(",")[:3] in low:
            return s
    return slots[0]


async def _persist(store: Store, skey: str, state: dict, conv_id: str, conv: dict, message: str, role: str,
                   out: Optional[ReplyOut]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conv.setdefault("turns", []).append({"from": role, "body": message, "ts": now})
    if out is not None and out.action == "send" and out.body:
        conv["turns"].append({"from": "bot", "body": out.body, "ts": now})
    if out is not None and out.action == "end":
        state["closed_convs"] = state.get("closed_convs", []) + [conv_id]
    conv["turns"] = conv["turns"][-20:]
    await save_state(store, skey, state)
    await store.set_json(f"conv:{conv_id}", conv, CONV_TTL_S)

