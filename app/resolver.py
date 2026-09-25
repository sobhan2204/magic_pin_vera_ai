"""Resolver: joins trigger -> merchant -> category (-> customer) into a closed FactSheet.

Everything the writer or fallback may say comes from here. Derived numbers (months since a visit,
days to a deadline, percentages) are computed in code and added as facts.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .humanize import (
    beautify_dates, cap_days, plural, days_between, fmt_date, fmt_month_year, fmt_money, fmt_num, fmt_pct, fmt_time, first_sentence, humanize_signal,
    humanize_trend, join_and, months_between, parse_dt, strip_end, words,
)
from .models import Fact, FactSheet
from .normalize import Trigger
from .playbooks import get_playbook
from .verifier import extract_numbers

_KIND_WORD = {"research": "study", "compliance": "circular", "cde": "session", "trend": "trend",
              "tech": "launch", "alert": "alert"}
_METRICS = {"ctr": ("click-through rate", "is"), "views": ("views", "are"), "calls": ("calls", "are"),
            "leads": ("leads", "are"), "directions": ("direction requests", "are"),
            "review_count": ("reviews", "are")}
_THEMES = {"delivery_late": "late delivery", "wait_time": "long waits", "saturday_wait": "Saturday waits"}
_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DEFAULT_DIGEST = {"research_digest": ("research", "trend", "tech"), "regulation_change": ("compliance",),
                   "cde_opportunity": ("cde",), "supply_alert": ("alert",)}


def _window(w: Any) -> str:
    m = re.match(r"^(\d+)d$", str(w or ""))
    return f"{m.group(1)} days" if m else (words(w) if w else "week")


def _program(code: Any) -> str:
    w = words(code)
    m = re.match(r"^(.*?) (\d+)-?day$", w)
    return f"{m.group(2)}-day {m.group(1)}" if m else w


def _season(note: Any) -> str:
    w = words(note)
    w = re.sub(rf"\b({_MON}) ({_MON})$", lambda m: f"{m.group(1).title()}-{m.group(2).title()}", w)
    return w


class Ctx:
    def __init__(self, trigger: Trigger, merchant: dict, category: dict, customer: Optional[dict],
                 now: Optional[datetime]) -> None:
        self.t, self.m, self.cat, self.cust = trigger, merchant, category, customer
        self.now = now or datetime.now(timezone.utc)
        self.p = trigger.payload if isinstance(trigger.payload, dict) else {}
        self.ident = merchant.get("identity") or {}
        self.perf = merchant.get("performance") or {}
        self.facts: list[Fact] = []
        self.slots: list[str] = []
        self.entities: set[str] = set()

    # --- fact plumbing ---------------------------------------------------------------
    def add(self, key: str, text: str, source: str, entities: tuple = (), hi: Optional[str] = None) -> None:
        text = text.strip()
        if not text or self.has(key):
            return
        self.facts.append(Fact(id=f"F{len(self.facts) + 1}", key=key, text=text,
                               atoms=extract_numbers(text) | (extract_numbers(hi) if hi else set()), source=source,
                               hi=hi.strip() if hi else None))
        for e in entities:
            if e:
                self.entities.add(str(e))

    def has(self, key: str) -> bool:
        return any(f.key == key for f in self.facts)

    def get(self, key: str):
        return next((f for f in self.facts if f.key == key), None)

    # --- helpers -----------------------------------------------------------------------
    @property
    def peer(self) -> dict:
        return self.cat.get("peer_stats") or {}

    def active_offers(self) -> list[str]:
        return [o.get("title") for o in (self.m.get("offers") or [])
                if isinstance(o, dict) and o.get("status") == "active" and o.get("title")]

    def digest_item(self) -> Optional[dict]:
        p = self.p
        inline = p.get("top_item")
        if isinstance(inline, dict) and inline.get("title"):
            return inline
        wanted = p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id")
        if wanted:
            for it in self.cat.get("digest") or []:
                if isinstance(it, dict) and it.get("id") == wanted:
                    return it
            return None                                   # an explicit id that we cannot find is a missing join
        for kind in _DEFAULT_DIGEST.get(self.t.kind, ()):   # no id at all: use the category's latest item of that kind
            for it in self.cat.get("digest") or []:
                if isinstance(it, dict) and it.get("kind") == kind and it.get("title"):
                    return it
        return None

    def dt(self, key: str) -> Optional[datetime]:
        return parse_dt(self.p.get(key))

    def days_to(self, dt: Optional[datetime]) -> Optional[int]:
        d = days_between(dt, self.now)
        return d if d is not None and d >= 0 else None

    @staticmethod
    def plausible(n: Any) -> Optional[int]:
        """A day count we are willing to say out loud: a non-negative number of at most two years."""
        return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n <= 730 else None

    def pick_days(self, payload_key: str, dt: Optional[datetime]) -> Optional[int]:
        """Prefer the payload's own number (days_until ...); otherwise compute from the tick clock; omit if implausible."""
        return self.plausible(self.p.get(payload_key)) if self.plausible(self.p.get(payload_key)) is not None \
            else self.plausible(self.days_to(dt))

    def collect_slots(self, raw: Any) -> None:
        for s in raw or []:
            if isinstance(s, dict) and s.get("label"):
                self.slots.append(str(s["label"]))
        if self.slots:
            en = " or ".join(self.slots[:3])
            hi = " ya ".join(self.slots[:3])
            self.add("t.slots", "Open slots: " + en, "trigger.slots", hi="Aapke liye ye slots khaali hain: " + hi)
            offers = self.active_offers()
            if offers:                                    # slots + price in ONE supporting sentence (case-study shape)
                self.add("t.slots_offer", f"Open slots: {en}, and {offers[0]} is on right now", "trigger.slots", (offers[0],),
                         hi=f"Aapke liye ye slots khaali hain: {hi}, aur {offers[0]} abhi chal raha hai")


# ================================ trigger builders =============================================
Builder = Callable[[Ctx], bool]


