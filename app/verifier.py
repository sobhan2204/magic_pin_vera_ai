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


_UNIT_WORDS = r"(?:day|month|week|year|review|patient|member|customer|call|view)s?"
_PCT = re.compile(r"(?<![A-Za-z0-9_])(\d[\d,]*(?:\.\d+)?)\s*%")
_RUPEE = re.compile(r"\u20b9\s*(\d[\d,]*(?:\.\d+)?)")
_KM = re.compile(r"(?<![A-Za-z0-9_])(\d[\d,]*(?:\.\d+)?)\s*km\b", re.I)
_UNIT_TIGHT = re.compile(rf"(?<![A-Za-z0-9_])(\d[\d,]*(?:\.\d+)?)\s*-?\s*({_UNIT_WORDS})\b", re.I)
_UNIT_LOOSE = re.compile(rf"(?<![A-Za-z0-9_])(\d[\d,]*(?:\.\d+)?)\s*-?\s*(?:[A-Za-z\-]+\s+){{0,2}}?({_UNIT_WORDS})\b", re.I)


def _unit(u: str) -> str:
    u = u.lower()
    return u[:-1] if u.endswith("s") else u


def unit_pairs(text: str, loose: bool = False) -> set[tuple[str, str]]:
    """(number, unit) bindings such as ('95','%'), ('299','\u20b9'), ('4','month'). `loose` lets facts license
    'in the last 30 days' / '24 more customers' style phrasing; the message side is matched tightly."""
    out = {(norm_number(m.group(1)), "%") for m in _PCT.finditer(text)}
    out |= {(norm_number(m.group(1)), "\u20b9") for m in _RUPEE.finditer(text)}
    out |= {(norm_number(m.group(1)), "km") for m in _KM.finditer(text)}
    rx = _UNIT_LOOSE if loose else _UNIT_TIGHT
    out |= {(norm_number(m.group(1)), _unit(m.group(2))) for m in rx.finditer(text)}
    return out


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
    b = re.sub(r"\b(Dr|Mr|Mrs|Ms|Shri|Smt)\.", r"\1", body)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", b) if s.strip()]


def allowed_tokens(fs: FactSheet, extra_text: str = "") -> set[str]:
    toks: set[str] = {w.lower() for w in _WORD.findall(extra_text)}
    for f in fs.facts:
        toks |= {w.lower() for w in _WORD.findall(f.text + " " + (f.hi or ""))}
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


_FILLER = {
    "your", "you", "that", "this", "with", "from", "have", "been", "were", "will", "into", "about", "also", "which", "while",
    "aapke", "aapka", "aapki", "aapko", "hain", "hota", "hote", "gaye", "hue", "hue", "jabki", "jaisa", "isse", "isliye", "saath",
    "yeh", "wahi", "bhi", "sirf", "abhi", "kaafi", "mein", "pichle", "pichla", "kuch", "sabse", "zyada", "jyada", "lekin", "aur",
    "toh", "liye", "kiya", "kiye", "hoga", "rahe", "raha", "rahi", "chal", "upar", "neeche",
}


def ungrounded_sentences(body: str, fs: FactSheet) -> list[str]:
    """Sentences (other than the final ask) that neither carry a grounded number nor share a content word with any fact.
    Catches invented diagnoses/advice that contain no numbers, e.g. 'Ye low engagement ka sanket hai'."""
    sents = _sentences(body)
    if len(sents) < 2:
        return []
    own = {w.lower() for w in _WORD.findall(fs.salutation + " " + fs.merchant_name)}
    vocab: set[str] = set()
    licensed: set[str] = set()
    for f in fs.facts:
        vocab |= {w.lower() for w in _WORD.findall(f.text + " " + (f.hi or "")) if len(w) > 3}
        licensed |= f.atoms | extract_numbers(f.text) | extract_numbers(f.hi or "")
    vocab -= own | _FILLER
    bad = []
    for s in sents[:-1]:
        if re.search(r"\breply\b", s, re.I):
            continue
        if fs.send_as == "merchant_on_behalf" and re.match(r"^(hi|hello|namaste)\b", s.strip(), re.I) and len(s) < 90:
            continue                                     # "Hi Priya, <clinic> here." is the customer greeting
        if extract_numbers(s, strip_allowed=True) & licensed:
            continue
        content = {w.lower() for w in _WORD.findall(s) if len(w) > 3} - own - _FILLER
        if not content:
            continue
        if len(content & vocab) / len(content) < 0.25:
            bad.append(s.strip())
    return bad


def changed_field_used(body: str, fs: FactSheet, changed_first: bool) -> bool:
    """When the merchant's data was updated, an LLM draft must mention the updated value (not an unchanged field)."""
    f = fs.get("m.changed")
    if not f or not changed_first or fs.send_as != "vera":
        return True
    return bool(f.atoms & extract_numbers(body))


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



# ---- message-quality rules (live-run items 2, 4, 5, 7, 8) -------------------------------------------------------------------
# Sentences that say nothing: every message must state the actual fact instead.
GENERIC_PHRASES = re.compile(
    r"ek festival aa raha hai|a festival is coming up|festival is coming up|here's a quick update|quick update on your profile|"
    r"worth a look|something worth|new competitor has opened near you|naya competitor khula hai|close to a new milestone|"
    r"naye milestone ke kareeb|showing a recurring theme|baar-baar aane wala theme|profile numbers have dipped recently|"
    r"numbers haal hi mein gire|numbers picked up recently|we haven't seen you in a while|demand is shifting with the season|"
    r"there's an ipl match on today|supply alert has been issued|your plan is coming up for renewal|"
    r"your plan has lapsed and your profile is slowing|it's been a while since we last spoke", re.I)

