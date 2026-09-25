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
    plural, days_between, fmt_date, fmt_month_year, fmt_money, fmt_num, fmt_pct, fmt_time, first_sentence, humanize_signal,
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
    def add(self, key: str, text: str, source: str, entities: tuple = ()) -> None:
        text = text.strip()
        if not text or self.has(key):
            return
        self.facts.append(Fact(id=f"F{len(self.facts) + 1}", key=key, text=text,
                               atoms=extract_numbers(text), source=source))
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

    def collect_slots(self, raw: Any) -> None:
        for s in raw or []:
            if isinstance(s, dict) and s.get("label"):
                self.slots.append(str(s["label"]))
        if self.slots:
            self.add("t.slots", "Open slots: " + " or ".join(self.slots[:3]), "trigger.slots")


# ================================ trigger builders =============================================
Builder = Callable[[Ctx], bool]


def _digest_hook(c: Ctx, item: dict) -> None:
    src = item.get("source", "")
    word = _KIND_WORD.get(item.get("kind", ""), "item")
    title = strip_end(item.get("title", ""))
    c.add("hook", f"{src} carries a new {word}: {title}" if src else f"There is a new {word}: {title}",
          "category.digest", (src,))
    if item.get("summary"):
        c.add("t.summary", first_sentence(item["summary"]), "category.digest.summary")
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
    title = strip_end(item.get("title", ""))
    dl = c.dt("deadline_iso")
    days = c.days_to(dl)
    tail = f" — {plural(days, 'day')} to go" if days is not None else ""
    c.add("hook", f"{title} ({src}){tail}" if src else f"{title}{tail}", "category.digest", (src,))
    if item.get("summary"):
        c.add("t.summary", first_sentence(item["summary"]), "category.digest.summary")
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
    return f"It's been {plural(n, 'month')} since your last visit" if n else None


def _recall(c: Ctx) -> bool:
    service = c.p.get("service_due")
    due = f"your {words(service)} recall is due" if service else "it's time for your regular check-up"
    ago = _months_hook(c)
    c.add("hook", f"{ago}, and {due}" if ago else due.capitalize(), "trigger.recall")
    dd = c.dt("due_date")
    if dd:
        c.add("t.due", f"Your recall date is {fmt_date(dd)}", "trigger.due_date")
    c.collect_slots(c.p.get("available_slots"))
    return True


def _lapsed_soft(c: Ctx) -> bool:
    ago = _months_hook(c)
    last = parse_dt(((c.cust or {}).get("relationship") or {}).get("last_visit"))
    if ago:
        c.add("hook", ago + ", so a quick check-up is due", "customer.relationship")
    elif last:
        c.add("hook", f"Your last visit with us was on {fmt_date(last)}", "customer.relationship")
    else:
        c.add("hook", "We haven't seen you in a while", "customer.relationship")
    c.collect_slots(c.p.get("available_slots"))
    return True


def _lapsed_hard(c: Ctx) -> bool:
    d = c.p.get("days_since_last_visit")
    if not d:
        return False
    c.add("hook", f"It's been {plural(d, 'day')} since your last visit", "trigger.days_since_last_visit")
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
    days = c.days_to(wd)
    days_s = f" — {plural(days, 'day')} to go —" if days is not None else ""
    prog = _program(c.p.get("next_step_window_open")) if c.p.get("next_step_window_open") else "skin prep"
    c.add("hook", f"Your wedding is on {fmt_date(wd)}{days_s} and the {prog} window is open", "trigger.wedding_date")
    tc = c.dt("trial_completed")
    if tc:
        c.add("t.trial", f"Your bridal trial was on {fmt_date(tc)}", "trigger.trial_completed")
    return True


def _trial(c: Ctx) -> bool:
    td = c.dt("trial_date")
    c.add("hook", f"Thank you for coming to the trial session on {fmt_date(td)}" if td else "Thank you for coming to the trial session",
          "trigger.trial_date")
    c.collect_slots(c.p.get("next_session_options"))
    return True


def _refill(c: Ctx) -> bool:
    meds = c.p.get("molecule_list") or []
    out = c.dt("stock_runs_out_iso")
    what = f"Your {join_and([str(m) for m in meds])} refill is due" if meds else "Your regular refill is due"
    tail = f", and your current stock runs out around {fmt_date(out)}" if out else ""
    c.add("hook", what + tail, "trigger.refill")
    if c.p.get("delivery_address_saved"):
        c.add("t.delivery", "We have your delivery address saved, so we can send it over", "trigger.delivery_address_saved")
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
    c.add("hook", f"Your {name} {verb} {direction} {fmt_pct(delta)} over the last {_window(c.p.get('window', '7d'))}{tail}",
          "trigger.delta_pct")
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
    c.add("hook", f"Your {name} {verb} down {fmt_pct(delta)} over the last {_window(c.p.get('window', '7d'))}, "
                  f"which is an expected seasonal dip{season}", "trigger.is_expected_seasonal")
    c.add("t.reframe", "It's a normal seasonal pattern, not a problem with your profile", "trigger.is_expected_seasonal")
    return True