def _digest_hook(c: Ctx, item: dict) -> None:
    src = item.get("source", "")
    word = _KIND_WORD.get(item.get("kind", ""), "item")
    title = beautify_dates(strip_end(item.get("title", "")))
    c.add("hook", f"{src} carries a new {word}: {title}" if src else f"There is a new {word}: {title}",
          "category.digest", (src,))
    if item.get("summary"):
        summ = first_sentence(beautify_dates(item["summary"]))
        n = (c.m.get("customer_aggregate") or {}).get("high_risk_adult_count")
        if n and "risk" in str(item.get("patient_segment", "")):
            summ += f", and your patient records show {fmt_num(n)} high-risk adult patients"
        c.add("t.summary", summ, "category.digest.summary")
    if item.get("trial_n"):
        seg = words(item.get("patient_segment", "patients")).replace("adults", "adult patients")
        c.add("t.trial", f"The trial covered {fmt_num(item['trial_n'])} {seg}", "category.digest.trial_n")


def _research(c: Ctx) -> bool:
    item = c.digest_item()
    if not item:
        return False
    _digest_hook(c, item)
    return True


def _regulation(c: Ctx) -> bool:
    item = c.digest_item()
    if not item:
        return False
    src = item.get("source", "")
    title = beautify_dates(strip_end(item.get("title", "")))
    dl = c.dt("deadline_iso")
    days = c.plausible(c.days_to(dl))
    tail = f" — {plural(days, 'day')} to go" if days is not None else ""
    c.add("hook", f"{title} ({src}){tail}" if src else f"{title}{tail}", "category.digest", (src,))
    if item.get("summary"):
        c.add("t.summary", first_sentence(beautify_dates(item["summary"])), "category.digest.summary")
    return True


def _cde(c: Ctx) -> bool:
    item = c.digest_item()
    if not item:
        return False
    when = parse_dt(item.get("date"))
    credits = c.p.get("credits") or item.get("credits")
    when_s = f" on {fmt_date(when)}, {fmt_time(when)}" if when else ""
    cred_s = f" and carries {credits} CDE credits" if credits else ""
    c.add("hook", f"{strip_end(item.get('title', ''))} is happening{when_s}{cred_s}", "category.digest",
          (item.get("source"),))
    fee = item.get("actionable") or (words(c.p["fee"]) if c.p.get("fee") else "")
    if fee:
        c.add("t.fee", strip_end(fee), "category.digest.actionable")
    return True


def _months_hook(c: Ctx) -> Optional[str]:
    last = parse_dt((c.cust or {}).get("relationship", {}).get("last_visit")) or c.dt("last_service_date")
    n = months_between(last, c.now)
    if n is not None and n > 60:
        n = None                                            # implausible: say nothing rather than something silly
    return f"It's been {plural(n, 'month')} since your last visit" if n else None


def _recall(c: Ctx) -> bool:
    service = c.p.get("service_due")
    due = f"your {words(service)} recall is due" if service else "it's time for your regular check-up"
    ago = _months_hook(c)
    n = months_between(parse_dt(((c.cust or {}).get("relationship") or {}).get("last_visit")), c.now)
    n = n if n and n <= 60 else None
    svc = words(service) if service else None
    hi = (f"Aapki last visit ko {n} mahine ho gaye hain, aur aapka {svc} recall due hai" if n and svc else
          f"Aapka {svc} recall due hai" if svc else "Aapke regular check-up ka time ho gaya hai")
    c.add("hook", f"{ago}, and {due}" if ago else due.capitalize(), "trigger.recall", hi=hi)
    dd = c.dt("due_date")
    if dd:
        c.add("t.due", f"Your recall date is {fmt_date(dd)}", "trigger.due_date")
    c.collect_slots(c.p.get("available_slots"))
    return True


def _lapsed_soft(c: Ctx) -> bool:
    ago = _months_hook(c)
    last = parse_dt(((c.cust or {}).get("relationship") or {}).get("last_visit"))
    n = months_between(last, c.now)
    if ago:
        c.add("hook", ago + ", so a quick check-up is due", "customer.relationship",
              hi=f"Aapki last visit ko {n} mahine ho gaye hain, isliye ek quick check-up due hai" if n else None)
    elif last:
        c.add("hook", f"Your last visit with us was on {fmt_date(last)}", "customer.relationship",
              hi=f"Aapki last visit {fmt_date(last)} ko thi")
    else:
        c.add("hook", "We haven't seen you in a while", "customer.relationship")
    c.collect_slots(c.p.get("available_slots"))
    return True


def _lapsed_hard(c: Ctx) -> bool:
    d = c.p.get("days_since_last_visit")
    if not d:
        return False
    c.add("hook", f"It's been {plural(d, 'day')} since your last visit", "trigger.days_since_last_visit",
          hi=f"Aapki last visit ko {d} din ho gaye hain")
    if c.p.get("previous_focus"):
        months = c.p.get("previous_membership_months")
        tail = f" during your {months} months with us" if months else ""
        c.add("t.focus", f"You were working on {words(c.p['previous_focus'])}{tail}", "trigger.previous_focus")
    return True


def _appointment(c: Ctx) -> bool:
    label = c.p.get("label") or c.p.get("slot_label")
    c.add("hook", f"Your appointment with us is tomorrow, {label}" if label else "Your appointment with us is tomorrow",
          "trigger.appointment")
    return True


def _wedding(c: Ctx) -> bool:
    wd = c.dt("wedding_date") or parse_dt(((c.cust or {}).get("preferences") or {}).get("wedding_date"))
    if not wd:
        return False
    days = c.pick_days("days_to_wedding", wd)
    days_s = f" — {plural(days, 'day')} to go —" if days is not None else ""
    prog = _program(c.p.get("next_step_window_open")) if c.p.get("next_step_window_open") else "skin prep"
    hi_days = f" — {days} din baaki —" if days is not None else ""
    c.add("hook", f"Your wedding is on {fmt_date(wd)}{days_s} and the {prog} window is open", "trigger.wedding_date",
          hi=f"Aapki shaadi {fmt_date(wd)} ko hai{hi_days} aur {prog} window khul gayi hai")
    tc = c.dt("trial_completed")
    if tc:
        c.add("t.trial", f"Your bridal trial was on {fmt_date(tc)}", "trigger.trial_completed",
              hi=f"Aapka bridal trial {fmt_date(tc)} ko hua tha")
    return True


