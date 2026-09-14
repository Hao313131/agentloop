"""Text helpers shared by retrieval, the evidence verifier and the offline model.

Tokenisation is a word regex plus a light suffix normaliser, so "refund", "refunds" and
"refunded" collapse to one token and BM25 and the IDF-weighted coverage gate compare words
rather than surface forms. It is deliberately dependency-free: the platform ships no NLP
library beyond numpy.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’]*|\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[A-Za-z0-9]")
_SENT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_NUM_RE = re.compile(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![\d])")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3})")
CITE_RE = re.compile(r"\[(\d{1,2})\]")
# Order numbers: "SO" + 8-14 digits. No word boundary, because a customer may type the number
# straight after a word ("orderSO20261008002"); the look-arounds only keep the digits from
# running on, so a longer number is not truncated into a valid one.
ORDER_ID_RE = re.compile(r"(?<!\d)SO\d{8,14}(?!\d)", re.IGNORECASE)

# A customer asking for a person ("transfer me to a human", "I want to speak to someone") is
# honoured without the model: it cannot be talked out of it, and it still works when the model
# service is down. Negated mentions ("don't transfer me") are cut out first.
_NEG = (r"(?:don'?t|do not|not|no need to)\s+(?:\w+\s+){0,2}?"
        r"(?:transfer|connect|escalate|put|hand|speak|talk)\s+(?:me\s+)?(?:over\s+)?(?:to\s+)?")
NEGATED_PERSON_RE = re.compile(
    rf"{_NEG}(?:a\s+|an\s+|the\s+)?(?:human|person|agent|staff|someone|representative|operator)"
    r"|(?:this\s+)?(?:is\s+)?not\s+a\s+complaint"
    r"|(?:no|not)\s+a\s+complaint",
    re.IGNORECASE,
)
_PERSON = r"(?:human|real person|live agent|human agent|person|staff|representative|operator)"
_EXPLICIT_PERSON_RE = re.compile(
    rf"(?:speak|talk|connect|transfer|escalate)\s+(?:me\s+)?(?:to\s+)?(?:a\s+|an\s+|the\s+)?{_PERSON}"
    rf"|put\s+me\s+(?:through\s+)?(?:to\s+)?(?:a\s+)?{_PERSON}"
    rf"|(?:i\s+(?:want|need|would\s+like|wish)\s+to\s+(?:speak|talk)\s+to\s+(?:someone|somebody|a\s+person|a\s+human))"
    rf"|(?:i\s+(?:want|wish)\s+to\s+(?:lodge\s+a\s+)?complain|i'?m\s+complaining)"
    rf"|^(?:a\s+)?(?:human|person|agent|someone|somebody)(?:\s+please)?$"
)


def asks_for_person(text: str) -> bool:
    """True for an explicit request to talk to a person, which is honoured without the model."""
    cleaned = NEGATED_PERSON_RE.sub(" ", normalize_input(text))
    cleaned = re.sub(r"[^\w\s']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return bool(cleaned) and bool(_EXPLICIT_PERSON_RE.search(cleaned))


# Typographic characters pasted from a word processor, a chat app or a PDF become ASCII, so
# "SO20261008002" typed inside smart quotes, or an en dash in "1-3 days", still matches.
_TYPOGRAPHIC = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "\u00a0": " ",
})


def normalize_input(text: str) -> str:
    return text.translate(_TYPOGRAPHIC)


# Function words that carry no topical signal. Kept to closed-class words only: a content word
# removed here can never be matched, and the relevance gate would then score the wrong thing.
STOP_TOKENS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "as", "of", "to", "in", "on",
    "at", "by", "for", "from", "with", "about", "into", "over", "under", "up", "out", "off",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "done",
    "have", "has", "had", "will", "would", "shall", "should", "can", "could", "may", "might",
    "must", "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "yours",
    "he", "she", "it", "its", "they", "them", "their", "theirs", "this", "that", "these",
    "those", "there", "here", "what", "which", "who", "whom", "whose", "when", "where", "why",
    "how", "any", "some", "all", "each", "no", "not", "yes", "please", "thanks", "thank",
    "hi", "hello", "hey", "just", "also", "too", "very", "much", "many", "more", "most",
    "get", "got", "want", "wanted", "need", "needed", "like", "know", "tell", "say", "says",
    "still", "already", "again", "only", "even", "now", "today", "okay", "ok",
}


def _normalise_word(word: str) -> str:
    """Lower-case, drop possessives, and fold the plural forms that matter for matching.

    Applied to queries and documents alike, so a stem that is not a real word is still
    consistent on both sides. Words ending in "ss" are left alone ("address" must not become
    "addres", which would then differ from "addresses" -> "address").
    """
    if word.endswith("'s") or word.endswith("’s"):
        word = word[:-2]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Tokens for BM25 and overlap scoring: lower-cased, normalised, stop words dropped."""
    if len(text) > _CACHE_MAX_CHARS:
        return list(_tokenize(text))
    return list(_tokenize_cached(text))


