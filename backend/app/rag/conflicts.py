"""Knowledge conflict detection at ingestion time.

When two documents state the same rule with different numbers ("Refunds are paid within
1-3 business days" in the policy, "1-2 business days" in a later patch), retrieval may
surface either one and the model has no way to know which is current. Conflicts are
therefore caught when a document is written, not when a customer gets the wrong answer.

Heuristic, deliberately conservative:
1. Split both documents into sentences and keep the ones that contain a number with a
   unit (business days, hours, days, $, %, points ...): these are the checkable claims.
2. Two claims from different documents are "about the same thing" when they share at
   least 2 content tokens, the overlap covers >= 60% of the shorter claim, and the
   shorter claim has at most one token of its own (numbers and units are removed first).
   The last rule keeps "card refunds take 3-7 days" and "PayPal refunds take 1-3 days"
   apart: same wording, different subject.
3. They conflict when their values differ in the same dimension. Durations are one
   dimension: minutes, hours, days and weeks are converted (24 hours = 1 day), business
   days are not (a business day is not 24 hours), so "3 days" against "1-3 business days"
   is flagged for a person.
Detected conflicts are stored for an administrator to resolve. Nothing is blocked at
ingestion (the flywheel blocks a patch that contradicts what the corrected answer cited),
but while a conflict is open, a question whose evidence holds both sides goes to a person
(see ``disputed``): the evidence check passes each of two contradicting sentences on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document, KnowledgeConflict
from ..textutil import split_sentences, token_set

_UNIT_WORDS = r"business\s+days?|working\s+days?|hours?|hrs?|minutes?|mins?|days?|weeks?|months?|points?|GB|TB|%"
_UNIT_END = r"(?!\w)"  # "%" is not a word character, so \b would not match after it
_RANGE = r"\d[\d,]*(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d[\d,]*(?:\.\d+)?)?"
_CLAIM_RE = re.compile(rf"({_RANGE})\s*({_UNIT_WORDS}){_UNIT_END}", re.IGNORECASE)
_MONEY_RE = re.compile(rf"\$\s?({_RANGE})")
_NUM_UNIT_RE = re.compile(rf"\$?\s?{_RANGE}\s*(?:{_UNIT_WORDS})?{_UNIT_END}", re.IGNORECASE)
MIN_SHARED_TOKENS = 2
MIN_OVERLAP = 0.6
MAX_OWN_TOKENS = 1

# Convertible durations, in hours. A business day is deliberately absent: 1 business day is
# not 24 hours, so it never compares equal to a plain "day" figure.
_HOURS = {"minute": 1 / 60, "min": 1 / 60, "hour": 1.0, "hr": 1.0, "day": 24.0, "week": 168.0, "month": 720.0}
_CANONICAL = {
    "business day": "business day", "business days": "business day",
    "working day": "business day", "working days": "business day",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "minute": "minute", "minutes": "minute", "min": "minute", "mins": "minute",
    "day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
    "point": "points", "points": "points", "%": "%", "gb": "GB", "tb": "TB",
}
FLYWHEEL = "flywheel"  # KnowledgeConflict.origin of a conflict a reviewer published a correction over


@dataclass
class Claim:
    chunk_id: int
    document_id: int
    sentence: str
    values: dict[str, set[str]]  # unit -> normalised values
    tokens: set[str]


def _normalise_value(raw: str) -> str:
    """Ranges compare as "1-3"; thousands separators and trailing zeros are dropped, so
    "$1,299.00" and "$1299" are the same claim."""
    raw = re.sub(r"\s*(?:–|to)\s*", "-", raw.replace(" ", ""))
    parts = []
    for part in raw.split("-"):
        part = part.replace(",", "")
        try:
            number = float(part)
        except ValueError:
            parts.append(part)
            continue
        parts.append(f"{number:g}")
    return "-".join(parts)


def extract_claims(chunk: Chunk) -> list[Claim]:
    return claims_in_text(chunk.text, chunk.id, chunk.document_id)


def claims_in_text(text: str, chunk_id: int = 0, document_id: int = 0) -> list[Claim]:
    claims = []
    for raw in split_sentences(text):
        sentence = raw.lstrip("-*• ").strip()
        values: dict[str, set[str]] = {}
        for number, unit in _CLAIM_RE.findall(sentence):
            values.setdefault(_CANONICAL.get(unit.lower(), unit.lower()), set()).add(_normalise_value(number))
        for number in _MONEY_RE.findall(sentence):
            values.setdefault("$", set()).add(_normalise_value(number))
        if not values:
            continue
        tokens = token_set(_NUM_UNIT_RE.sub(" ", sentence))
        if len(tokens) >= MIN_SHARED_TOKENS:
            claims.append(Claim(chunk_id, document_id, sentence, values, tokens))
    return claims


def _same_topic(a: Claim, b: Claim) -> bool:
    shared = a.tokens & b.tokens
    shorter, longer = (a, b) if len(a.tokens) <= len(b.tokens) else (b, a)
    return (
        len(shared) >= MIN_SHARED_TOKENS
        and len(shared) / len(shorter.tokens) >= MIN_OVERLAP
        and len(shorter.tokens - longer.tokens) <= MAX_OWN_TOKENS
    )


def _dimension(unit: str) -> str:
    return "duration" if unit in _HOURS or unit == "business day" else unit


def _hours(value: str, unit: str) -> str:
    def fmt(x: float) -> str:
        return f"{x:g}"
    return "-".join(fmt(float(part) * _HOURS[unit]) for part in value.split("-")) + " hours"


def comparable_values(claim: Claim) -> dict[str, set[str]]:
    """Values per dimension, in a form that compares across units where that is meaningful."""
    out: dict[str, set[str]] = {}
    for unit, values in claim.values.items():
        for value in values:
            shown = _hours(value, unit) if unit in _HOURS else f"{value} {unit}"
            out.setdefault(_dimension(unit), set()).add(shown)
    return out


def conflicting_dimensions(a: Claim, b: Claim) -> dict[str, dict[str, list[str]]]:
    """Dimensions in which two claims state different values."""
    va, vb = comparable_values(a), comparable_values(b)
    return {dim: {"this": sorted(va[dim]), "other": sorted(vb[dim])}
            for dim in va.keys() & vb.keys() if va[dim] != vb[dim]}


def detect_conflicts(db: Session, document: Document) -> int:
    """Recompute open conflicts between ``document`` and every other document of the tenant.

    A conflict a reviewer published a correction over came from comparing the correction with
    what the corrected answer cited, which wording alone cannot reproduce. It stays open while
    the document still contains its sentence and closes itself when an edit removes it."""
    chunks = db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars().all()
    # Pairs are (document, sentence) on both sides: the same sentence in another document is
    # another conflict, even if this pair was resolved.
    seen: set[tuple[int, str, int, str]] = set()

    def remember(c: KnowledgeConflict) -> None:
        seen.update({(c.document_id, c.sentence, c.other_document_id, c.other_sentence),
                     (c.other_document_id, c.other_sentence, c.document_id, c.sentence)})

    for conflict in db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.status == "open",
        or_(KnowledgeConflict.document_id == document.id, KnowledgeConflict.other_document_id == document.id),
    )).scalars().all():
        if conflict.origin != FLYWHEEL:
            db.delete(conflict)
            continue
        own = conflict.sentence if conflict.document_id == document.id else conflict.other_sentence
        if any(own in chunk.text for chunk in chunks):
            remember(conflict)
        else:
            conflict.status, conflict.note = "resolved", "Document edited; the conflicting sentence is gone"
            conflict.resolved_at = datetime.now(timezone.utc)
    # A pair an administrator resolved stays resolved while both sentences read the same; a
    # changed sentence is a new claim and is checked again.
    for conflict in db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.status == "resolved",
        or_(KnowledgeConflict.document_id == document.id, KnowledgeConflict.other_document_id == document.id),
    )).scalars():
        remember(conflict)
    mine = [c for chunk in chunks if not chunk.flagged_injection for c in extract_claims(chunk)]
    if not mine:
        db.commit()
        return 0
    others = [c for chunk in db.execute(
        select(Chunk).where(Chunk.tenant_id == document.tenant_id, Chunk.document_id != document.id,
                            Chunk.flagged_injection.is_(False))
    ).scalars() for c in extract_claims(chunk)]
    found = 0
    for a in mine:
        for b in others:
            pair = (a.document_id, a.sentence, b.document_id, b.sentence)
            if pair in seen or not _same_topic(a, b):
                continue
            detail = conflicting_dimensions(a, b)
            if not detail:
                continue
            seen.add(pair)
            db.add(KnowledgeConflict(
                tenant_id=document.tenant_id, document_id=a.document_id, chunk_id=a.chunk_id,
                other_document_id=b.document_id, other_chunk_id=b.chunk_id,
                sentence=a.sentence, other_sentence=b.sentence, detail=detail,
            ))
            found += 1
    db.commit()
    return found


def record_published_anyway(db: Session, document: Document, found: list[dict], note: str) -> None:
    """Keep the conflicts a reviewer published a correction over on the open list (``found`` as
    returned by flywheel.service.patch_conflicts): until one side is edited or the conflict is
    marked resolved, answers resting on both sides go to a person."""
    listed = {frozenset({(c.document_id, c.sentence), (c.other_document_id, c.other_sentence)})
              for c in db.execute(select(KnowledgeConflict).where(
                  KnowledgeConflict.tenant_id == document.tenant_id, KnowledgeConflict.status == "open")).scalars()}
    chunks = db.execute(select(Chunk).where(Chunk.document_id == document.id)).scalars().all()
    for item in found:
        pair = frozenset({(document.id, item["patch_sentence"]), (item["document_id"], item["existing_sentence"])})
        if pair in listed:  # detection on ingestion has already listed it
            continue
        listed.add(pair)
        db.add(KnowledgeConflict(
            tenant_id=document.tenant_id, document_id=document.id,
            chunk_id=next((c.id for c in chunks if item["patch_sentence"] in c.text), 0),
            other_document_id=item["document_id"], other_chunk_id=item["chunk_id"],
            sentence=item["patch_sentence"], other_sentence=item["existing_sentence"],
            detail=item["detail"], origin=FLYWHEEL, note=note,
        ))
    db.commit()


def disputed(db: Session, tenant_id: int, texts: list[str]) -> list[KnowledgeConflict]:
    """Open conflicts with both sides among ``texts``, the knowledge one answer would rest on.
    Matched by sentence rather than chunk id, so re-indexing a document does not hide them."""
    if not texts:
        return []
    rows = db.execute(select(KnowledgeConflict).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).scalars()
    return [c for c in rows
            if any(c.sentence in text for text in texts) and any(c.other_sentence in text for text in texts)]


def open_conflict_counts(db: Session, tenant_id: int) -> dict[int, int]:
    counts: dict[int, int] = {}
    rows = db.execute(select(KnowledgeConflict.document_id, KnowledgeConflict.other_document_id).where(
        KnowledgeConflict.tenant_id == tenant_id, KnowledgeConflict.status == "open")).all()
    for doc_a, doc_b in rows:
        for doc in (doc_a, doc_b):
            counts[doc] = counts.get(doc, 0) + 1
    return counts