def _trial(c: Ctx) -> bool:
    td = c.dt("trial_date")
    c.add("hook", f"Thank you for coming to the trial session on {fmt_date(td)}" if td else "Thank you for coming to the trial session",
          "trigger.trial_date",
          hi=f"{fmt_date(td)} ke trial session mein aane ke liye shukriya" if td else "Trial session mein aane ke liye shukriya")
    c.collect_slots(c.p.get("next_session_options"))
    return True


def _refill(c: Ctx) -> bool:
    meds = c.p.get("molecule_list") or []
    out = c.dt("stock_runs_out_iso")
    what = f"Your {join_and([str(m) for m in meds])} refill is due" if meds else "Your regular refill is due"
    tail = f", and your current stock runs out around {fmt_date(out)}" if out else ""
    hi_tail = f", aur aapka current stock {fmt_date(out)} ke aas-paas khatam hoga" if out else ""
    hi_what = f"Aapki {join_and([str(m) for m in meds]).replace(' and ', ' aur ')} refill due hai" if meds else "Aapki regular refill due hai"
    c.add("hook", what + tail, "trigger.refill", hi=hi_what + hi_tail)
    if c.p.get("delivery_address_saved"):
        c.add("t.delivery", "We have your delivery address saved, so we can send it over", "trigger.delivery_address_saved",
              hi="Aapka delivery address humare paas saved hai, isliye hum ise bhej sakte hain")
    return True


def _metric(m: Any) -> tuple[str, str]:
    return _METRICS.get(str(m), (words(m) if m else "numbers", "are"))


def _merchant_delta(c: Ctx, sign: int) -> Optional[tuple[str, float]]:
    d = c.perf.get("delta_7d") or {}
    for key, name in (("calls_pct", "calls"), ("views_pct", "views")):
        v = d.get(key)
        if isinstance(v, (int, float)) and v * sign > 0:
            return name, v
    return None


def _perf(c: Ctx, sign: int) -> bool:
    metric, delta = c.p.get("metric"), c.p.get("delta_pct")
    if not isinstance(delta, (int, float)):
        found = _merchant_delta(c, sign)
        if not found:
            return False
        metric, delta = found
    name, verb = _metric(metric)
    direction = "down" if delta < 0 else "up"
    base = c.p.get("vs_baseline")
    tail = f" (your usual is {fmt_num(base)})" if isinstance(base, (int, float)) else ""
    win = c.p.get("window", "7d")
    hi_win = re.sub(r"^(\d+)d$", r"\1 din", str(win)) if re.match(r"^\d+d$", str(win)) else words(win)
    hi_tail = f" (aapka usual {fmt_num(base)} hai)" if isinstance(base, (int, float)) else ""
    c.add("hook", f"Your Google profile shows {name} {direction} {fmt_pct(delta)} over the last {_window(win)}{tail}",
          "trigger.delta_pct",
          hi=f"Aapke Google profile par {name} pichle {hi_win} mein {fmt_pct(delta)} {'gir gaye hain' if delta < 0 else 'badh gaye hain'}{hi_tail}")
    if c.p.get("likely_driver"):
        c.add("t.driver", f"The likely driver is your {words(c.p['likely_driver'])}", "trigger.likely_driver")
    return True


def _perf_dip(c: Ctx) -> bool:
    return _perf(c, -1)


def _perf_spike(c: Ctx) -> bool:
    return _perf(c, +1)


def _seasonal_dip(c: Ctx) -> bool:
    if not c.p.get("is_expected_seasonal"):
        return _perf(c, -1)
    name, verb = _metric(c.p.get("metric", "views"))
    delta = c.p.get("delta_pct")
    if not isinstance(delta, (int, float)):
        return False
    season = f" ({_season(c.p['season_note'])})" if c.p.get("season_note") else ""
    c.add("hook", f"Your Google profile shows {name} down {fmt_pct(delta)} over the last {_window(c.p.get('window', '7d'))}, "
                  f"which is an expected seasonal dip{season}", "trigger.is_expected_seasonal")
    c.add("t.reframe", "It's a normal seasonal pattern, not a problem with your profile", "trigger.is_expected_seasonal",
          hi="Ye ek normal seasonal pattern hai, aapke profile mein koi problem nahi hai")
    return True


def _renewal(c: Ctx) -> bool:
    days, plan, amt = c.p.get("days_remaining"), c.p.get("plan"), c.p.get("renewal_amount")
    if days is None:
        return False
    tail = f", and renewal is {fmt_money(amt)}" if amt else ""
    hi_tail = f", aur renewal {fmt_money(amt)} ka hai" if amt else ""
    c.add("hook", f"Your {plan + ' ' if plan else ''}plan has {plural(days, 'day')} left{tail}", "trigger.days_remaining",
          hi=f"Aapke {plan + ' ' if plan else ''}plan mein ab sirf {days} din bache hain{hi_tail}")
    return True


def _winback(c: Ctx) -> bool:
    d = c.p.get("days_since_expiry")
    if d is None:
        return False
    dip = c.p.get("perf_dip_pct")
    tail = f", and your numbers are down {fmt_pct(dip)} since" if isinstance(dip, (int, float)) else ""
    hi_tail = f", aur tab se aapke numbers {fmt_pct(dip)} gir gaye hain" if isinstance(dip, (int, float)) else ""
    c.add("hook", f"Your plan expired {plural(d, 'day')} ago{tail}", "trigger.days_since_expiry",
          hi=f"Aapka plan {d} din pehle expire ho gaya{hi_tail}")
    n = c.p.get("lapsed_customers_added_since_expiry")
    if n:
        c.add("t.lapsed", f"{n} more customers have gone quiet in that time", "trigger.lapsed_customers_added_since_expiry",
              hi=f"Is dauraan {n} aur customers inactive ho gaye hain")
    return True


def _dormant(c: Ctx) -> bool:
    d = c.p.get("days_since_last_merchant_message")
    if d is None:
        return False
    topic = f", when we talked about {words(c.p['last_topic'])}" if c.p.get("last_topic") else ""
    hi_topic = f", jab humne {words(c.p['last_topic'])} ke baare mein baat ki thi" if c.p.get("last_topic") else ""
    c.add("hook", f"It's been {plural(d, 'day')} since we last spoke{topic}", "trigger.days_since_last_merchant_message",
          hi=f"Humari last baat ko {d} din ho gaye hain{hi_topic}")
    return True


