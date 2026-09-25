"""Deterministic template composer. Built only from FactSheet facts; never calls an LLM."""
from __future__ import annotations

import hashlib
from typing import Optional

from .humanize import strip_end
from .models import FactSheet
from .playbooks import Playbook

N_VARIANTS = 3
_LOWER_LEADS = {"your", "you", "you're", "it's", "a", "an", "time", "following", "thank", "we", "here's", "one",
                "quick", "there", "the", "some"}


def variant_for(seed: str) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) % N_VARIANTS


def _lead(text: str) -> str:
    """Lower-case a leading common word so it reads naturally after a salutation."""
    first = text.split(" ", 1)[0]
    if first.lower().rstrip(",") in _LOWER_LEADS and first[0].isupper():
        return text[0].lower() + text[1:]
    return text


def _slot_ask(fs: FactSheet, pb: Playbook) -> Optional[str]:
    tpl = pb.slot_ask_hi if fs.language == "hi-en" else pb.slot_ask_en
    if not tpl or not fs.slots:
        return None
    if "{s2}" in tpl:
        if len(fs.slots) < 2:
            return None
        shorts = [s.split()[0].rstrip(",") for s in fs.slots[:2]]
        s1, s2 = (shorts if shorts[0] != shorts[1] else fs.slots[:2])
        return tpl.format(s1=s1, s2=s2)
    return tpl.format(s1=fs.slots[0])


def compose_fallback(fs: FactSheet, pb: Playbook, seed: str, variant: Optional[int] = None,
                     minimal: bool = False) -> dict:
    v = variant_for(seed) if variant is None else variant % N_VARIANTS
    hook_fact = fs.get("hook")
    hook = strip_end(hook_fact.text if hook_fact else "Here's a quick update")
    used = [hook_fact.id] if hook_fact else []

    supports: list[str] = []
    if not minimal:
        for key in pb.support_keys:
            f = fs.get(key)
            if f:
                supports.append(strip_end(f.text))
                used.append(f.id)
            if len(supports) >= pb.support_n:
                break

    slot = _slot_ask(fs, pb)
    ask = slot or (pb.ask_hi if fs.language == "hi-en" else pb.ask_en)
    cta = "multi_choice_slot" if slot else pb.cta_type

    def assemble(sup: list[str]) -> str:
        tail = " ".join(s + "." for s in sup)
        if fs.send_as == "merchant_on_behalf":
            who = f"{fs.merchant_name} here." if fs.merchant_name else ""
            head = f"Hi {fs.salutation}, {who}".strip()
            first = f"{head} {hook[0].upper() + hook[1:]}."
        elif v == 0:
            first = f"{fs.salutation}, {_lead(hook)}."
        elif v == 1:
            first = f"{fs.salutation} — {_lead(hook)}."
        else:
            first = f"Quick one, {fs.salutation}: {_lead(hook)}."
        return " ".join(p for p in (first, tail, ask) if p)

    body = assemble(supports)
    while len(body) > 600 and supports:
        supports.pop()
        body = assemble(supports)
    return {"body": body, "cta": cta, "facts_used": used, "rationale_note": ""}
