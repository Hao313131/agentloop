"""Prompt-injection heuristics.

Two places use this:
- ingestion: knowledge chunks that try to instruct the model are flagged and never
  reach the prompt (indirect injection, the more dangerous kind for RAG);
- user input: suspicious messages are annotated in the trace. They are not blocked,
  because the real defence is that tools enforce permissions server-side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(
        r"\b(?:ignore|disregard|forget|skip)\s+(?:all\s+)?(?:of\s+)?(?:the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|system)?\s*(?:instructions?|prompts?|rules|directions?|settings?)\b",
        re.I)),
    ("role_hijack", re.compile(
        r"\b(?:you are now|from now on you are|act as|pretend to be|enter)\b.{0,24}"
        r"\b(?:admin|administrator|developer|system|unrestricted)\b", re.I)),
    ("system_prompt_probe", re.compile(
        r"\b(?:system\s*prompt|your\s+(?:instructions?|prompt|rules)|print your (?:instructions?|prompt)|"
        r"reveal your (?:instructions?|prompt))\b", re.I)),
    ("bypass_approval", re.compile(
        r"\b(?:no|without|skip|bypass|don'?t need)\s+(?:a\s+)?(?:human\s+)?"
        r"(?:approval|review|confirmation|verification)\b", re.I)),
    ("direct_tool_order", re.compile(
        r"\b(?:call|run|execute|fire|just)\b.{0,20}\b(?:refund|refund tool|request_refund)\b", re.I)),
    ("fake_system_tag", re.compile(r"(?:\[system\]|<\s*system\s*>|###\s*system|SYSTEM NOTICE:)", re.I)),
]


@dataclass
class InjectionReport:
    suspicious: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionReport:
    matches = [name for name, pattern in _PATTERNS if pattern.search(text)]
    return InjectionReport(bool(matches), matches)