def _festival(c: Ctx) -> bool:
    fest, d = c.p.get("festival"), c.dt("date")
    if not fest or not d:
        return False
    days = c.pick_days("days_until", d)
    tail = f" — {days} days away" if days and days > 1 else (" — tomorrow" if days == 1 else (" — today" if days == 0 else ""))
    hi_tail = f", yaani abhi {days} din baaki hain" if days and days > 1 else (", yaani kal" if days == 1 else (", yaani aaj" if days == 0 else ""))
    c.add("hook", f"{fest} is on {fmt_date(d)}{tail}", "trigger.festival", (fest,), hi=f"{fest} {fmt_date(d)} ko hai{hi_tail}")
    return True


def _seasonal_cat(c: Ctx) -> bool:
    trends = [humanize_trend(t) for t in (c.p.get("trends") or [])]
    if not trends:
        return False
    season = words(c.p.get("season", "this season")).capitalize()
    c.add("hook", f"{season} is shifting demand: {join_and(trends)}", "trigger.trends")
    if c.p.get("shelf_action_recommended"):
        c.add("t.action", "It may be worth moving the fast-rising items to the front of your shelf", "trigger.shelf_action_recommended")
    return True


def _ipl(c: Ctx) -> bool:
    match, mt = c.p.get("match"), c.dt("match_time_iso")
    if not match:
        return False
    venue = f" at {c.p['venue']}" if c.p.get("venue") else ""
    when = ""
    if mt:
        local_now = c.now.astimezone(mt.tzinfo)
        when = f" tonight, {fmt_time(mt)}" if local_now.date() == mt.date() else f" on {fmt_date(mt)}, {fmt_time(mt)}"
    c.add("hook", f"{match}{venue}{when}", "trigger.match", (match, c.p.get("venue")))
    offers = c.active_offers()
    with_offer = f", using your {offers[0]} offer" if offers else ""
    if c.p.get("is_weeknight") is True:
        c.add("t.advice", f"It's a weeknight, so a match-night combo can pull in dine-in crowds{with_offer}", "trigger.is_weeknight",
              tuple(offers[:1]))
    elif c.p.get("is_weeknight") is False:
        c.add("t.advice", f"It's not a weeknight, so lean on delivery rather than a dine-in match promo{with_offer}",
              "trigger.is_weeknight", tuple(offers[:1]))
    return True


def _review_theme(c: Ctx) -> bool:
    theme, n = c.p.get("theme"), c.p.get("occurrences_30d")
    if not theme or not n:
        return False
    trend = f", and it's {c.p['trend']}" if c.p.get("trend") else ""
    hi_trend = ", aur ye badh raha hai" if c.p.get("trend") == "rising" else ""
    c.add("hook", f"{plural(n, 'review')} in the last 30 days mention {_THEMES.get(theme, words(theme))}{trend}", "trigger.theme",
          hi=f"Pichle 30 din mein {n} reviews mein {_THEMES.get(theme, words(theme))} ka zikr hai{hi_trend}")
    if c.p.get("common_quote"):
        c.add("t.quote", f"One reviewer wrote: “{strip_end(c.p['common_quote'])}”", "trigger.common_quote")
    return True


def _milestone(c: Ctx) -> bool:
    now_v, goal = c.p.get("value_now"), c.p.get("milestone_value")
    if not isinstance(now_v, (int, float)) or not isinstance(goal, (int, float)):
        return False
    name = _metric(c.p.get("metric", "review_count"))[0]
    gap = int(goal - now_v)
    c.add("hook", f"You're at {fmt_num(now_v)} {name}, just {gap} away from {fmt_num(goal)}" if gap > 0
          else f"You've crossed {fmt_num(goal)} {name}", "trigger.milestone",
          hi=f"Aap {fmt_num(now_v)} {name} par hain, {fmt_num(goal)} se bas {gap} door" if gap > 0 else None)
    avg = c.peer.get("avg_review_count") or c.peer.get("avg_reviews")
    if avg:
        c.add("t.peer", f"That's already ahead of the peer average of {fmt_num(avg)}" if now_v > avg
              else f"The peer average is {fmt_num(avg)}", "category.peer_stats",
              hi=f"Ye peer average {fmt_num(avg)} se already aage hai" if now_v > avg else f"Peer average {fmt_num(avg)} hai")
    return True


def _planning(c: Ctx) -> bool:
    topic = c.p.get("intent_topic")
    if not topic:
        return False
    c.add("hook", f"You asked about your {words(topic)} idea, so here's a starter outline to edit", "trigger.intent_topic")
    offers = c.active_offers()
    anchor = offers[0] if offers else "your current offers"
    c.add("t.draft", f"Outline: who it's for, what's included, how you price it against {anchor}, and the announcement message",
          "playbook", tuple(offers[:1]))
    return True


def _curious(c: Ctx) -> bool:
    c.add("hook", f"Time for our regular check-in on what's in demand at {c.ident.get('name', 'your place')}",
          "trigger.curious_ask", (c.ident.get("name"),),
          hi=f"{c.ident.get('name', 'Aapke yahan')} mein abhi kya demand mein hai, iska hamara regular check-in ka time ho gaya hai")
    c.add("t.reciprocity", "I'll turn your answer into a Google post and a short WhatsApp reply you can use with customers",
          "playbook")
    return True


def _competitor(c: Ctx) -> bool:
    name, km = c.p.get("competitor_name"), c.p.get("distance_km")
    if not name and km is None:
        return False
    opened = c.dt("opened_date")
    offer = f", advertising {c.p['their_offer']}" if c.p.get("their_offer") else ""
    where = f" {km} km away" if km is not None else " nearby"
    on = f" on {fmt_date(opened)}" if opened else ""
    hi_offer = f", jo {c.p['their_offer']} chala raha hai" if c.p.get("their_offer") else ""
    hi_where = f" {km} km door" if km is not None else " paas"
    hi_on = f" {fmt_date(opened)} ko khula hai" if opened else " khula hai"
    c.add("hook", f"A new competitor, {name}, opened{where}{on}{offer}" if name else f"A new competitor opened{where}{on}{offer}",
          "trigger.competitor", (name, c.p.get("their_offer")),
          hi=f"{hi_where.strip()} ek naya competitor{', ' + name if name else ''},{hi_on}{hi_offer}".replace(" ,", ","))
    return True