# Words that make no sense for a customer of that business (unless the facts themselves say them).
CATEGORY_FORBIDDEN = {
    "dentists": ("refill", "medicine", "prescription", "class", "membership", "haircut", "facial"),
    "pharmacies": ("check-up", "checkup", "cleaning", "class", "membership", "haircut", "facial", "table", "appointment"),
    "gyms": ("check-up", "checkup", "refill", "medicine", "prescription", "cleaning", "haircut", "facial"),
    "salons": ("check-up", "checkup", "refill", "medicine", "prescription", "cleaning", "membership fee"),
    "restaurants": ("check-up", "checkup", "refill", "medicine", "prescription", "cleaning", "appointment", "class"),
}
_CAP_LEADS = {"your", "you", "you're", "it's", "we", "our", "aapka", "aapke", "aapki"}


def category_forbidden(body: str, fs: FactSheet) -> list[str]:
    fact_text = " ".join(f.text + " " + (f.hi or "") for f in fs.facts).lower()
    low = body.lower()
    return [w for w in CATEGORY_FORBIDDEN.get(fs.category_slug, ()) if re.search(rf"\b{re.escape(w)}", low) and w not in fact_text]


def repeated_facts(body: str) -> list[str]:
    """The same fact stated twice: two sentences that share a number and a content word, or say nearly the same thing."""
    sents = [x for x in _sentences(body) if len(x) > 12]
    out: list[str] = []
    info = []
    for x in sents:
        words_ = {w.lower() for w in _WORD.findall(x) if len(w) > 3} - _FILLER
        nums = {n for n in extract_numbers(x, strip_allowed=True) if len(n) >= 2}     # "3-month vs 6-month" is not a repeat
        info.append((x, nums, words_))
    for i in range(len(info)):
        for j in range(i + 1, len(info)):
            (a, na, wa), (b, nb, wb) = info[i], info[j]
            shared_words = wa & wb
            if (na & nb) and shared_words:
                out.append(f"the same fact is stated twice ({sorted(na & nb)[0]}): {b.strip()[:50]!r}")
            elif wa and wb and len(shared_words) / min(len(wa), len(wb)) >= 0.7 and len(shared_words) >= 3:
                out.append(f"the same point is repeated: {b.strip()[:50]!r}")
    return out


def polish(out: Any, fs: FactSheet) -> Any:
    """Tidy the salutation of a drafted body in place: "Anjali. Aapka plan" / "Dr. Bharat, Your Pro plan" -> "Anjali, aapka plan"."""
    body = out.get("body") if isinstance(out, dict) else None
    sal = fs.salutation
    if not isinstance(body, str) or not sal or fs.send_as == "merchant_on_behalf":
        return out
    m = re.match(rf"(\s*{re.escape(sal)})(\s*[.,]\s+|\s+)([A-Za-z']+)", body)
    if m:
        first = m.group(3)
        lower_ok = first.lower() in _CAP_LEADS or first.lower() in _HINDI or first.lower() in {"aapko", "aapke", "aapka", "aapki"}
        sep = m.group(2)
        if sep.strip() in (".", ",") or lower_ok:
            new_first = first[0].lower() + first[1:] if (lower_ok and first[0].isupper()) else first
            body = f"{m.group(1)}, {new_first}" + body[m.end():] if sep.strip() in (".", ",") else body
            out["body"] = body
    return out


def salutation_problems(body: str, fs: FactSheet) -> list[str]:
    sal = fs.salutation
    if not sal or fs.send_as == "merchant_on_behalf":
        return []
    out = []
    if re.match(rf"\s*{re.escape(sal)}\.\s", body):
        out.append("salutation is followed by a full stop instead of a comma")
    m = re.match(rf"\s*{re.escape(sal)}\s*[,\u2014-]\s*([A-Za-z']+)", body)
    if m and m.group(1)[0].isupper() and m.group(1).lower() in _CAP_LEADS:
        out.append(f"capital {m.group(1)!r} straight after the salutation")
    return out


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
    # a grounded number must also keep its meaning: "95 customers" is not "95%"
    stated: set[tuple[str, str]] = set()
    for f in fs.facts:
        stated |= unit_pairs(f.text, loose=True)
    stated |= unit_pairs(extra_text, loose=True)
    for n, u in sorted(unit_pairs(body) - stated):
        if n in licensed:
            v.append(f"{n}{'' if u in ('%', 'km') else ' '}{u} is not stated that way in the facts")

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

    # 12. message quality: no filler sentences, no raw key/value text, no repeated facts, tidy salutation, category fit
    if not reply_mode:
        m = GENERIC_PHRASES.search(body)
        if m:
            v.append(f"generic filler phrase {m.group(0)!r}: state the actual fact instead")
        if ";" in body:
            v.append("semicolon-joined text (looks like raw key/value data)")
        for w in category_forbidden(body, fs) if fs.send_as == "merchant_on_behalf" else []:
            v.append(f"word {w!r} does not fit a {fs.category_slug.rstrip('s')} business")
        v.extend(repeated_facts(body))
        if fs.language == "hi-en":
            for sent in _sentences(body):
                low_s = sent.lower()
                if len({w for w in re.findall(r"[a-z]+", low_s) if w in _HINDI}) >= 2 and re.search(r"\b(your|you're|our)\b", low_s):
                    v.append(f"English template fragment inside a Hindi sentence: {sent.strip()[:60]!r}")
                    break
        v.extend(salutation_problems(body, fs))

    # 10. duplicates
    dup = too_similar(body, list(recent_bodies))
    if dup is not None:
        v.append("body is a duplicate/near-duplicate of a previous message")
    return v
