"""Intent classification with strict precedence (first match wins):
opt_out > hostile > auto_reply > commitment > later > off_topic > question > other."""
from __future__ import annotations

import re

OPT_OUT = [
    r"\bstop\b", r"\bunsubscribe\b", r"\bdon'?t (message|text|contact|send|call)\b", r"\bdo not (message|text|contact|send|call)\b",
    r"\bnot interested\b", r"\bband karo\b", r"\bmat bhejo\b", r"\bremove me\b", r"\bleave me alone\b",
    r"\bopt[- ]?out\b", r"\bnahi chahiye\b", r"\bmessage mat\b",
]
HOSTILE = [
    r"\buseless\b", r"\bscam\b", r"\bwaste\b", r"\bstupid\b", r"\bmoron", r"\bidiots?\b", r"\bbakwas\b", r"\bnonsense\b", r"\bshut up\b",
    r"\bfraud\b", r"\bbothering\b", r"\bannoying\b", r"\bdisgusting\b", r"\bpathetic\b", r"\bspam\b", r"\bfuck", r"\bshit\b",
    r"\bbloody\b", r"\bharass", r"\brubbish\b", r"\bget lost\b", r"\birritating\b", r"\bworst\b", r"\bfaltu\b", r"\bchup\b",
]
AUTO_REPLY = [
    r"thank(s| you) for (contacting|reaching out|your message|messaging)", r"we(\s+will|'ll| shall) get back",
    r"our team will (respond|get back|contact|reach|revert)", r"will (respond|reply|revert) (shortly|soon)",
    r"automated (assistant|message|reply|response)", r"this is an automated", r"currently unavailable",
    r"business hours", r"aapki jaankari ke liye", r"hamari team", r"auto[- ]?reply", r"out of office",
    r"\bi am away\b", r"abhi available nahi", r"jald hi (sampark|reply|jawab)",
]
COMMITMENT = [
    r"\byes\b", r"\bhaan\b", r"\bha\b", r"lets? do it", r"let'?s do it", r"\bgo ahead\b", r"\bdo it\b", r"\bsure\b",
    r"\bdone\b", r"\bchalega\b", r"theek hai", r"thik hai", r"\bkar do\b", r"\bkaro\b", r"\bproceed\b", r"\bconfirm\b",
    r"send it", r"i want to join", r"judna hai", r"sounds good", r"\bplease do\b",
]
LATER = [
    r"\blater\b", r"\bbusy\b", r"baad mein", r"baad me\b", r"\bkal\b", r"\btomorrow\b", r"not now", r"next week",
    r"abhi nahi", r"thodi der", r"in a meeting", r"\bdriving\b",
]
LONG_WAIT = [r"\btomorrow\b", r"\bkal\b", r"next week"]
OFF_TOPIC = [
    r"\bgst\b", r"\btax\b", r"\bitr\b", r"\bloans?\b", r"\binsurance\b", r"\bvisa\b", r"\bpassport\b", r"\bsalary\b",
    r"\bpolitic", r"\bweather\b", r"\bjokes?\b", r"\brecipe\b", r"\bstock market\b", r"\bcrypto\b", r"\bbitcoin\b",
    r"\baadhaa?r\b", r"\bpan card\b", r"\blawyer\b", r"\blegal\b", r"\bcricket score\b",
]
QUESTION_START = re.compile(r"^(what|how|why|when|where|which|who|can|could|do|does|is|are|kya|kaise|kyun|kab|kitna)\b")


def _hit(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    return " ".join(t.split())


def classify(message: str, seen_before: bool = False) -> str:
    t = norm(message)
    if not t:
        return "other"
    if _hit(OPT_OUT, t):
        return "opt_out"
    if _hit(HOSTILE, t):
        return "hostile"
    if _hit(AUTO_REPLY, t) or (seen_before and len(t) >= 20):
        return "auto_reply"
    if _hit(COMMITMENT, t) and len(t.split()) <= 12:
        return "commitment"
    if _hit(LATER, t):
        return "later"
    if _hit(OFF_TOPIC, t):
        return "off_topic"
    if "?" in t or QUESTION_START.match(t):
        return "question"
    return "other"


def wait_seconds(message: str) -> int:
    return 86400 if _hit(LONG_WAIT, norm(message)) else 3600