def _gbp(c: Ctx) -> bool:
    verified = c.p.get("verified", c.ident.get("verified"))
    if verified is not False:
        return False
    path = f" — it can be done by {words(c.p['verification_path'])}" if c.p.get("verification_path") else ""
    hi_path = f", ye {words(c.p['verification_path'])} se ho sakta hai" if c.p.get("verification_path") else ""
    c.add("hook", f"Your Google Business Profile is not verified yet{path}", "trigger.verified",
          hi=f"Aapka Google Business Profile abhi verify nahi hua hai{hi_path}")
    up = c.p.get("estimated_uplift_pct")
    if isinstance(up, (int, float)):
        c.add("t.uplift", f"An estimated {fmt_pct(up)} uplift in views is on the table once it's verified", "trigger.estimated_uplift_pct")
    return True


def _supply(c: Ctx) -> bool:
    item = c.digest_item()
    mol, batches, mfr = c.p.get("molecule"), c.p.get("affected_batches") or [], c.p.get("manufacturer")
    src = (item or {}).get("source", "")
    if mol and batches:
        frm = f" from {mfr}" if mfr else ""
        cite = f" ({src})" if src else ""
        c.add("hook", f"A recall notice{cite} covers {mol} batches {join_and([str(b) for b in batches])}{frm}",
              "trigger.affected_batches", (mfr, src))
    elif item:
        c.add("hook", f"{strip_end(item.get('title', ''))} ({src})", "category.digest", (src,))
    else:
        return False
    if item and item.get("actionable"):
        c.add("t.action", strip_end(item["actionable"]), "category.digest.actionable")
    return True


BUILDERS: dict[str, Builder] = {
    "research_digest": _research, "regulation_change": _regulation, "cde_opportunity": _cde,
    "recall_due": _recall, "customer_lapsed_soft": _lapsed_soft, "customer_lapsed_hard": _lapsed_hard,
    "appointment_tomorrow": _appointment, "wedding_package_followup": _wedding, "trial_followup": _trial,
    "chronic_refill_due": _refill, "perf_dip": _perf_dip, "perf_spike": _perf_spike,
    "seasonal_perf_dip": _seasonal_dip, "renewal_due": _renewal, "winback_eligible": _winback,
    "dormant_with_vera": _dormant, "festival_upcoming": _festival, "category_seasonal": _seasonal_cat,
    "ipl_match_today": _ipl, "review_theme_emerged": _review_theme, "milestone_reached": _milestone,
    "active_planning_intent": _planning, "curious_ask_due": _curious, "competitor_opened": _competitor,
    "gbp_unverified": _gbp, "supply_alert": _supply,
}

# these kinds are useless without their digest item -> missing join
_STRICT = {"research_digest", "regulation_change", "cde_opportunity"}


def _generic_hook(c: Ctx) -> None:
    """Most specific thing we know when the kind is unknown or its payload is empty/placeholder."""
    if c.t.scope == "customer":
        ago = _months_hook(c)
        c.add("hook", ago or "We haven't seen you in a while", "customer.relationship")
        return
    if not c.p.get("placeholder"):
        bits = []
        for k, v in c.p.items():
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and k not in ("category",) \
                    and not str(k).endswith("_id") and not str(k).endswith("_iso"):
                bits.append(f"{words(k)} {v}" if isinstance(v, (int, float)) else f"{words(k)}: {words(v)}")
            if len(bits) == 2:
                break
        if bits:
            c.add("hook", "Here's something worth a look: " + "; ".join(bits), "trigger.payload")
            return


# What to lead with when the trigger payload carries no data (all generated triggers): first a trigger-specific fact derived
# from the merchant/category context, then an honest kind-level statement, then the strongest merchant fact.
_FALLBACK_KEYS = {
    "perf_dip": ("m.week_down", "m.views_below", "m.calls_below"),
    "perf_spike": ("m.week_up", "m.views_above", "m.calls_above"),
    "seasonal_perf_dip": ("m.week_down", "m.views_below", "m.calls_below"),
    "renewal_due": ("m.subscription",),
    "winback_eligible": ("m.expired",),
    "dormant_with_vera": ("m.dormant",),
    "review_theme_emerged": ("m.review",),
}
_KIND_STATEMENT = {
    "perf_dip": "Your profile numbers have dipped recently",
    "perf_spike": "Your profile numbers picked up recently",
    "seasonal_perf_dip": "Your profile numbers have dipped, which is common at this time of year",
    "competitor_opened": "A new competitor has opened near you",
    "milestone_reached": "You're close to a new milestone on your profile",
    "review_theme_emerged": "Recent reviews are showing a recurring theme",
    "festival_upcoming": "A festival is coming up",
    "category_seasonal": "Demand is shifting with the season",
    "ipl_match_today": "There's an IPL match on today",
    "active_planning_intent": "Following up on the idea you shared with me",
    "renewal_due": "Your plan is coming up for renewal",
    "winback_eligible": "Your plan has lapsed and your profile is slowing down",
    "dormant_with_vera": "It's been a while since we last spoke",
    "gbp_unverified": "Your Google Business Profile still needs verification",
    "supply_alert": "A supply alert has been issued for your category",
}


