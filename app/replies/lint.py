"""Action-mode lint: after a merchant commits, the reply must DO the next step, not qualify again."""
from __future__ import annotations

REQUIRED_ANY = ("done", "here's", "here is", "sending", "drafted", "draft", "confirm", "next", "live", "scheduled", "booked")
BANNED = ("would you", "do you", "can you tell", "what if", "how about", "are you sure", "just to confirm whether")


def lint_action_body(body: str) -> list[str]:
    low = (body or "").lower().replace("’", "'")
    problems = []
    if not any(w in low for w in REQUIRED_ANY):
        problems.append("no action word (done/here's/sending/draft/confirm/next/live/scheduled/booked)")
    problems += [f"qualifying phrase {b!r}" for b in BANNED if b in low]
    if low.count("?") > 0:
        problems.append("action-mode reply must not end in a question")
    return problems


SAFE_ACTION_BODY = "Done, moving ahead. Here's the next step: I'll send the draft here for your approval. Reply CONFIRM and I'll get it moving."
