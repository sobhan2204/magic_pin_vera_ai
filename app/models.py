"""Response models (validated at the HTTP boundary) and internal dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict

CTA_TYPES = {
    "binary_yes_stop", "open_ended", "none",
    "multi_choice_slot", "binary_yes_no", "binary_confirm_cancel",
}


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: Literal["vera", "merchant_on_behalf"]
    trigger_id: str
    template_name: str
    template_params: list[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


class ReplyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["send", "wait", "end"]
    body: Optional[str] = None
    cta: Optional[str] = None
    wait_seconds: Optional[int] = None
    rationale: str

    def to_wire(self) -> dict:
        d = self.model_dump(exclude_none=True)
        if d["action"] == "send" and not (d.get("body") or "").strip():
            raise ValueError("send with empty body")
        return d


@dataclass
class Fact:
    id: str                      # "F1", "F2", ...
    key: str                     # stable role, e.g. "hook", "m.ctr", "t.slots"
    text: str                    # plain English, merchant-readable
    atoms: set[str] = field(default_factory=set)   # normalized numbers this fact licenses
    source: str = ""             # which context field it came from


@dataclass
class FactSheet:
    facts: list[Fact]
    salutation: str
    language: Literal["en", "hi-en"]
    send_as: Literal["vera", "merchant_on_behalf"]
    category_slug: str
    voice: dict
    allowed_entities: set[str]
    active_offers: list[str]
    kind: str
    merchant_name: str = ""
    slots: list[str] = field(default_factory=list)

    def get(self, key: str) -> Optional[Fact]:
        for f in self.facts:
            if f.key == key:
                return f
        return None

    def text(self, key: str) -> Optional[str]:
        f = self.get(key)
        return f.text if f else None

    def ids(self) -> set[str]:
        return {f.id for f in self.facts}


@dataclass
class Decision:
    action: Literal["send", "no_op"]
    trigger_id: str
    merchant_id: str
    customer_id: Optional[str]
    objective: str = ""
    hook_fact_id: str = ""
    facts_allowed: list[str] = field(default_factory=list)
    cta_type: str = "open_ended"
    reason: str = ""
    score: float = 0.0
    kind: str = ""


JSON = dict[str, Any]
