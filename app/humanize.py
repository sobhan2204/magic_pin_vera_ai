"""Plain-English rendering helpers. No raw field names or signal codes may reach a message."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_dt(s: Any) -> Optional[datetime]:
    """Parse ISO date/datetime into an aware datetime (naive input is treated as UTC)."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_num(n: Any) -> str:
    """Indian digit grouping: 2410 -> 2,410 ; 120000 -> 1,20,000."""
    try:
        v = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    s, neg = str(abs(v)), v < 0
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s


def fmt_money(n: Any) -> str:
    return "₹" + fmt_num(n)


def fmt_pct(fraction: Any) -> str:
    """0.5 -> '50%', 0.021 -> '2.1%', 0.030 -> '3%'. Sign is dropped; callers say up/down."""
    try:
        v = abs(float(fraction)) * 100
    except (TypeError, ValueError):
        return str(fraction)
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v))}%"
    return f"{v:.1f}%"


def fmt_date(dt: Optional[datetime]) -> str:
    return f"{dt.day} {_MONTHS[dt.month - 1]}" if dt else ""


def fmt_month_year(dt: Optional[datetime]) -> str:
    return f"{_MONTHS[dt.month - 1]} {dt.year}" if dt else ""


def fmt_time(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def months_between(earlier: Optional[datetime], later: Optional[datetime]) -> Optional[int]:
    if not earlier or not later:
        return None
    days = (later - earlier).days
    return days // 30 if days >= 30 else None


def days_between(earlier: Optional[datetime], later: Optional[datetime]) -> Optional[int]:
    if not earlier or not later:
        return None
    return (earlier - later).days


def words(code: Any) -> str:
    """snake_case / kebab -> plain words."""
    s = re.sub(r"[_\-]+", " ", str(code)).strip()
    s = re.sub(r"\bhigh risk\b", "high-risk", s)
    return re.sub(r"(\d+) (month|day|week|year)\b", r"\1-\2", s)


def plural(n: Any, word: str) -> str:
    """plural(1,'day') -> '1 day'; plural(5,'day') -> '5 days'."""
    return f"{n} {word}" if str(n) in ("1", "1.0") else f"{n} {word}s"


_MONTHS_FULL = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def beautify_dates(text: str) -> str:
    """'effective 2026-12-15' -> 'effective 15 Dec 2026' (data titles carry raw ISO dates)."""
    def one(m: "re.Match") -> str:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{d} {_MONTHS_FULL[mo - 1]} {y}" if 1 <= mo <= 12 and 1 <= d <= 31 else m.group(0)
    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", one, text or "")


def cap_days(text: str) -> str:
    return re.sub(r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b", lambda m: m.group(0).capitalize(), text)


def join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def first_sentence(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])", text)
    return (text[: m.start()] if m else text).rstrip(".")


def strip_end(text: str) -> str:
    return (text or "").strip().rstrip(".!?;:, ")


# --- merchant signals -> plain English -------------------------------------------------
_SIGNALS = {
    "ctr_below_peer_median": "your click-through rate is below the peer median",
    "above_peer_median_calls": "your calls are above the peer median",
    "growing_views_7d": "your views are growing this week",
    "perf_dip_severe": "your numbers have dipped sharply",
    "unverified_gbp": "your Google profile is not verified yet",
    "no_active_offers": "you have no active offer live",
    "dormant_with_vera_14d": "we haven't spoken in a couple of weeks",
    "high_engagement": "customers are engaging well with your profile",
}


def humanize_signal(sig: str) -> Optional[str]:
    if sig in _SIGNALS:
        return _SIGNALS[sig]
    m = re.match(r"stale_posts:(\d+)d$", sig)
    if m:
        return f"your last Google post was {m.group(1)} days ago"
    m = re.match(r"renewal_due_soon:(\d+)d$", sig)
    if m:
        return f"your plan renews in {m.group(1)} days"
    return None


def humanize_trend(token: str) -> str:
    """'ORS_demand_+40' -> 'ORS demand up 40%'; 'cold_cough_demand_-60' -> 'cold cough demand down 60%'."""
    m = re.match(r"^(.*?)_?demand_([+-]?)(\d+)$", str(token))
    if not m:
        return words(token)
    name = words(m.group(1)).strip() or "overall"
    direction = "down" if m.group(2) == "-" else "up"
    return f"{name} demand {direction} {m.group(3)}%"
