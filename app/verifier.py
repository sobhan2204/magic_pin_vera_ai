"""Grounding + rule checks. Returns a list of violation strings (empty list == pass)."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .dedup import too_similar
from .models import CTA_TYPES, FactSheet

_NUM = re.compile(r"(?<![A-Za-z0-9_])\d[\d,]*(?:\.\d+)?")
# Tiny explicit allowlist of numbers that need no grounding
_ALLOW = [
    re.compile(r"\breply\s+\d\b", re.I),                       # "Reply 1"
    re.compile(r"\b\d\s+for\s+[A-Z][a-z]{2}\b"),               # "2 for Thu"
    re.compile(r"\b(?:2|3|5|10)[- ]?(?:min|mins|minute|minutes)\b", re.I),   # effort phrases
]
_WORD = re.compile(r"[A-Za-z][A-Za-z]+")
_COMMON = {
    "vera", "reply", "yes", "stop", "confirm", "cancel", "google", "whatsapp", "hi", "hello", "dr", "ok", "okay",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
    "december", "gbp", "sms", "pdf", "am", "pm", "i", "id", "ji", "aap", "kya", "haan", "namaste", "instagram",
    "insta", "swiggy", "zomato", "ist", "cde", "qr", "url",
}
_HINDI = {
    "aap", "aapke", "aapka", "aapki", "hai", "hain", "kar", "karo", "karna", "ke", "ka", "ki", "liye", "abhi",
    "chahiye", "chahein", "chahenge", "ho", "main", "kya", "ya", "toh", "bata", "dijiye", "bhej", "hoon", "ji",
    "haan", "nahi", "mein", "hum", "ne", "kis", "hafte", "is", "doon", "denge", "sakein", "batao", "se", "ko",
}
_JARGON_WORDS = re.compile(r"\b(payload|trigger|triggers|context|signal|signals|suppression|json|null|undefined)\b", re.I)
_SNAKE = re.compile(r"\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b")
_URL = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|in|org|net|co|io|app)\b", re.I)


def norm_number(tok: str) -> str:
    t = tok.replace(",", "").rstrip(".")
    try:
        return str(int(t))
    except ValueError:
        try:
            return f"{float(t):g}"
        except ValueError:
            return t


def extract_numbers(text: str, strip_allowed: bool = False) -> set[str]:
    if strip_allowed:
        for rx in _ALLOW:
            text = rx.sub(" ", text)
    return {norm_number(m.group()) for m in _NUM.finditer(text)}


def _sentences(body: str) -> list[str]:
    b = re.sub(r"\bDr\.", "Dr", body)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", b) if s.strip()]


def allowed_tokens(fs: FactSheet, extra_text: str = "") -> set[str]:
    toks: set[str] = {w.lower() for w in _WORD.findall(extra_text)}
    for f in fs.facts:
        toks |= {w.lower() for w in _WORD.findall(f.text)}
    for e in fs.allowed_entities:
        toks |= {w.lower() for w in _WORD.findall(e)}
    toks |= {w.lower() for w in _WORD.findall(fs.salutation + " " + fs.merchant_name)}
    return toks


def verify_rationale(rationale: str, body: str, fs: FactSheet, kind_words: str) -> list[str]:
    """The rationale must describe what the body actually does: name the trigger/hook, invent no numbers."""
    v: list[str] = []
    low = rationale.lower()
    hook = (fs.text("hook") or "").lower()
    if kind_words.lower() not in low and (not hook or hook[:25] not in low):
        v.append("rationale does not mention the trigger kind or the hook")
    licensed = extract_numbers(body)
    for f in fs.facts:
        licensed |= f.atoms
    stripped = re.sub(r"urgency \d/5", " ", rationale)
    for n in sorted(extract_numbers(stripped) - licensed):
        v.append(f"rationale mentions number {n!r} that is not in the message or facts")
    if _URL.search(rationale):
        v.append("rationale contains a URL")
    return v


def hook_covered(body: str, fs: FactSheet) -> bool:
    """LLM messages must actually lead with the hook fact (keeps message, rationale and trigger aligned)."""
    hook = fs.get("hook")
    if not hook:
        return True
    nums = hook.atoms
    if nums:
        return len(nums & extract_numbers(body)) >= max(1, len(nums) // 2)
    stop = {"your", "you", "the", "and", "for", "with", "that", "this", "our", "are", "was", "has", "have", "from", "time"}
    hw = {w.lower() for w in _WORD.findall(hook.text) if len(w) > 3 and w.lower() not in stop}
    bw = {w.lower() for w in _WORD.findall(body)}
    return len(hw & bw) >= min(2, len(hw))


def verify(out: Any, fs: FactSheet, recent_bodies: Iterable[str] = (), *, reply_mode: bool = False,
           extra_text: str = "") -> list[str]:
    v: list[str] = []
    if not isinstance(out, dict):
        return ["output is not a JSON object"]
    body = out.get("body")
    if not isinstance(body, str) or not body.strip():
        return ["body is empty"]
    if out.get("cta") not in CTA_TYPES:
        v.append(f"cta {out.get('cta')!r} is not one of {sorted(CTA_TYPES)}")
    used = out.get("facts_used")
    if used is not None:
        if not isinstance(used, list):
            v.append("facts_used must be a list")
        else:
            unknown = [u for u in used if u not in fs.ids()]
            if unknown:
                v.append(f"facts_used references unknown fact ids {unknown}")

    # 11. length
    if len(body) < (20 if reply_mode else 60):
        v.append("body is too short")
    if len(body) > 600:
        v.append("body is longer than 600 characters")

    # URLs are a hard fail in the official judge
    if _URL.search(body):
        v.append("body contains a URL")

    # 2. numeric grounding
    licensed: set[str] = set()
    for f in fs.facts:
        licensed |= f.atoms | extract_numbers(f.text)
    licensed |= extract_numbers(extra_text)
    for n in sorted(extract_numbers(body, strip_allowed=True) - licensed):
        v.append(f"number {n!r} is not in the facts")

    # 3. entity grounding (capitalised words that are not sentence-initial)
    ok = allowed_tokens(fs, extra_text)
    for s in _sentences(body):
        toks = _WORD.findall(s)
        for i, w in enumerate(toks):
            if i == 0 or not w[0].isupper() or len(w) < 2:
                continue
            lw = w.lower()
            if lw in ok or lw in _COMMON:
                continue
            v.append(f"unknown name/entity {w!r}")

    # 4. taboo words
    taboos = fs.voice.get("vocab_taboo") or fs.voice.get("taboos") or []
    low = body.lower()
    for t in taboos:
        if isinstance(t, str) and t.strip() and t.lower() in low:
            v.append(f"taboo word {t!r}")

    # 5. jargon
    for m in _SNAKE.findall(body):
        v.append(f"raw field name {m!r}")
    for m in _JARGON_WORDS.findall(body):
        v.append(f"internal jargon {m!r}")

    # 6. one ask
    if body.count("?") > 1:
        v.append("more than one question mark")
    sents = _sentences(body)
    if "?" in body and sents and "?" not in sents[-1]:
        v.append("the ask is not in the final sentence")
    replies = len(re.findall(r"\breply\b", body, re.I))
    if replies > (1 if fs.send_as == "merchant_on_behalf" else 1):
        v.append("more than one 'Reply ...' menu")

    # 7. salutation
    if fs.salutation and not reply_mode and fs.salutation not in body:
        v.append(f"salutation {fs.salutation!r} missing")

    # 8. language
    if fs.language == "hi-en" and not reply_mode:
        hits = {w for w in re.findall(r"[a-z]+", low) if w in _HINDI}
        if len(hits) < 2:
            v.append("hi-en merchant but body lacks Hindi-English code-mix")

    # 10. duplicates
    dup = too_similar(body, list(recent_bodies))
    if dup is not None:
        v.append("body is a duplicate/near-duplicate of a previous message")
    return v