_KIND_STATEMENT_HI = {
    "perf_dip": "Aapke profile ke numbers haal hi mein gire hain",
    "perf_spike": "Aapke profile ke numbers haal hi mein badhe hain",
    "seasonal_perf_dip": "Aapke profile ke numbers gire hain, jo saal ke is samay mein aam baat hai",
    "competitor_opened": "Aapke paas ek naya competitor khula hai",
    "milestone_reached": "Aap apne profile par ek naye milestone ke kareeb hain",
    "review_theme_emerged": "Recent reviews mein ek baar-baar aane wala theme dikh raha hai",
    "festival_upcoming": "Ek festival aa raha hai",
    "category_seasonal": "Season ke saath demand shift ho rahi hai",
    "ipl_match_today": "Aaj IPL match hai",
    "active_planning_intent": "Aapne jo idea share kiya tha, ye usi ka follow-up hai",
    "renewal_due": "Aapka plan renewal ke liye aa raha hai",
    "winback_eligible": "Aapka plan lapse ho gaya hai aur profile slow ho raha hai",
    "dormant_with_vera": "Humari baat ko kaafi time ho gaya hai",
    "gbp_unverified": "Aapke Google Business Profile ko abhi verification chahiye",
    "supply_alert": "Aapki category ke liye ek supply alert aaya hai",
}


def _strongest_keys(c: Ctx) -> list[str]:
    """Merchant facts ranked by how striking they are: peer gap, lapsed customers, review theme, retention gap, offers."""
    agg = c.m.get("customer_aggregate") or {}
    cands: list[tuple[float, str]] = []
    for k in ("views", "calls"):
        v, pv = c.perf.get(k), c.peer.get(f"avg_{k}_30d")
        if isinstance(v, (int, float)) and isinstance(pv, (int, float)) and pv:
            cands.append((abs(v - pv) / pv, f"m.{k}_{'below' if v < pv else 'above'}"))
    ctr, pc = c.perf.get("ctr"), c.peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(pc, (int, float)) and pc:
        cands.append((0.8 * abs(ctr - pc) / pc, "m.ctr"))
    for key in ("lapsed_180d_plus", "lapsed_90d_plus"):
        if agg.get(key):
            total = agg.get("total_unique_ytd")
            cands.append((1.5 * (agg[key] / total if total else 0.2), "m.lapsed"))
            break
    for th in c.m.get("review_themes") or []:
        if isinstance(th, dict) and th.get("occurrences_30d"):
            cands.append((min(1.0, th["occurrences_30d"] / 10) + 0.05, "m.review"))
            break
    ret, pr = agg.get("retention_6mo_pct"), c.peer.get("retention_6mo_pct")
    if isinstance(ret, (int, float)) and isinstance(pr, (int, float)) and pr:
        cands.append((abs(ret - pr) / pr, "m.retention"))
    if agg.get("high_risk_adult_count"):
        cands.append((0.3, "m.cohort"))
    if c.active_offers():
        cands.append((0.25, "m.offers"))
    cands.sort(key=lambda x: (-x[0], x[1]))
    return [k for _, k in cands if c.get(k)]


def _fallback_hook(c: Ctx) -> None:
    """The trigger payload is thin/placeholder: lead with the most relevant, then the strongest, merchant fact."""
    def promote(fact: Fact) -> None:
        c.facts.remove(fact)
        c.facts.insert(0, replace(fact, key="hook"))

    for key in _FALLBACK_KEYS.get(c.t.kind, ()):
        f = c.get(key)
        if f:
            promote(f)
            return
    stmt = _KIND_STATEMENT.get(c.t.kind)
    if c.t.scope != "customer":
        for key in _strongest_keys(c):
            promote(c.get(key))
            if stmt:                                     # the trigger itself becomes the one supporting sentence
                c.facts.insert(1, Fact(id="", key="t.kind", text=stmt, atoms=set(), source="trigger.kind",
                                       hi=_KIND_STATEMENT_HI.get(c.t.kind)))
            return
    if stmt:
        c.facts.insert(0, Fact(id="", key="hook", text=stmt, atoms=set(), source="trigger.kind",
                               hi=_KIND_STATEMENT_HI.get(c.t.kind)))
        return
    c.facts.insert(0, Fact(id="", key="hook", text="Here's a quick update on your profile", atoms=set(), source="merchant"))


