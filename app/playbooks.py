"""Per-trigger-kind playbooks: objective, levers, CTA, consent scopes and the template fallback pieces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Playbook:
    kind: str
    objective: str
    scope: str = "merchant"                    # merchant | customer
    levers: tuple[str, ...] = ()
    cta_type: str = "open_ended"
    tone_notes: str = ""
    support_keys: tuple[str, ...] = ()         # ordered fact keys for the supporting sentence(s)
    support_n: int = 1
    ask_en: str = "Want me to look into this and suggest a next step?"
    ask_hi: str = "Kya main isko dekh ke aapko ek next step suggest kar doon?"
    slot_ask_en: Optional[str] = None          # format string with {s1} {s2}
    slot_ask_hi: Optional[str] = None
    consent: tuple[str, ...] = ()              # any-of scopes for customer triggers; empty = any active consent
    needs_digest: bool = False
    deliverable: str = "the draft"             # what we hand over when the merchant says yes
    why_now: str = ""
    statement_support: Optional[tuple[str, ...]] = None   # support keys to use when the hook is only a kind-level statement
    stmt_ask_en: Optional[str] = None                     # ask to use in that case (we do not know the specifics)
    stmt_ask_hi: Optional[str] = None


def _pb(**kw) -> Playbook:
    return Playbook(**kw)


_ALL = [
    _pb(kind="research_digest", objective="Share one merchant-relevant research item and offer to draft patient-facing content",
        levers=("curiosity", "reciprocity", "specificity"), needs_digest=True,
        support_keys=("t.summary", "m.cohort"), support_n=2,
        ask_en="Want me to pull the abstract and draft a patient-ed WhatsApp you can share?",
        ask_hi="Kya aap chahenge ki main abstract nikaal ke ek patient-ed WhatsApp draft kar doon?",
        deliverable="the abstract and a patient-ready WhatsApp draft", why_now="new digest item this week"),
    _pb(kind="regulation_change", objective="Flag a compliance change with its deadline and offer a checklist",
        levers=("loss_aversion", "specificity"), needs_digest=True, support_keys=("t.summary",),
        ask_en="Want me to draft a short compliance checklist for this?",
        ask_hi="Kya main iske liye ek chhota compliance checklist draft kar doon?",
        deliverable="a short compliance checklist", why_now="compliance deadline approaching"),
    _pb(kind="recall_due", scope="customer", objective="Bring a lapsed customer back with real slots and the active offer price",
        levers=("specificity", "effort_externalization"), cta_type="multi_choice_slot",
        support_keys=("t.slots", "m.offer_price", "c.services", "c.lastvisit"), support_n=3,
        ask_en="Reply YES and we'll share the earliest slots.",
        ask_hi="Aap Reply YES kar dijiye, hum sabse pehle wale slots bhej denge.",
        slot_ask_en="Reply 1 for {s1}, 2 for {s2}, or suggest a time that suits you.",
        slot_ask_hi="Aap chahein toh Reply 1 for {s1}, 2 for {s2}, ya apna time bata dijiye.",
        consent=("recall_reminders", "recall_alerts", "appointment_reminders"),
        deliverable="your booking", why_now="recall window is open"),
    _pb(kind="customer_lapsed_soft", scope="customer", objective="Nudge a recently lapsed customer to rebook",
        levers=("specificity", "effort_externalization"), cta_type="binary_yes_stop",
        support_keys=("t.slots", "c.visits", "m.offer_price", "c.since"), support_n=2,
        ask_en="Reply YES and we'll share the earliest slots.",
        ask_hi="Aap Reply YES kar dijiye, hum sabse pehle wale slots bhej denge.",
        consent=("recall_reminders", "promotional_offers", "winback_offers", "renewal_reminders", "recall_alerts"),
        deliverable="your booking", why_now="customer has gone quiet"),
    _pb(kind="customer_lapsed_hard", scope="customer", objective="Win back a long-lapsed customer without pressure",
        levers=("reciprocity", "relationship"), cta_type="binary_yes_stop", support_keys=("t.focus", "c.services", "m.offer_price", "c.visits"),
        ask_en="Reply YES and we'll help you restart at your own pace.",
        ask_hi="Aap Reply YES kar dijiye, hum aapko apne pace se restart karne mein help karenge.",
        consent=("winback_offers", "renewal_reminders", "promotional_offers", "recall_reminders"),
        deliverable="your restart plan", why_now="customer lapsed for a long time"),
    _pb(kind="appointment_tomorrow", scope="customer", objective="Confirm tomorrow's appointment and reduce no-shows",
        levers=("effort_externalization",), cta_type="binary_yes_stop", support_keys=("c.stylist", "c.services"),
        ask_en="Reply YES to confirm, or tell us if you need a different time.",
        ask_hi="Aap Reply YES kar dijiye confirm ke liye, ya koi aur time bata dijiye.",
        consent=("appointment_reminders",), deliverable="your confirmation", why_now="appointment is tomorrow"),
    _pb(kind="wedding_package_followup", scope="customer", objective="Convert a completed bridal trial into the prep program",
        levers=("specificity", "urgency", "relationship"), support_keys=("t.trial", "c.pref", "m.offer_price"), support_n=2,
        cta_type="binary_yes_stop",
        ask_en="Want me to block a slot for your first session?",
        ask_hi="Kya main aapke pehle session ke liye ek slot block kar doon?",
        consent=("bridal_package_followup",), deliverable="your slot booking", why_now="prep window is open"),
    _pb(kind="trial_followup", scope="customer", objective="Book the next session after a trial",
        levers=("relationship", "effort_externalization"), cta_type="multi_choice_slot",
        support_keys=("m.offer_price",),
        ask_en="Reply YES and we'll share the next session options.",
        ask_hi="Aap Reply YES kar dijiye, hum agle session ke options bhej denge.",
        slot_ask_en="Reply 1 to book {s1}, or tell us another time.",
        slot_ask_hi="Aap chahein toh Reply 1 kar dijiye {s1} ke liye, ya koi aur time bata dijiye.",
        consent=("kids_program_updates", "program_updates", "appointment_reminders"),
        deliverable="your session booking", why_now="trial just completed"),
    _pb(kind="chronic_refill_due", scope="customer", objective="Prompt a timely refill before stock runs out",
        levers=("loss_aversion", "effort_externalization"), cta_type="binary_yes_stop", support_keys=("t.delivery",),
        ask_en="Reply YES and we'll send the refill over.", ask_hi="Aap Reply YES kar dijiye, hum refill bhej denge.",
        consent=("refill_reminders", "recall_alerts"), deliverable="your refill order", why_now="stock is about to run out"),
    _pb(kind="perf_dip", objective="Name the drop precisely and offer a diagnosis and one fix",
        levers=("loss_aversion", "specificity"), support_keys=("m.ctr", "m.offers"),
        ask_en="Want me to check what changed and suggest one fix?",
        ask_hi="Kya main dekh loon ki kya badla hai aur ek fix suggest kar doon?",
        deliverable="a diagnosis and one concrete fix", why_now="sharp week-over-week drop"),
    _pb(kind="perf_spike", objective="Reinforce what worked while momentum is up",
        levers=("reciprocity", "specificity"), support_keys=("t.driver", "m.ctr"),
        ask_en="Want me to draft a follow-up post to keep this going?",
        ask_hi="Kya main isko chalte rakhne ke liye ek follow-up post draft kar doon?",
        deliverable="a follow-up post draft", why_now="numbers spiked this week"),
    _pb(kind="seasonal_perf_dip", objective="Reframe an expected seasonal dip and suggest a low-cost response",
        levers=("reassurance", "specificity"), support_keys=("t.reframe", "m.offers"),
        ask_en="Want me to plan a low-cost offer to keep walk-ins steady?",
        ask_hi="Kya main walk-ins ko steady rakhne ke liye ek low-cost offer plan kar doon?",
        deliverable="a low-cost offer plan", why_now="seasonal dip in progress"),
    _pb(kind="renewal_due", objective="Renew before the plan lapses, anchored on recent results",
        levers=("loss_aversion", "specificity"), cta_type="binary_yes_stop", support_keys=("m.perf30",),
        ask_en="Reply YES and I'll set up your renewal.", ask_hi="Aap Reply YES kar dijiye, main renewal set kar doon.",
        deliverable="your renewal", why_now="plan is close to expiry"),
    _pb(kind="winback_eligible", objective="Restart a lapsed subscription by showing what it cost them",
        levers=("loss_aversion", "specificity"), cta_type="binary_yes_stop", support_keys=("t.lapsed", "m.perf30"),
        ask_en="Reply YES and I'll set up your restart.", ask_hi="Aap Reply YES kar dijiye, main aapka restart set kar doon.",
        deliverable="your restart", why_now="plan lapsed and numbers are slipping"),
    _pb(kind="dormant_with_vera", objective="Re-open a quiet conversation with something new and specific",
        levers=("reciprocity", "curiosity"), support_keys=("m.perf30",),
        ask_en="Want a quick update on what has changed on your profile?",
        ask_hi="Kya aap chahenge ki main profile mein kya badla hai uska ek quick update bhej doon?",
        deliverable="a quick profile update", why_now="no conversation for weeks"),
    _pb(kind="festival_upcoming", objective="Prepare a festival push around an existing offer",
        levers=("urgency", "effort_externalization"), support_keys=("m.offers",),
        ask_en="Want me to draft a festive post around one of your offers?",
        ask_hi="Kya main aapke offer ke saath ek festive post draft kar doon?",
        deliverable="a festive post draft", why_now="festival is coming up"),
    _pb(kind="category_seasonal", objective="Adjust shelf/offer mix to a seasonal demand shift",
        levers=("specificity", "loss_aversion"), support_keys=("t.action",),
        ask_en="Want me to draft a shelf plan and a WhatsApp broadcast?",
        ask_hi="Kya main ek shelf plan aur WhatsApp broadcast draft kar doon?",
        deliverable="a shelf plan and broadcast draft", why_now="seasonal demand shift"),
    _pb(kind="ipl_match_today", objective="Give match-day advice that fits the day of the week",
        levers=("specificity", "counter_intuitive_advice"), support_keys=("t.advice", "m.offers"), support_n=2,
        ask_en="Want me to draft the promo message for the match?",
        ask_hi="Kya main match ke liye promo message draft kar doon?",
        deliverable="the promo message draft", why_now="match is on today"),
    _pb(kind="review_theme_emerged", objective="Surface a recurring review complaint and offer a public reply plus fix",
        levers=("loss_aversion", "specificity"), support_keys=("t.quote",), statement_support=(),
        ask_en="Want me to draft a public reply and a short fix checklist?",
        ask_hi="Kya main ek public reply aur chhota fix checklist draft kar doon?",
        stmt_ask_en="Want me to pull the main themes from your recent reviews and draft a public reply?",
        stmt_ask_hi="Kya main aapke recent reviews ke main themes nikaal ke ek public reply draft kar doon?",
        deliverable="a public reply and fix checklist", why_now="a review theme is rising"),
    _pb(kind="milestone_reached", objective="Celebrate a near milestone and help the merchant cross it",
        levers=("social_proof", "effort_externalization"), support_keys=("t.peer",), statement_support=(),
        ask_en="Want me to draft a short note you can hand customers to get you there?",
        ask_hi="Kya main ek chhota note draft kar doon jo aap customers ko de sakein?",
        stmt_ask_en="Want me to check exactly how close you are and draft a short note for customers?",
        stmt_ask_hi="Kya main dekh loon ki aap kitne kareeb hain aur customers ke liye ek chhota note draft kar doon?",
        deliverable="a short customer note", why_now="milestone is within reach"),
    _pb(kind="active_planning_intent", objective="Merchant already said yes: deliver a concrete first draft now",
        levers=("effort_externalization",), cta_type="binary_yes_stop", support_keys=("t.draft", "m.offers"), support_n=2,
        ask_en="Reply CONFIRM and I'll expand this into the full draft with the announcement text, for your approval.",
        ask_hi="Aap Reply CONFIRM kar dijiye, main ise announcement text ke saath poora draft bana ke aapke approval ke liye bhej doon.",
        deliverable="the full draft", why_now="merchant asked for this"),
    _pb(kind="curious_ask_due", objective="Ask the merchant one low-stakes question and promise something back",
        levers=("asking_the_merchant", "reciprocity"), support_keys=("m.offers", "t.reciprocity"), support_n=2,
        ask_en="Which service have customers asked about most this week?",
        ask_hi="Is hafte customers ne sabse zyada kis service ke baare mein poochha?",
        deliverable="a Google post and a short WhatsApp reply", why_now="weekly check-in"),
    _pb(kind="competitor_opened", objective="Make the merchant aware of a nearby competitor and offer a retention move",
        levers=("loss_aversion", "curiosity"), support_keys=("m.offers", "m.retention"),
        ask_en="Want me to draft a retention message for your existing customers?",
        ask_hi="Kya main aapke existing customers ke liye ek retention message draft kar doon?",
        deliverable="a retention message draft", why_now="competitor just opened nearby"),
    _pb(kind="gbp_unverified", objective="Get the Google profile verified",
        levers=("loss_aversion", "effort_externalization"), support_keys=("t.uplift",),
        ask_en="Want me to walk you through the verification steps now?",
        ask_hi="Kya main aapko verification ke steps abhi samjha doon?",
        deliverable="the verification steps", why_now="profile is still unverified"),
    _pb(kind="cde_opportunity", objective="Invite the merchant to a relevant free/cheap learning event",
        levers=("specificity", "effort_externalization"), cta_type="binary_yes_stop", needs_digest=True,
        support_keys=("t.fee",),
        ask_en="Reply YES and I'll save your seat.", ask_hi="Aap Reply YES kar dijiye, main aapki seat save kar doon.",
        deliverable="your registration", why_now="session is coming up"),
    _pb(kind="supply_alert", objective="Alert the pharmacy to a batch recall and offer a customer notice",
        levers=("loss_aversion", "urgency"), support_keys=("t.action",),
        ask_en="Want me to draft a notice for customers who bought these batches?",
        ask_hi="Kya main in batches ke customers ke liye ek notice draft kar doon?",
        deliverable="a customer notice draft", why_now="recall notice is live"),
    _pb(kind="generic", objective="Lead with the most specific fact available and offer a next step",
        levers=("specificity",), support_keys=("m.perf30",), why_now="new information arrived"),
]

PLAYBOOKS: dict[str, Playbook] = {p.kind: p for p in _ALL}
GENERIC = PLAYBOOKS["generic"]


def get_playbook(kind: str) -> Playbook:
    return PLAYBOOKS.get(kind, GENERIC)