def _renewal(c: Ctx) -> bool:
    days, plan, amt = c.p.get("days_remaining"), c.p.get("plan"), c.p.get("renewal_amount")
    if days is None:
        return False
    tail = f", and renewal is {fmt_money(amt)}" if amt else ""
    c.add("hook", f"Your {plan + ' ' if plan else ''}plan has {plural(days, 'day')} left{tail}", "trigger.days_remaining")
    return True


def _winback(c: Ctx) -> bool:
    d = c.p.get("days_since_expiry")
    if d is None:
        return False
    dip = c.p.get("perf_dip_pct")
    tail = f", and your numbers are down {fmt_pct(dip)} since" if isinstance(dip, (int, float)) else ""
    c.add("hook", f"Your plan expired {plural(d, 'day')} ago{tail}", "trigger.days_since_expiry")
    n = c.p.get("lapsed_customers_added_since_expiry")
    if n:
        c.add("t.lapsed", f"{n} more customers have gone quiet in that time", "trigger.lapsed_customers_added_since_expiry")
    return True


def _dormant(c: Ctx) -> bool:
    d = c.p.get("days_since_last_merchant_message")
    if d is None:
        return False
    topic = f", when we talked about {words(c.p['last_topic'])}" if c.p.get("last_topic") else ""
    c.add("hook", f"It's been {plural(d, 'day')} since we last spoke{topic}", "trigger.days_since_last_merchant_message")
    return True


def _festival(c: Ctx) -> bool:
    fest, d = c.p.get("festival"), c.dt("date")
    if not fest or not d:
        return False
    days = c.days_to(d)
    tail = f" — {days} days away" if days and days > 1 else (" — tomorrow" if days == 1 else (" — today" if days == 0 else ""))
    c.add("hook", f"{fest} is on {fmt_date(d)}{tail}", "trigger.festival", (fest,))
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
    if c.p.get("is_weeknight") is True:
        c.add("t.advice", "It's a weeknight, so a match-night combo can pull in dine-in crowds", "trigger.is_weeknight")
    elif c.p.get("is_weeknight") is False:
        c.add("t.advice", "It's not a weeknight, so lean on delivery rather than a dine-in match promo", "trigger.is_weeknight")
    return True


def _review_theme(c: Ctx) -> bool:
    theme, n = c.p.get("theme"), c.p.get("occurrences_30d")
    if not theme or not n:
        return False
    trend = f", and it's {c.p['trend']}" if c.p.get("trend") else ""
    c.add("hook", f"{plural(n, 'review')} in the last 30 days mention {_THEMES.get(theme, words(theme))}{trend}", "trigger.theme")
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
          else f"You've crossed {fmt_num(goal)} {name}", "trigger.milestone")
    avg = c.peer.get("avg_review_count") or c.peer.get("avg_reviews")
    if avg:
        c.add("t.peer", f"That's already ahead of the peer average of {fmt_num(avg)}" if now_v > avg
              else f"The peer average is {fmt_num(avg)}", "category.peer_stats")
    return True


def _planning(c: Ctx) -> bool:
    topic = c.p.get("intent_topic")
    if not topic:
        return False
    c.add("hook", f"You asked about your {words(topic)} idea, so here's a starter outline to edit", "trigger.intent_topic")
    c.add("t.draft", "Outline: who it's for, what's included, how you price it against your current offers, and the announcement "
                     "message. Nothing goes out without your go-ahead", "playbook")
    return True


def _curious(c: Ctx) -> bool:
    c.add("hook", f"Time for our regular check-in on what's in demand at {c.ident.get('name', 'your place')}",
          "trigger.curious_ask", (c.ident.get("name"),))
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
    c.add("hook", f"A new competitor, {name}, opened{where}{on}{offer}" if name else f"A new competitor opened{where}{on}{offer}",
          "trigger.competitor", (name, c.p.get("their_offer")))
    return True