# ================================ merchant / customer facts ===================================
# Facts say where they come from ("Your Google profile shows ...", "Your customer records show ...").
def _merchant_facts(c: Ctx) -> None:
    m, perf, cust_agg = c.m, c.perf, c.m.get("customer_aggregate") or {}
    win = perf.get("window_days", 30)
    parts = []
    if perf.get("views") is not None:
        parts.append(f"{fmt_num(perf['views'])} views")
    if perf.get("calls") is not None:
        parts.append(f"{fmt_num(perf['calls'])} calls")
    if perf.get("directions") is not None:
        parts.append(f"{fmt_num(perf['directions'])} direction requests")
    if parts:
        c.add("m.perf30", f"Your Google profile shows {join_and(parts)} in the last {win} days", "merchant.performance",
              hi=f"Aapke Google profile par pichle {win} din mein {join_and(parts).replace(' and ', ' aur ')} dikhe")
    ctr, peer_ctr = perf.get("ctr"), c.peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
        c.add("m.ctr", f"Your Google profile shows a click-through rate of {fmt_pct(ctr)} against a peer average of {fmt_pct(peer_ctr)}",
              "merchant.performance.ctr",
              hi=f"Aapke Google profile par click-through rate {fmt_pct(ctr)} hai, jabki peer average {fmt_pct(peer_ctr)} hai")
    elif isinstance(ctr, (int, float)):
        c.add("m.ctr", f"Your Google profile shows a click-through rate of {fmt_pct(ctr)}", "merchant.performance.ctr",
              hi=f"Aapke Google profile par click-through rate {fmt_pct(ctr)} hai")
    offers = c.active_offers()
    if offers:
        c.add("m.offers", f"Your offer catalog has {join_and(offers)} live", "merchant.offers", tuple(offers),
              hi=f"Aapke offer catalog mein {join_and(offers).replace(' and ', ' aur ')} live hai")
        c.add("m.offer_price", f"{offers[0]} is on right now", "merchant.offers", (offers[0],))
    if isinstance(cust_agg.get("retention_6mo_pct"), (int, float)):
        pr = c.peer.get("retention_6mo_pct")
        tail = f" against {fmt_pct(pr)} for peers" if isinstance(pr, (int, float)) else ""
        c.add("m.retention", f"Your customer records show a 6-month retention of {fmt_pct(cust_agg['retention_6mo_pct'])}{tail}",
              "merchant.customer_aggregate")
    elif isinstance(cust_agg.get("retention_3mo_pct"), (int, float)):
        c.add("m.retention", f"Your customer records show a 3-month retention of {fmt_pct(cust_agg['retention_3mo_pct'])}",
              "merchant.customer_aggregate")
    if cust_agg.get("high_risk_adult_count"):
        c.add("m.cohort", f"Your patient records show {fmt_num(cust_agg['high_risk_adult_count'])} high-risk adult patients",
              "merchant.customer_aggregate")
    for k, days in (("lapsed_180d_plus", 180), ("lapsed_90d_plus", 90)):
        if cust_agg.get(k):
            c.add("m.lapsed", f"Your customer records show {fmt_num(cust_agg[k])} customers who haven't visited in over {days} days",
                  "merchant.customer_aggregate",
                  hi=f"Aapke customer records mein {fmt_num(cust_agg[k])} customers hain jo {days} din se zyada se nahi aaye")
            break
    for key, name, peer_key in (("views", "views", "avg_views_30d"), ("calls", "calls", "avg_calls_30d")):
        pv = c.peer.get(peer_key)
        if isinstance(perf.get(key), (int, float)) and isinstance(pv, (int, float)):
            text = f"Your Google profile shows {fmt_num(perf[key])} {name} over {win} days, against a peer average of {fmt_num(pv)}"
            hi_text = f"Aapke Google profile par {win} din mein {fmt_num(perf[key])} {name} hain, jabki peer average {fmt_num(pv)} hai"
            c.add(f"m.{key}_peer", text, f"merchant.performance.{key}", hi=hi_text)
            if perf[key] != pv:
                c.add(f"m.{key}_{'below' if perf[key] < pv else 'above'}", text, f"merchant.performance.{key}", hi=hi_text)
    d7 = perf.get("delta_7d") or {}
    for key, name in (("views_pct", "views"), ("calls_pct", "calls")):
        v = d7.get(key)
        if isinstance(v, (int, float)) and v != 0:
            text = f"Your Google profile shows {name} {'up' if v > 0 else 'down'} {fmt_pct(v)} this week"
            hi_text = f"Aapke Google profile par is hafte {name} {fmt_pct(v)} {'badhe' if v > 0 else 'gire'} hain"
            c.add("m.week", text, "merchant.performance.delta_7d", hi=hi_text)
            c.add("m.week_up" if v > 0 else "m.week_down", text, "merchant.performance.delta_7d", hi=hi_text)
            break
    hist = [h for h in (m.get("conversation_history") or []) if isinstance(h, dict)]
    last_ts = parse_dt(hist[-1].get("ts")) if hist else None
    quiet = (c.now - last_ts).days if last_ts else None
    if quiet is None or quiet < 0 or quiet > 730:
        quiet = None
        for sig in m.get("signals") or []:
            mm = re.match(r"dormant_with_vera_(\d+)d$", str(sig))
            if mm:
                quiet = int(mm.group(1))
    if quiet and 1 <= quiet <= 730:
        c.add("m.dormant", f"Our chat history shows it's been {plural(quiet, 'day')} since we last spoke", "merchant.conversation_history")
    sub = m.get("subscription") or {}
    if sub.get("status") == "active" and sub.get("days_remaining") is not None:
        plan = f"your {sub['plan']} plan" if sub.get("plan") else "your plan"
        c.add("m.subscription", f"Your magicpin subscription shows {plan} has {plural(sub['days_remaining'], 'day')} left",
              "merchant.subscription",
              hi=f"Aapke magicpin subscription ke hisaab se {plan} mein {sub['days_remaining']} din bache hain")
    elif sub.get("status") == "expired" and sub.get("days_since_expiry"):
        text = f"Your magicpin subscription shows your plan expired {plural(sub['days_since_expiry'], 'day')} ago"
        hi_text = f"Aapke magicpin subscription ke hisaab se aapka plan {sub['days_since_expiry']} din pehle expire ho gaya"
        c.add("m.subscription", text, "merchant.subscription", hi=hi_text)
        c.add("m.expired", text, "merchant.subscription", hi=hi_text)
    for th in m.get("review_themes") or []:
        if isinstance(th, dict) and th.get("theme") and th.get("occurrences_30d"):
            verb = "praise" if th.get("sentiment") == "pos" else "mention"
            name = _THEMES.get(th["theme"], words(th["theme"]))
            c.add("m.review", f"Your recent Google reviews show {th['occurrences_30d']} that {verb} {name}", "merchant.review_themes")
            break
    sigs = [h for h in (humanize_signal(s) for s in (m.get("signals") or [])) if h]
    if sigs:
        c.add("m.signals", "On your profile: " + join_and(sigs[:3]), "merchant.signals")
    for turn in reversed(m.get("conversation_history") or []):
        if isinstance(turn, dict) and turn.get("from") == "merchant" and turn.get("body"):
            c.add("m.history", f"When we last spoke you said: “{strip_end(turn['body']).replace('?', '')}”", "merchant.conversation_history")
            break


def _month_in_range(rng: str, month: int) -> bool:
    names = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    parts = [x.strip()[:3].lower() for x in str(rng).replace("–", "-").split("-") if x.strip()]
    if not parts or any(x not in names for x in parts):
        return False
    lo, hi = names.index(parts[0]) + 1, names.index(parts[-1]) + 1
    return lo <= month <= hi if lo <= hi else (month >= lo or month <= hi)


def _category_facts(c: Ctx) -> None:
    for beat in c.cat.get("seasonal_beats") or []:
        if isinstance(beat, dict) and beat.get("note") and _month_in_range(beat.get("month_range", ""), c.now.month):
            c.add("cat.season", f"Category data shows a seasonal pattern for {beat['month_range']}: {strip_end(beat['note'])}",
                  "category.seasonal_beats")
            break
    trends = [t for t in (c.cat.get("trend_signals") or []) if isinstance(t, dict) and isinstance(t.get("delta_yoy"), (int, float))]
    if trends:
        top = max(trends, key=lambda t: abs(t["delta_yoy"]))
        c.add("cat.trend", f"Search trends show “{top.get('query')}” searches {'up' if top['delta_yoy'] > 0 else 'down'} "
                           f"{fmt_pct(top['delta_yoy'])} year on year", "category.trend_signals")
    if not c.active_offers():
        ideas = [o.get("title") for o in (c.cat.get("offer_catalog") or [])
                 if isinstance(o, dict) and o.get("type") == "service_at_price" and o.get("title")][:2]
        if ideas:
            c.add("cat.offers", f"Popular offers in your category include {join_and(ideas)}", "category.offer_catalog", tuple(ideas))


