"""/v1/reply orchestration: rules-first, merchant/customer-level state, strict intent precedence.

The LLM question path is added in Phase 4; until then questions get a deterministic, grounded recap.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from ..config import Settings
from ..models import ReplyOut
from ..store.base import Store
from .classifier import classify, wait_seconds
from .lint import SAFE_ACTION_BODY, lint_action_body
from .llm_reply import try_llm_reply
from .state import ckey, load_state, message_hash, mkey, save_state

log = logging.getLogger("vera.reply")
CONV_TTL_S = 3 * 24 * 3600

# placeholders: {ill} I'll/we'll, {Ill}, {i_will}, {d} deliverable (noun phrase), {ctx} pending hook text, {slots} slot menu
_T = {
    "hostile": {
        "en": ["Sorry about that. I'll keep it short: if it helps, {ill} prepare {d}, otherwise {i_will} stay quiet until you message me.",
               "Sorry, I hear you. {Ill} only reach out when there's something specific. Just say the word and {ill} prepare {d}."],
        "hi": ["Sorry, aapko disturb kiya. Chahein toh {me} {d} taiyaar kar {do}, warna aap khud message karein tab tak hum chup rahenge."],
    },
    "auto": {
        "en": ["Looks like an automated reply, no problem. When the owner sees this, just reply YES and {ill} prepare {d}.",
               "That looks like an auto-reply. Whenever the owner is back, a quick YES here and {ill} prepare {d}."],
        "hi": ["Lagta hai ye ek auto-reply hai. Jab owner dekhein, bas Reply YES kar dijiye aur {me} {d} taiyaar kar {do}."],
    },
    "commit": {
        "en": ["Done, moving ahead. Here's the next step: {ill} prepare {d} and send it here for your approval. Reply CONFIRM and {ill} get it moving.",
               "Sending it now. Next up: {d}, shared here shortly for your approval. Reply CONFIRM to lock it in."],
        "hi": ["Theek hai, aage badhte hain. Next step: {me} {d} taiyaar karke yahin bhej {do}. Aap Reply CONFIRM kar dijiye."],
    },
    "confirmed": {
        "en": ["Confirmed, it's done. {Ill} share the result here as soon as it's live, and you can message me anytime for the next step."],
        "hi": ["Confirmed, ho gaya. Live hote hi {me} result yahin share {will}, aur agle step ke liye aap kabhi bhi message kar sakte hain."],
    },
    "off": {
        "en": ["That one is outside what I can help with, so it's best left to the right specialist. Back to where we were: reply YES and {ill} prepare {d}.",
               "I'll leave that to the right expert. Coming back to our plan: reply YES and {ill} prepare {d}."],
        "hi": ["Ye mere kaam ke bahar hai, iske liye sahi expert se baat kijiye. Hum jahan the wahin wapas: Reply YES kar dijiye aur {me} {d} taiyaar kar {do}."],
    },
    "off_none": {
        "en": ["That one is outside what I can help with. I'm best at your Google profile, offers and customer messages, so tell me which of those to start with."],
        "hi": ["Ye mere kaam ke bahar hai. Hum aapke Google profile, offers aur customer messages mein madad kar sakte hain, bataiye kahan se shuru karein."],
    },
    "ask": {
        "en": ["Happy to help. Where we are: {ctx}. Reply YES and {ill} prepare {d}, or tell me what you'd like changed.",
               "Sure. Quick recap: {ctx}. Reply YES and {ill} prepare {d}."],
        "hi": ["Zaroor. Abhi hum yahan hain: {ctx}. Aap Reply YES kar dijiye aur {me} {d} taiyaar kar {do}."],
    },
    "ask_none": {
        "en": ["Happy to help. Tell me what you'd like to sort first on your profile: offers, posts or customer messages."],
        "hi": ["Zaroor. Bataiye aap sabse pehle kya theek karwana chahte hain: offers, posts ya customer messages."],
    },
    "pick_slot": {
        "en": ["Great! Reply {slots} and we'll lock it in."],
        "hi": ["Badhiya! Aap Reply {slots} kar dijiye, hum book kar denge."],
    },
    "close": {
        "en": ["Thanks for your time. I'll leave it here for now, and you can message me whenever you want to pick this up."],
        "hi": ["Aapke time ke liye shukriya. Abhi yahin rokte hain, jab bhi aage badhna ho aap message kar dijiye."],
    },
}
_HINDI_MARKERS = {"aap", "aapke", "hai", "hain", "kar", "karo", "nahi", "haan", "ji", "kya", "bhej", "chahiye", "mujhe",
                  "hum", "baad", "mein", "kal", "abhi", "theek", "chalega", "bata", "dijiye", "karna", "liye", "ka", "ki",
                  "ke", "judna", "bhejo", "band", "mat", "kaise", "karein", "chahte"}


def _render(kind: str, lang: str, customer: bool, d: str, used: list[str], ctx: str = "", slots: str = "") -> Optional[str]:
    opts = _T[kind]["hi" if lang == "hi-en" else "en"] + (_T[kind]["en"] if lang == "hi-en" else [])
    ill = "we'll" if customer else "I'll"
    me, do, will = ("hum", "denge", "karenge") if customer else ("main", "doon", "karunga")
    if lang == "hi-en" and d.startswith("the "):
        d = d[4:]                                     # "the draft" reads oddly inside a Hindi sentence
    for tpl in opts:
        body = tpl.format(ill=ill, Ill=ill.capitalize(), i_will="we will" if customer else "I will", d=d, ctx=ctx, slots=slots,
                          me=me, do=do, will=will)
        if body not in used:
            return body
    return None


async def _none(value=None):
    return value


def _speaks_hindi(message: str) -> bool:
    return len({t for t in re.findall(r"[a-z]+", message.lower()) if t in _HINDI_MARKERS}) >= 2


def _pick_slot(slots: list[str], message: str) -> Optional[str]:
    m = re.match(r"^\s*([12])\b", message)
    if m:
        i = int(m.group(1))
        return slots[i - 1] if i <= len(slots) else None
    low = message.lower()
    for s in slots:
        if s.split()[0].lower().rstrip(",")[:3] in low:
            return s
    return None


def _slot_menu(slots: list[str]) -> str:
    if len(slots) >= 2:
        shorts = [s.split()[0].rstrip(",") for s in slots[:2]]
        return f"1 for {shorts[0]}, 2 for {shorts[1]}" if shorts[0] != shorts[1] else f"1 for {slots[0]}, 2 for {slots[1]}"
    return "1"


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

    customer = bool(customer_id) and role == "customer"
    skey = ckey(customer_id) if customer else mkey(merchant_id, conv_id)
    # independent reads go out together (one network round-trip of latency instead of four)
    conv_raw, state, merchant_row, sent_bodies = await asyncio.gather(
        store.get_json(f"conv:{conv_id}"), load_state(store, skey),
        store.get_context("merchant", merchant_id) if (merchant_id and not customer) else _none(),
        store.list_range(f"sent:bodies:{merchant_id}") if merchant_id else _none([]))
    conv = conv_raw or {}

    lang = conv.get("language")
    if not lang and merchant_row:
        langs = [str(x).lower() for x in ((merchant_row[1].get("identity") or {}).get("languages") or [])]
        lang = "hi-en" if "hi" in langs else "en"
    lang = "hi-en" if _speaks_hindi(message) else (lang or "en")          # per-turn language switch

    pending = state.get("pending") or {}
    d = pending.get("deliverable") or "the draft"
    ctx = (pending.get("hook") or "").rstrip(".")
    used = [t["body"] for t in conv.get("turns", []) if t.get("from") == "bot"] + list(sent_bodies or [])

    h = message_hash(message)
    cls = classify(message, seen_before=h in state["seen_hashes"])
    state["unanswered"] = 0
    state["seen_hashes"] = state["seen_hashes"] + [h]
    if cls not in ("auto_reply", "hostile", "opt_out"):
        state["auto_count"] = 0                                             # a genuine reply resets the auto-reply streak

    slots = conv.get("slots") or []
    pick = _pick_slot(slots, message) if (customer and slots) else None
    out: ReplyOut
    if state.get("opted_out") and cls != "opt_out":
        out = ReplyOut(action="end", rationale="Recipient opted out earlier; staying silent.")
    elif cls == "opt_out":
        state["opted_out"] = True
        out = ReplyOut(action="end", rationale="Recipient opted out; exiting gracefully and suppressing future sends.")
    elif cls == "hostile":
        state["hostile_count"] += 1
        body = _render("hostile", lang, customer, d, used) if state["hostile_count"] < 2 else None
        out = (ReplyOut(action="send", body=body, cta="none",
                        rationale="Frustration detected; one short non-defensive acknowledgement, conversation kept open.")
               if body else ReplyOut(action="end", rationale="Hostility repeated; exiting to respect the recipient."))
    elif cls == "auto_reply":
        state["auto_count"] += 1
        body = _render("auto", lang, customer, d, used) if state["auto_count"] < 2 else None
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop",
                        rationale="Canned auto-reply detected; one explicit prompt aimed at the owner.")
               if body else ReplyOut(action="end", rationale="Auto-reply detected repeatedly; exiting to avoid wasting turns."))
    elif pick and cls in ("commitment", "other", "question"):
        state["pending"] = None
        out = ReplyOut(action="send", cta="none", body=f"Booked: {pick}. Done, see you then! Reply CHANGE if you need another time.",
                       rationale=f"Customer chose the {pick} slot; confirming the exact slot from the offer.")
    elif customer and slots and cls == "commitment":
        body = _render("pick_slot", lang, True, d, used, slots=_slot_menu(slots))
        out = (ReplyOut(action="send", body=body, cta="multi_choice_slot",
                        rationale="Customer said yes without choosing; asking which of the offered slots.")
               if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Slot menu already sent; waiting for the choice."))
    elif cls == "commitment":
        stage = pending.get("stage")
        if stage == "done":
            out = ReplyOut(action="wait", wait_seconds=1800, rationale="Already confirmed and done; nothing new to send.")
        else:
            kind = "confirmed" if stage == "confirm" else "commit"
            body = _render(kind, lang, customer, d, used)
            if body and lint_action_body(body):
                log.warning("action body failed lint: %s", lint_action_body(body))
                body = SAFE_ACTION_BODY
            state["pending"] = {**pending, "stage": "done" if kind == "confirmed" else "confirm"}
            out = (ReplyOut(action="send", body=body, cta="none" if kind == "confirmed" else "binary_yes_stop",
                            rationale="Recipient committed; switching from pitch to action and delivering the next step.")
                   if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Already sent this confirmation; giving it time."))
    elif cls == "later":
        out = ReplyOut(action="wait", wait_seconds=wait_seconds(message),
                       rationale="Recipient asked for time; backing off and not chasing.")
    elif turn >= 6 or (turn == 5 and not pending):
        out = ReplyOut(action="end", rationale="Conversation has run its course; closing.")
    elif turn == 5:
        body = _render("close", lang, customer, d, used)
        out = (ReplyOut(action="send", body=body, cta="none", rationale="Turn cap reached; short closing line.")
               if body else ReplyOut(action="end", rationale="Turn cap reached; closing."))
    elif cls == "off_topic":
        body = _render("off" if pending else "off_none", lang, customer, d, used)
        out = (ReplyOut(action="send", body=body, cta="binary_yes_stop" if pending else "open_ended",
                        rationale="Out-of-scope ask politely declined; redirected to the pending item with one ask.")
               if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Nothing new to add; waiting."))
    else:
        llm = None
        if cls in ("question", "other") and settings.llm_mode == "live":
            llm = await try_llm_reply(store, settings, conv=conv, merchant_id=merchant_id, customer_id=customer_id,
                                      message=message, lang=lang, used=used, deliverable=d, customer=customer)
        if llm:
            out = ReplyOut(action="send", body=llm["body"], cta=llm.get("cta") or "open_ended",
                           rationale="Answered the question from the known facts and pointed to the next step.")
        else:
            body = _render("ask" if pending else "ask_none", lang, customer, d, used, ctx=ctx or "your profile update")
            out = (ReplyOut(action="send", body=body, cta="binary_yes_stop" if pending else "open_ended",
                            rationale="Answered from the pending item and pointed to the single next step.")
                   if body else ReplyOut(action="wait", wait_seconds=1800, rationale="Already replied with this; waiting."))

    await _persist(store, skey, state, conv_id, conv, message, role, out, merchant_id, customer_id)
    return out


async def _persist(store: Store, skey: str, state: dict, conv_id: str, conv: dict, message: str, role: str,
                   out: ReplyOut, merchant_id: Optional[str], customer_id: Optional[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conv.setdefault("turns", []).append({"from": role, "body": message, "ts": now})
    if out.action == "send" and out.body:
        conv["turns"].append({"from": "bot", "body": out.body, "ts": now})
    if out.action == "end":
        state["closed_convs"] = state.get("closed_convs", []) + [conv_id]
    conv.setdefault("merchant_id", merchant_id)
    conv.setdefault("customer_id", customer_id)
    conv["turns"] = conv["turns"][-20:]
    await asyncio.gather(save_state(store, skey, state), store.set_json(f"conv:{conv_id}", conv, CONV_TTL_S))
