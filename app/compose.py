"""Orchestrates Resolve -> Write -> Verify -> Repair -> Fallback for one decision.

Phase 1: the writer is the deterministic fallback composer. Phase 4 puts the LLM writer in front of it.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fallback import N_VARIANTS, compose_fallback
from .humanize import strip_end, words
from .models import FactSheet
from .normalize import Trigger
from .playbooks import Playbook, get_playbook
from .verifier import verify


@dataclass
class Composed:
    body: str
    cta: str
    facts_used: list[str]
    rationale: str
    template_params: list[str]
    via: str                       # "fallback" | "llm"
    violations: list[str]


def build_rationale(t: Trigger, pb: Playbook, fs: FactSheet, cta: str) -> str:
    hook = fs.text("hook") or ""
    why = pb.why_now or "new information arrived"
    return (f"{words(t.kind)} trigger ({why}; urgency {t.urgency}/5); leading with “{strip_end(hook)}”; "
            f"objective: {pb.objective}; CTA: {cta}.")


def compose_message(t: Trigger, fs: FactSheet, recent_bodies: list[str]) -> Composed:
    pb = get_playbook(t.kind)
    seed = t.id
    best, best_v = None, None
    attempts = [(None, False)] + [(i, False) for i in range(N_VARIANTS)] + [(None, True)]
    for variant, minimal in attempts:
        out = compose_fallback(fs, pb, seed, variant=variant, minimal=minimal)
        v = verify(out, fs, recent_bodies)
        if best is None or len(v) < len(best_v or []):
            best, best_v = out, v
        if not v:
            break
    assert best is not None
    hook = fs.text("hook") or ""
    ask = best["body"].rsplit(". ", 1)[-1] if ". " in best["body"] else best["body"]
    return Composed(
        body=best["body"], cta=best["cta"], facts_used=best["facts_used"],
        rationale=build_rationale(t, pb, fs, best["cta"]),
        template_params=[fs.salutation, strip_end(hook), ask],
        via="fallback", violations=best_v or [],
    )
