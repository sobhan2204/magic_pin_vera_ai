"""Prompt construction. Kept compact (~800-1100 tokens) because Groq's free tier is token-limited.

PROMPT_VERSION (config.py) is part of the draft-cache key: bump it whenever prompt text changes.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import CTA_TYPES, FactSheet
from .normalize import Trigger
from .playbooks import Playbook

MAX_FACTS = 9
MAX_TOKENS = 450

MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string"},
        "cta": {"type": "string", "enum": ["binary_yes_stop", "open_ended", "none", "multi_choice_slot"]},
        "facts_used": {"type": "array", "items": {"type": "string"}},
        "rationale_note": {"type": "string"},
    },
    "required": ["body", "cta", "facts_used", "rationale_note"],
    "additionalProperties": False,
}

WRITER_SYSTEM = """You write ONE WhatsApp message for Vera, magicpin's merchant assistant, and reply with JSON only.
CLOSED WORLD: use ONLY the numbered FACTS. Never add any number, price, date, name, place, product, offer, freebie, study or claim that is not in them. If unsure, leave it out. Copy numbers, prices and names exactly as written. Keep the exact meaning of each fact (\"gone quiet\" is not \"ignored you\"). Do not explain why numbers moved and do not predict effects or risks; state the facts, then offer the next step.
STRUCTURE: begin with the salutation, then the lead fact (why now) in the first sentence. At most one supporting fact. Exactly ONE ask, in the LAST sentence (at most one "?"). Usually 2-4 sentences, under 480 characters. Give the reader something to gain or lose. No preamble, no self-introduction, no hype, no ALL CAPS, no URLs. Prefer service+price wording over "% off".
EVERY sentence before the ask must restate one listed FACT in your own words. Add no advice, diagnosis, recommendation, cause or promise of your own; the only thing you may propose is the single ask. Say it in plain shop-owner language. Never mention internal words: trigger, signal, payload, context, field names, snake_case.
Output JSON: {"body": string, "cta": "binary_yes_stop"|"open_ended"|"none"|"multi_choice_slot", "facts_used": ["F1",...], "rationale_note": short string}"""

# Own-wording exemplars (illustrative facts, deliberately different from the dataset) -> shape, not text.
_EX_MERCHANT = {
    "dentists": ('F1 IJDR Jan 2027, p.9 reports a 900-patient trial of fluoride gel in children | F2 You have 60 paediatric patients on your roster',
                 "Dr. Rao, IJDR's Jan issue (p.9) has a 900-patient trial on fluoride gel in children. With 60 paediatric patients on your roster this one is worth a read. Want me to summarise it and draft a parent-friendly WhatsApp?"),
    "salons": ('F1 Bridal-trial searches near you are up 28% this week | F2 Your active offers are Haircut @ ₹99 and Hair Spa @ ₹499',
               "Meena, bridal-trial searches near you are up 28% this week. Your Hair Spa @ ₹499 is a natural add-on for brides. Want me to draft a Google post around it?"),
    "restaurants": ('F1 Tonight there is a weeknight match, 8pm kickoff | F2 Your active offer is Combo Meal @ ₹249',
                    "Sunil, there's a weeknight match tonight (8pm). Match evenings favour a quick-order combo, and your Combo Meal @ ₹249 fits. Want me to draft a delivery-app banner for it?"),
    "gyms": ('F1 Your views are down 20% over the last 7 days, which is an expected seasonal dip | F2 You have 210 active members',
             "Asha, your views are down 20% this week, but that's the usual seasonal dip, not a profile problem. Best use of this window is keeping your 210 members active. Want me to draft a short attendance challenge?"),
    "pharmacies": ('F1 A recall notice covers metformin batch MF7-31 | F2 Pull the batch; message affected customers from your repeat list',
                   "Ravi, a recall notice covers metformin batch MF7-31. Please pull it from the shelf and let repeat customers know. Want me to draft that customer message?"),
}
_EX_CUSTOMER = ('F1 It has been 3 months since your last visit, so your check-up is due | F2 Open slots: Mon 4 Aug, 5pm or Tue 5 Aug, 6pm',
                "Hi Nisha, Bright Smile Clinic here. It's been 3 months since your last visit, so your check-up is due. We have Mon 4 Aug, 5pm or Tue 5 Aug, 6pm open. Reply 1 for Mon, 2 for Tue, or suggest a time that suits you.")

_FACT_ORDER_EXTRA = ("t.prev_hook",)


def select_facts(fs: FactSheet, pb: Playbook) -> list:
    chosen, seen = [], set()

    texts: set[str] = set()

    def take(f) -> None:
        if f and f.id not in seen and f.text not in texts and len(chosen) < MAX_FACTS:
            seen.add(f.id)
            texts.add(f.text)
            chosen.append(f)

    take(fs.get("hook"))
    for k in (*_FACT_ORDER_EXTRA, "t.kind", *pb.support_keys):
        take(fs.get(k))
    for f in fs.facts:
        take(f)
    return chosen


def _voice_line(fs: FactSheet) -> str:
    v = fs.voice or {}
    taboo = [t for t in (v.get("vocab_taboo") or v.get("taboos") or []) if isinstance(t, str) and len(t) < 40][:8]
    allowed = [a for a in (v.get("vocab_allowed") or []) if isinstance(a, str)][:8]
    parts = [f"tone {v.get('tone', 'peer')}", f"register {v.get('register', 'respectful')}"]
    if allowed:
        parts.append("vocabulary you may use: " + ", ".join(allowed))
    line = "Voice: " + "; ".join(parts) + "."
    if taboo:
        line += " NEVER use: " + ", ".join(taboo) + "."
    return line


def ask_example(fs: FactSheet, pb: Playbook) -> str:
    from .fallback import _slot_ask
    slot = _slot_ask(fs, pb)
    hook = fs.get("hook")
    if not slot and (fs.get("t.kind") or (hook and hook.source == "trigger.kind")) and pb.stmt_ask_en:
        return pb.stmt_ask_hi if fs.language == "hi-en" and pb.stmt_ask_hi else pb.stmt_ask_en
    return slot or (pb.ask_hi if fs.language == "hi-en" else pb.ask_en)


def _block_lines(fs: FactSheet, pb: Playbook) -> list[str]:
    """Everything the writer needs about ONE message (shared by the single and the batch prompt)."""
    customer = fs.send_as == "merchant_on_behalf"
    facts = select_facts(fs, pb)
    lines = [_voice_line(fs)]
    if customer:
        lines.append(f'Audience: a customer of {fs.merchant_name}. Write as the business team ("we"), warm and respectful. '
                     f'Open exactly with: "Hi {fs.salutation}, {fs.merchant_name} here."')
    else:
        lines.append(f'Audience: the merchant. Salutation (start the message with it): "{fs.salutation}".')
    if fs.language == "hi-en":
        lines.append("Language: natural Hindi-English code-mix in Roman script (use words like aap, hai, kya, main, kar, ke liye, "
                     "chahenge); keep numbers, names and prices exactly as in the facts.")
    else:
        lines.append("Language: plain English.")
    lines.append(f"Objective: {pb.objective}. Levers to use: {', '.join(pb.levers) or 'specificity'}.")
    lines.append(f"CTA: {('multi_choice_slot' if fs.slots and pb.slot_ask_en else pb.cta_type)}. "
                 f"Example ask (rephrase naturally, keep it one ask): \"{ask_example(fs, pb)}\"")
    lines.append("FACTS (lead with the first one):")
    for i, f in enumerate(facts):
        lines.append(f"{f.id}{' (lead)' if i == 0 else ''}: {f.text}")
    return lines


def _example_lines(fs: FactSheet) -> list[str]:
    customer = fs.send_as == "merchant_on_behalf"
    ex_facts, ex_body = _EX_CUSTOMER if customer else _EX_MERCHANT.get(fs.category_slug, _EX_MERCHANT["salons"])
    return [f"STYLE EXAMPLE (different business; imitate the shape, never its facts):\nFACTS: {ex_facts}\nMESSAGE: {ex_body}"]


def build_writer_messages(fs: FactSheet, pb: Playbook, t: Trigger) -> list[dict]:
    lines = _block_lines(fs, pb) + _example_lines(fs)
    return [{"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


BATCH_SCHEMA = {
    "type": "object",
    "properties": {"messages": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "body": {"type": "string"},
                       "cta": {"type": "string", "enum": ["binary_yes_stop", "open_ended", "none", "multi_choice_slot"]},
                       "facts_used": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "body", "cta", "facts_used"], "additionalProperties": False}}},
    "required": ["messages"], "additionalProperties": False,
}

BATCH_SYSTEM = WRITER_SYSTEM.split("Output JSON:")[0].replace("ONE WhatsApp message", "several WhatsApp messages") + (
    "You get several MESSAGE blocks (A, B, C...). Write exactly one message per block using ONLY that block's own facts; "
    "never mix facts, names or numbers between blocks. Each block has its own salutation, language and ask.\n"
    'Output JSON: {"messages": [{"id": "A", "body": string, "cta": "binary_yes_stop"|"open_ended"|"none"|"multi_choice_slot", '
    '"facts_used": ["F1",...]}, ...]}')

BATCH_IDS = "ABCD"


def build_batch_messages(items: list[tuple[FactSheet, Playbook]]) -> list[dict]:
    lines: list[str] = []
    for i, (fs, pb) in enumerate(items):
        lines.append(f"=== MESSAGE {BATCH_IDS[i]} ===")
        lines += _block_lines(fs, pb)
    lines += _example_lines(items[0][0])
    return [{"role": "system", "content": BATCH_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_batch(text: str) -> dict[str, dict]:
    """id -> normalized output. Raises ValueError when nothing usable came back."""
    t = _FENCE.sub("", (text or "").strip())
    if not t.startswith("{"):
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("no JSON object in completion")
        t = m.group(0)
    obj = json.loads(t)
    msgs = obj.get("messages") if isinstance(obj, dict) else None
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("no messages array")
    out: dict[str, dict] = {}
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("body"), str) and m["body"].strip():
            out[m["id"].strip().upper()] = parse_output(json.dumps({**m, "rationale_note": ""}))
    if not out:
        raise ValueError("no usable messages")
    return out


def build_repair_messages(messages: list[dict], previous: dict, violations: list[str]) -> list[dict]:
    fix = "Fix ONLY these problems and keep everything else the same. Still use only the FACTS. Return the same JSON.\n- " + \
          "\n- ".join(violations[:8])
    if any("duplicate" in v for v in violations):
        fix += "\n- Say it differently and lead with a different fact than before."
    return messages + [{"role": "assistant", "content": json.dumps(previous, ensure_ascii=False)},
                       {"role": "user", "content": fix}]


REPLY_SYSTEM = """You are Vera, magicpin's merchant assistant, replying on WhatsApp to the person's latest message. Reply with JSON only.
CLOSED WORLD: use ONLY the numbered FACTS (and words the person just wrote). Never invent numbers, prices, dates, names, offers or promises. If the facts do not answer the question, say you will check and offer the one next step.
Style: 1-3 short sentences, plain language, no preamble, no URLs, no internal words (trigger, signal, payload, context). End with at most one ask (one "?" at most), or none.
Output JSON: {"body": string, "cta": "binary_yes_stop"|"open_ended"|"none"|"multi_choice_slot", "facts_used": ["F1",...], "rationale_note": short string}"""


def build_reply_messages(fs: FactSheet, turns: list[dict], message: str, deliverable: str, customer: bool) -> list[dict]:
    lines = [_voice_line(fs)]
    lines.append("Speak as the business team (we) to a customer." if customer else f'Address the merchant as "{fs.salutation}" only if natural.')
    lines.append("Language: Hindi-English code-mix in Roman script." if fs.language == "hi-en" else "Language: plain English.")
    lines.append(f"Pending offer you can move forward: {deliverable}.")
    lines.append("FACTS:")
    lines += [f"{f.id}: {f.text}" for f in fs.facts[:MAX_FACTS]]
    lines.append("CONVERSATION (latest last):")
    for t in turns[-4:]:
        who = "Vera" if t.get("from") == "bot" else "Them"
        lines.append(f"{who}: {str(t.get('body', ''))[:240]}")
    lines.append(f"Them: {message[:400]}")
    return [{"role": "system", "content": REPLY_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_output(text: str) -> dict:
    """Parse the model's JSON. Raises ValueError on anything unusable (router treats that as a failed call)."""
    t = _FENCE.sub("", (text or "").strip())
    if not t.startswith("{"):
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("no JSON object in completion")
        t = m.group(0)
    obj: Any = json.loads(t)
    if not isinstance(obj, dict) or not isinstance(obj.get("body"), str) or not obj["body"].strip():
        raise ValueError("completion has no body")
    obj["body"] = re.sub(r"[ \t]+", " ", obj["body"]).strip()
    if obj.get("cta") not in CTA_TYPES:
        obj["cta"] = "open_ended"
    fu = obj.get("facts_used")
    obj["facts_used"] = [x for x in fu if isinstance(x, str)] if isinstance(fu, list) else []
    obj["rationale_note"] = str(obj.get("rationale_note") or "")[:200]
    return obj
