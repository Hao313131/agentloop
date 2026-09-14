"""PII masking for prompts and traces (Australian formats).

Digits glued to letters are skipped, so order numbers such as SO20261008002 survive.
People type numbers the way they read them: "0412 345 678", "0412-345-678",
"+61 412 345 678", "(02) 9876 5432", "2123 456 789" (Medicare), "123 456 789" (TFN),
so a single space or hyphen is allowed between digit groups. Over-masking a long digit run
is the cheaper mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_RULES: list[tuple[str, re.Pattern[str]]] = [
    # A card number is checked first: its 13-19 digits would otherwise be half-eaten by the
    # Medicare rule ("4567 1234 5678" looks like one). A Medicare number is only 10 digits, so
    # this order cannot mask one as a card.
    ("bank_card", re.compile(r"(?<![A-Za-z\d])\d(?:[ -]?\d){12,18}(?![A-Za-z\d])")),
    # Medicare: 10 digits, first digit 2-6. Checked before the 9-digit TFN so a Medicare
    # number is not half-masked as a tax file number.
    ("medicare", re.compile(r"(?<![A-Za-z\d])[2-6]\d{3}[ -]?\d{3}[ -]?\d{3}(?![A-Za-z\d])")),
    # Mobile 04xx xxx xxx (or +61 4xx xxx xxx) and landline 0[2378] xxxx xxxx. Before the TFN:
    # "+61 412 345 678" is nine digits too, and the country code says it is a phone.
    ("phone", re.compile(
        r"(?<![A-Za-z\d+])(?:(?:\+|00)61[ -]?|0)4\d{2}[ -]?\d{3}[ -]?\d{3}(?![A-Za-z\d])"
        r"|(?<![A-Za-z\d+])(?:(?:\+|00)61[ -]?[2378]|\(0[2378]\)[ -]?|0[2378][ -]?)\d{4}[ -]?\d{4}(?![A-Za-z\d])"
    )),
    ("tfn", re.compile(r"(?<![A-Za-z\d])\d{3}[ -]?\d{3}[ -]?\d{3}(?![A-Za-z\d])")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]


@dataclass
class PIIReport:
    masked: str
    found: list[str] = field(default_factory=list)


def _mask(kind: str, value: str) -> str:
    if kind == "email":
        name, _, domain = value.partition("@")
        return f"{name[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if kind == "phone":
        digits = digits[-9:]  # drop the country code and the trunk 0
        return f"{digits[:2]} *** {digits[-3:]}"
    return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"


def mask_pii(text: str) -> PIIReport:
    found: list[str] = []
    for kind, pattern in _RULES:
        def replace(match: re.Match[str], kind: str = kind) -> str:
            found.append(kind)
            return _mask(kind, match.group(0))

        text = pattern.sub(replace, text)
    return PIIReport(text, found)
