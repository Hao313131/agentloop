"""Model routing: send easy questions to the fast tier, risky or complex ones to the
strong tier, and fall back to the other tier when a call fails."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Settings
from ..textutil import ORDER_ID_RE

# Escalation language is always high stakes; a refund is high stakes only when it is an
# action on a concrete order ("SO... I want a refund"), not a policy question ("how long do
# refunds take").
_ESCALATION = re.compile(
    r"\b(?:compensation|sue|lawsuit|lawyer|legal action|ombudsman|accc|fair trading|consumer "
    r"affairs|police|go to the media|expose)\b", re.IGNORECASE)
_MONEY_ACTION = re.compile(r"\b(?:refund|reimburse|money back|return)\w*", re.IGNORECASE)
_INTENTS = [
    re.compile(r"\b(?:refund|return|money back)\w*", re.IGNORECASE),
    re.compile(r"\b(?:shipping|delivery|dispatch|parcel|track)\w*", re.IGNORECASE),
    re.compile(r"\b(?:invoice|tax invoice|receipt)\b", re.IGNORECASE),
    re.compile(r"\b(?:points|membership|rewards)\b", re.IGNORECASE),
    re.compile(r"\b(?:broken|faulty|repair|not working|stopped working)\b", re.IGNORECASE),
]


@dataclass
class RouteDecision:
    tier: str
    model: str
    reasons: list[str]


class ModelRouter:
    def __init__(self, settings: Settings):
        self.fast = settings.llm_model_fast
        self.strong = settings.llm_model_strong

    def route(self, text: str, history_turns: int = 0) -> RouteDecision:
        reasons: list[str] = []
        if len(text) > 80:
            reasons.append("long question")
        if sum(1 for p in _INTENTS if p.search(text)) >= 2:
            reasons.append("several intents")
        if _ESCALATION.search(text) or (_MONEY_ACTION.search(text) and ORDER_ID_RE.search(text)):
            reasons.append("high-risk intent")
        if history_turns >= 4:
            reasons.append("multi-turn context")
        if reasons:
            return RouteDecision("strong", self.strong, reasons)
        return RouteDecision("fast", self.fast, ["simple question"])

    def candidates(self, decision: RouteDecision) -> list[str]:
        """Primary model first, then the other tier as fallback."""
        other = self.fast if decision.model == self.strong else self.strong
        return [decision.model] if other == decision.model else [decision.model, other]
