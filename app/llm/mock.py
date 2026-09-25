"""Deterministic mock writer for LLM_MODE=mock: same JSON shape as the LLM, built from the fact sheet.

It reuses the template composer so the whole system (and every test) runs with zero API calls.
"""
from __future__ import annotations

from ..fallback import compose_fallback
from ..models import FactSheet
from ..playbooks import Playbook


def mock_write(fs: FactSheet, pb: Playbook, seed: str) -> dict:
    return compose_fallback(fs, pb, seed)


def mock_reply(ctx: str, d: str) -> dict:
    return {"body": f"Happy to help. Where we are: {ctx}. Reply YES and I'll prepare {d}.", "cta": "binary_yes_stop",
            "facts_used": [], "rationale_note": ""}