# One answer tokenises the same knowledge-base sentences several times (retrieval, answer
# extraction, the verifier), and tokenising is the largest CPU cost per request. It is a pure
# function of the text, so it is memoised; long texts (whole documents at ingest) are not, to
# bound memory.
_CACHE_MAX_CHARS = 2000


def _tokenize(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = _normalise_word(match.group(0).lower())
        if not token or not _WORD_RE.search(token) or token in STOP_TOKENS:
            continue
        tokens.append(token)
    return tuple(tokens)


_tokenize_cached = lru_cache(maxsize=4096)(_tokenize)


# Query normalisation for how customers actually type. Colloquial phrases are *expanded* with
# the knowledge base's own wording (never replaced, so recall can only grow), and the relevance
# gate scores the *replaced* form. Kept small and domain-specific on purpose: every entry
# should be justified by an eval case ("colloquial" tag) or a real bad case from the flywheel.
QUERY_SYNONYMS: dict[str, str] = {
    # money back and returns
    "money back": "refund",
    "send it back": "return",
    "send them back": "return",
    "send back": "return",
    "change my mind": "change of mind",
    "changed my mind": "change of mind",
    # postage, carriers and tracking
    "postage": "shipping fee",
    "postage cost": "shipping fee",
    "shipping cost": "shipping fee",
    "delivery fee": "shipping fee",
    "freight": "shipping fee",
    "postie": "carrier",
    "courier": "carrier",
    "auspost": "australia post",
    "track and trace": "tracking",
    "track my parcel": "tracking",
    "where is my parcel": "tracking",
    "where's my order": "tracking",
    "where is my order": "tracking",
    # invoices and tax
    "receipt": "tax invoice",
    "invoice": "tax invoice",
    # product quality
    "broken": "faulty",
    "damaged": "faulty",
    "dud": "faulty",
    "dead on arrival": "faulty",
    "stopped working": "faulty",
    "not working": "faulty",
    "packed it in": "faulty",
    "dodgy": "faulty",
    "wonky": "faulty",
    # membership
    "loyalty": "membership points",
    "rewards": "membership points",
    # delivery reach
    "overseas": "internationally",
    "abroad": "internationally",
    # the SaaS tenant
    "can't log in": "login issue",
    "cannot log in": "login issue",
    "not responding": "outage",
    "went down": "outage",
}
_SYNONYM_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in sorted(QUERY_SYNONYMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def expand_query(text: str) -> str:
    """"where's my parcel?" -> "where's my parcel? tracking"."""
    folded = normalize_input(text)
    extras = list(dict.fromkeys(
        QUERY_SYNONYMS[m.group(0).lower()] for m in _SYNONYM_RE.finditer(folded)))
    return f"{folded} {' '.join(extras)}" if extras else folded


def canonical_query(text: str) -> str:
    """The question in the knowledge base's own words: colloquial phrases are *replaced*.

    Used by the relevance gate. There, the colloquial words themselves are out of the
    knowledge base's vocabulary, get the highest IDF, and would drag coverage under the
    threshold even though the canonical words match: "postage to NZ?" -> "shipping fee to NZ"."""
    return _SYNONYM_RE.sub(lambda m: f" {QUERY_SYNONYMS[m.group(0).lower()]} ", normalize_input(text))


def token_set(text: str) -> set[str]:
    return set(tokenize(text))


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text)]
    return [p for p in parts if p]


def normalize_numbers(text: str) -> str:
    return _THOUSANDS_RE.sub("", text)


def extract_numbers(text: str) -> list[float]:
    """Numbers that make a factual claim (amounts, days, hours, dates).
    Digits glued to letters (order IDs such as SO20261008002) are identifiers, not numbers."""
    return [float(m.group(0)) for m in _NUM_RE.finditer(normalize_numbers(text))]


def strip_citations(text: str) -> tuple[str, list[int]]:
    refs = [int(r) for r in CITE_RE.findall(text)]
    return CITE_RE.sub("", text).strip(), refs


def overlap_recall(claim: str, evidence: str) -> float:
    """Share of the claim's tokens that also appear in the evidence."""
    claim_tokens = token_set(claim)
    if not claim_tokens:
        return 1.0
    return len(claim_tokens & token_set(evidence)) / len(claim_tokens)


def estimate_tokens(text: str) -> int:
    """Rough token estimate used when a provider does not report usage (about 4 chars/token)."""
    if not text:
        return 0
    return math.ceil(len(text) / 4)