def _gbp(c: Ctx) -> bool:
    verified = c.p.get("verified", c.ident.get("verified"))
    if verified is not False:
        return False
    path = f" — it can be done by {words(c.p['verification_path'])}" if c.p.get("verification_path") else ""
    c.add("hook", f"Your Google Business Profile is not verified yet{path}", "trigger.verified")
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
    "seasonal_perf_dip": ("cat.season",),
    "renewal_due": ("m.subscription",),
    "winback_eligible": ("m.expired",),
    "dormant_with_vera": ("m.dormant",),
    "review_theme_emerged": ("m.review",),
    "festival_upcoming": ("cat.season",),
    "category_seasonal": ("cat.season", "cat.trend"),
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
_GENERIC_ORDER = ("m.week", "m.views_peer", "m.calls_peer", "m.ctr", "m.lapsed", "m.retention", "m.perf30", "cat.trend", "cat.season")


def _fallback_hook(c: Ctx) -> None:
    def promote(fact: Fact) -> None:
        c.facts.remove(fact)
        c.facts.insert(0, replace(fact, key="hook"))

    for key in _FALLBACK_KEYS.get(c.t.kind, ()):
        f = next((x for x in c.facts if x.key == key), None)
        if f:
            promote(f)
            return
    stmt = _KIND_STATEMENT.get(c.t.kind)
    if stmt:
        c.facts.insert(0, Fact(id="", key="hook", text=stmt, atoms=set(), source="trigger.kind"))
        return
    for key in _GENERIC_ORDER:
        f = next((x for x in c.facts if x.key == key), None)
        if f:
            promote(f)
            return
    c.facts.insert(0, Fact(id="", key="hook", text="Here's a quick update on your profile", atoms=set(), source="merchant"))