def _customer_facts(c: Ctx) -> None:
    cu = c.cust or {}
    rel = cu.get("relationship") or {}
    n = months_between(parse_dt(rel.get("last_visit")), c.now)
    if n and n <= 60:
        c.add("c.months", f"It's been {n} months since your last visit", "customer.relationship.last_visit")
    last = parse_dt(rel.get("last_visit"))
    if last and last <= c.now:
        c.add("c.lastvisit", f"Our records show your last visit was on {fmt_date(last)}", "customer.relationship.last_visit")
    first = parse_dt(rel.get("first_visit"))
    if first and first <= c.now:
        c.add("c.since", f"Our records show you've been with us since {fmt_month_year(first)}", "customer.relationship.first_visit")
    if rel.get("visits_total"):
        c.add("c.visits", f"Our records show you've visited {plural(rel['visits_total'], 'time')}", "customer.relationship.visits_total")
    if rel.get("favourite_dish"):
        c.add("c.fav", f"Your favourite with us is {rel['favourite_dish']}", "customer.relationship.favourite_dish", (rel["favourite_dish"],))
    slots_pref = (cu.get("preferences") or {}).get("preferred_slots")
    if slots_pref:
        c.add("c.pref", f"You prefer {cap_days(words(slots_pref))} slots", "customer.preferences.preferred_slots")
    seen: list[str] = []
    for svc in rel.get("services_received") or []:
        w = words(svc)
        if w and w != "..." and w not in seen:
            seen.append(w)
    if seen:
        c.add("c.services", f"Our records show you've had {join_and(seen[:3])} with us before", "customer.relationship.services_received")
    stylist = (cu.get("preferences") or {}).get("preferred_stylist")
    if stylist:
        c.add("c.stylist", f"Your preferred stylist is {stylist}", "customer.preferences", (stylist,))


# ================================ entry point ====================================================
def _first_name(name: str) -> str:
    m = re.search(r"\(parent:\s*([^)]+)\)", name)
    if m:
        return m.group(1).strip()
    base = name.split("(")[0].strip()
    toks = base.split()
    if not toks:
        return ""
    if toks[0].rstrip(".").lower() in ("mr", "mrs", "ms", "dr", "shri", "smt") and len(toks) > 1:
        return f"{toks[0].rstrip('.')}. {toks[1]}"
    return toks[0]


def make_salutation(category_slug: str, merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    owner = ident.get("owner_first_name")
    if owner:
        if category_slug == "dentists" and not re.match(r"^dr\b\.?", owner.strip(), re.I):
            return f"Dr. {owner}"
        return owner.strip()
    return ident.get("name", "there")


def build_factsheet(trigger: Trigger, merchant: dict, category: dict, customer: Optional[dict],
                    now: Optional[datetime]) -> Optional[FactSheet]:
    pb = get_playbook(trigger.kind)
    c = Ctx(trigger, merchant, category, customer, now)
    builder = BUILDERS.get(trigger.kind)
    ok = builder(c) if builder else False
    if not ok:
        if trigger.kind in _STRICT:
            return None                       # required digest item not found -> missing join
        _generic_hook(c)
    _merchant_facts(c)
    _category_facts(c)
    if customer:
        _customer_facts(c)
    if not c.has("hook"):
        _fallback_hook(c)
    if trigger.scope == "customer" and customer:
        # A message to a customer may only use the trigger, the customer's own data and the offer price: never the merchant's
        # subscription, performance, peer benchmarks or internal history.
        c.facts = [f for f in c.facts if f.key == "hook" or f.key.startswith(("t.", "c.")) or f.key == "m.offer_price"]
    for i, f in enumerate(c.facts):           # ids follow the final order (F1 is always the hook)
        f.id = f"F{i + 1}"

    slug = merchant.get("category_slug") or category.get("slug", "")
    ident = merchant.get("identity") or {}
    customer_facing = trigger.scope == "customer" and bool(customer)
    if customer_facing:
        cname = ((customer or {}).get("identity") or {}).get("name", "")
        salutation = _first_name(cname) or "there"
        lang_pref = str(((customer or {}).get("identity") or {}).get("language_pref", "")).lower()
        language = "hi-en" if re.search(r"\bhi\b|hindi", lang_pref) else "en"
    else:
        salutation = make_salutation(slug, merchant)
        langs = [str(x).lower() for x in (ident.get("languages") or [])]
        language = "hi-en" if "hi" in langs else "en"

    entities = set(c.entities)
    entities |= {x for x in (ident.get("name"), ident.get("owner_first_name"), ident.get("city"), ident.get("locality"),
                             category.get("display_name")) if x}
    if customer:
        entities |= {x for x in [((customer.get("identity") or {}).get("name"))] if x}
    entities |= set(c.active_offers())
    consent_note = ""
    if customer_facing:
        cons = (customer or {}).get("consent") or {}
        scopes = [x for x in (cons.get("scope") or []) if isinstance(x, str)]
        covers = not pb.consent or bool(set(pb.consent) & set(scopes))
        if scopes:
            consent_note = f"customer opted in ({', '.join(scopes)})" + ("" if covers else
                                                                          "; this reminder type is not named in that scope, sent under the lenient consent policy")
        elif cons.get("opted_in_at"):
            consent_note = "customer has an opt-in on record (no scope listed)"
    return FactSheet(
        facts=c.facts, salutation=salutation, language=language,  # type: ignore[arg-type]
        send_as="merchant_on_behalf" if customer_facing else "vera", category_slug=slug,
        voice=category.get("voice") or {}, allowed_entities=entities, active_offers=c.active_offers(),
        kind=trigger.kind, merchant_name=ident.get("name", ""), slots=c.slots, consent_note=consent_note,
    )