# ================================ merchant / customer facts ===================================
def _merchant_facts(c: Ctx) -> None:
    m, perf, cust_agg = c.m, c.perf, c.m.get("customer_aggregate") or {}
    parts = []
    if perf.get("views") is not None:
        parts.append(f"{fmt_num(perf['views'])} views")
    if perf.get("calls") is not None:
        parts.append(f"{fmt_num(perf['calls'])} calls")
    if perf.get("directions") is not None:
        parts.append(f"{fmt_num(perf['directions'])} direction requests")
    if parts:
        c.add("m.perf30", f"In the last {perf.get('window_days', 30)} days your profile had {join_and(parts)}", "merchant.performance")
    ctr, peer_ctr = perf.get("ctr"), c.peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
        c.add("m.ctr", f"Your click-through rate is {fmt_pct(ctr)} against a peer average of {fmt_pct(peer_ctr)}", "merchant.performance.ctr")
    elif isinstance(ctr, (int, float)):
        c.add("m.ctr", f"Your click-through rate is {fmt_pct(ctr)}", "merchant.performance.ctr")
    offers = c.active_offers()
    if offers:
        c.add("m.offers", f"Your active offer is {offers[0]}" if len(offers) == 1 else f"Your active offers are {join_and(offers)}",
              "merchant.offers", tuple(offers))
        c.add("m.offer_price", f"{offers[0]} is on right now", "merchant.offers", (offers[0],))
    if isinstance(cust_agg.get("retention_6mo_pct"), (int, float)):
        pr = c.peer.get("retention_6mo_pct")
        tail = f" against {fmt_pct(pr)} for peers" if isinstance(pr, (int, float)) else ""
        c.add("m.retention", f"Your 6-month retention is {fmt_pct(cust_agg['retention_6mo_pct'])}{tail}", "merchant.customer_aggregate")
    elif isinstance(cust_agg.get("retention_3mo_pct"), (int, float)):
        c.add("m.retention", f"Your 3-month retention is {fmt_pct(cust_agg['retention_3mo_pct'])}", "merchant.customer_aggregate")
    if cust_agg.get("high_risk_adult_count"):
        c.add("m.cohort", f"You have {fmt_num(cust_agg['high_risk_adult_count'])} high-risk adult patients on your roster",
              "merchant.customer_aggregate")
    for k, days in (("lapsed_180d_plus", 180), ("lapsed_90d_plus", 90)):
        if cust_agg.get(k):
            c.add("m.lapsed", f"{fmt_num(cust_agg[k])} of your customers haven't visited in over {days} days", "merchant.customer_aggregate")
            break
    win = perf.get("window_days", 30)
    for key, name, peer_key in (("views", "views", "avg_views_30d"), ("calls", "calls", "avg_calls_30d")):
        pv = c.peer.get(peer_key)
        if isinstance(perf.get(key), (int, float)) and isinstance(pv, (int, float)):
            c.add(f"m.{key}_peer", f"Your {win}-day {name} are {fmt_num(perf[key])} against a peer average of {fmt_num(pv)}",
                  f"merchant.performance.{key}")
    for key, peer_key in (("views", "avg_views_30d"), ("calls", "avg_calls_30d")):
        pv = c.peer.get(peer_key)
        fact = c.get(f"m.{key}_peer")
        if fact and isinstance(perf.get(key), (int, float)) and isinstance(pv, (int, float)) and perf[key] != pv:
            c.add(f"m.{key}_{'below' if perf[key] < pv else 'above'}", fact.text, f"merchant.performance.{key}")
    d7 = perf.get("delta_7d") or {}
    for key, name in (("views_pct", "views"), ("calls_pct", "calls")):
        v = d7.get(key)
        if isinstance(v, (int, float)) and v != 0:
            text = f"This week your {name} are {'up' if v > 0 else 'down'} {fmt_pct(v)}"
            c.add("m.week", text, "merchant.performance.delta_7d")
            c.add("m.week_up" if v > 0 else "m.week_down", text, "merchant.performance.delta_7d")
            break
    hist = [h for h in (m.get("conversation_history") or []) if isinstance(h, dict)]
    last_ts = parse_dt(hist[-1].get("ts")) if hist else None
    quiet = (c.now - last_ts).days if last_ts else None
    if quiet is None:
        for sig in m.get("signals") or []:
            mm = re.match(r"dormant_with_vera_(\d+)d$", str(sig))
            if mm:
                quiet = int(mm.group(1))
    if quiet and quiet >= 1:
        c.add("m.dormant", f"It's been {plural(quiet, 'day')} since we last spoke", "merchant.conversation_history")
    sub = m.get("subscription") or {}
    if sub.get("status") == "active" and sub.get("days_remaining") is not None:
        c.add("m.subscription", f"Your {sub.get('plan', '')} plan has {sub['days_remaining']} days left".replace("  ", " "), "merchant.subscription")
    elif sub.get("status") == "expired" and sub.get("days_since_expiry"):
        text = f"Your plan expired {plural(sub['days_since_expiry'], 'day')} ago"
        c.add("m.subscription", text, "merchant.subscription")
        c.add("m.expired", text, "merchant.subscription")
    for th in m.get("review_themes") or []:
        if isinstance(th, dict) and th.get("theme") and th.get("occurrences_30d"):
            verb = "praise" if th.get("sentiment") == "pos" else "mention"
            name = _THEMES.get(th["theme"], words(th["theme"]))
            c.add("m.review", f"{th['occurrences_30d']} recent reviews {verb} {name}", "merchant.review_themes")
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
            c.add("cat.season", f"Seasonal pattern for {beat['month_range']}: {strip_end(beat['note'])}", "category.seasonal_beats")
            break
    trends = [t for t in (c.cat.get("trend_signals") or []) if isinstance(t, dict) and isinstance(t.get("delta_yoy"), (int, float))]
    if trends:
        top = max(trends, key=lambda t: abs(t["delta_yoy"]))
        c.add("cat.trend", f"Searches for “{top.get('query')}” are {'up' if top['delta_yoy'] > 0 else 'down'} "
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
    if n:
        c.add("c.months", f"It's been {n} months since your last visit", "customer.relationship.last_visit")
    last = parse_dt(rel.get("last_visit"))
    if last:
        c.add("c.lastvisit", f"Your last visit with us was on {fmt_date(last)}", "customer.relationship.last_visit")
    first = parse_dt(rel.get("first_visit"))
    if first:
        c.add("c.since", f"You've been with us since {fmt_month_year(first)}", "customer.relationship.first_visit")
    if rel.get("visits_total"):
        c.add("c.visits", f"You've visited {plural(rel['visits_total'], 'time')} so far", "customer.relationship.visits_total")
    if rel.get("favourite_dish"):
        c.add("c.fav", f"Your favourite with us is {rel['favourite_dish']}", "customer.relationship.favourite_dish", (rel["favourite_dish"],))
    slots_pref = (cu.get("preferences") or {}).get("preferred_slots")
    if slots_pref:
        c.add("c.pref", f"You prefer {words(slots_pref)} slots", "customer.preferences.preferred_slots")
    seen: list[str] = []
    for svc in rel.get("services_received") or []:
        w = words(svc)
        if w and w != "..." and w not in seen:
            seen.append(w)
    if seen:
        c.add("c.services", f"You've had {join_and(seen[:3])} with us before", "customer.relationship.services_received")
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
    return FactSheet(
        facts=c.facts, salutation=salutation, language=language,  # type: ignore[arg-type]
        send_as="merchant_on_behalf" if customer_facing else "vera", category_slug=slug,
        voice=category.get("voice") or {}, allowed_entities=entities, active_offers=c.active_offers(),
        kind=trigger.kind, merchant_name=ident.get("name", ""), slots=c.slots,
    )
