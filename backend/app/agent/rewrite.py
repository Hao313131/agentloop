"""Follow-up rewriting for multi-turn conversations.

"What are the conditions for a change-of-mind return?" → "and who pays the return postage?":
searched on its own, the follow-up retrieves the generic shipping-fee rule (free over $99)
instead of who pays for return shipping. The runtime turns a detected follow-up into a
standalone question before the agent sees it; the conversation keeps what the customer
actually typed, the trace records both.

Detection is deliberately explicit (a leading "what about" / "how about" / "and" / "also" /
"then", or a trailing "then?"): "short question" alone is a bad signal, because "how many
points do I get?" is short and complete. With a real model the rewrite is one cheap fast-tier
call; the offline model, or any failure, falls back to a rule: the previous question's topic
plus the follow-up.

Chains: the reference is the previous turn's *standalone* question (kept in the user
message's meta), not what was typed, so "what about a faulty item?" after "who pays the return
postage?" still knows the topic is returns. An additive opener followed by a complete question
("also, can I get a tax invoice?") starts a new topic and is only stripped of its opener.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.base import LLMMessage, LLMProvider
from ..textutil import ORDER_ID_RE

_LEADING = ("what about", "how about", "and what about", "and how about", "and", "also", "then",
            "so", "ok and", "okay and", "what if", "but what about")
_QUESTION_WORDS = re.compile(
    r"\b(?:what|who|when|where|why|how|which|is|are|was|were|do|does|did|can|could|would|should|will|the|a|an)\b",
    re.IGNORECASE,
)
_LEADING_RE = re.compile(
    r"^(?:(?:and|but)\s+)?(?:what|how)\s+about\s+|^(?:and|but|also|then|so|ok|okay|what if)[,\s]+",
    re.IGNORECASE,
)
_ADDITIVE = ("also", "and also", "by the way", "one more thing", "another thing", "plus")
_OWN_QUESTION = re.compile(
    r"\b(?:what|who|when|where|why|how|which|do|does|did|can|could|would|should|will|is|are)\b",
    re.IGNORECASE,
)
_END_PUNCT = " ?.!,"

REWRITE_PROMPT = """You rewrite conversations. Using the history, turn the customer's last follow-up into a
standalone, complete question that does not depend on the context.
Output only the rewritten question, do not answer it, at most 40 words.

Conversation history:
{history}

Follow-up: {question}

If the follow-up is already a complete new question (for example "also, can I get a tax invoice?"),
drop the opening connector and output it unchanged instead of merging it with the context."""


@dataclass
class Rewrite:
    standalone: str
    method: str  # llm | rule


@dataclass
class Frame:
    """What the conversation is about (topic) and what was last asked about it (predicate).

    "What are the conditions for a change-of-mind return?" -> topic and predicate "conditions
    for a change-of-mind return"; then "and who pays the return postage?" keeps the topic and
    asks something new (predicate "who pays the return postage"); then "what about a faulty
    item?" swaps the topic and keeps the question: "a faulty item, who pays the return
    postage"."""

    topic: str
    predicate: str


def is_followup(text: str, previous_user_question: str | None) -> bool:
    if not previous_user_question:
        return False
    stripped = text.strip()
    if ORDER_ID_RE.search(stripped):  # an order number makes the question self-contained
        return False
    lowered = stripped.lower()
    if lowered.startswith(_LEADING):
        return True
    return lowered.rstrip(" ?.").endswith("then")


def _topic(question: str) -> str:
    """A question minus its opener and question words: "what are the conditions for a
    change-of-mind return?" -> "conditions for change-of-mind return"."""
    cleaned = _LEADING_RE.sub("", question.strip())
    cleaned = _QUESTION_WORDS.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ?.!,;:")


def frame_of(question: str) -> Frame:
    """The frame a self-contained question opens."""
    topic = _topic(question)
    return Frame(topic, topic)


def _kind(text: str) -> str:
    """new: a complete question after also / by the way; subject: a bare "what about X?";
    predicate: anything else."""
    stripped = text.strip()
    rest = _LEADING_RE.sub("", stripped).rstrip(_END_PUNCT)
    if stripped.lower().startswith(_ADDITIVE) and _OWN_QUESTION.search(rest):
        return "new"
    if re.match(r"^(?:(?:and|but)\s+)?(?:what|how)\s+about\b", stripped, re.IGNORECASE) \
            and not _OWN_QUESTION.search(rest):
        return "subject"
    return "predicate"


def needs_context(text: str) -> bool:
    """"and who pays the return postage?" leans on the previous question; "also, can I get a
    tax invoice?" does not."""
    return _kind(text) != "new"


def _focus(text: str) -> str:
    return _LEADING_RE.sub("", text.strip()).rstrip(_END_PUNCT).strip()


def rule_rewrite(text: str, previous: str | Frame) -> str:
    frame = previous if isinstance(previous, Frame) else frame_of(previous)
    kind = _kind(text)
    if kind == "new":
        return _LEADING_RE.sub("", text.strip())
    if kind == "subject":
        return f"{_focus(text)}, {frame.predicate}"
    return f"{frame.topic}, {_LEADING_RE.sub('', text.strip())}"


def next_frame(previous: str | Frame, text: str, standalone: str) -> Frame:
    """The frame after this follow-up, for the one after it."""
    frame = previous if isinstance(previous, Frame) else frame_of(previous)
    kind = _kind(text)
    if kind == "new":
        return frame_of(standalone)
    if kind == "subject":
        return Frame(_focus(text), frame.predicate)
    return Frame(frame.topic, _topic(text))


def rewrite_followup(provider: LLMProvider, model: str, history: list[LLMMessage], text: str,
                     previous: str | Frame, use_model: bool = True,
                     deadline: float | None = None) -> tuple[Rewrite, int, int]:
    """Returns the rewrite plus (prompt_tokens, completion_tokens) spent on it. ``deadline`` (a
    monotonic time) caps the model call; without the model the rule rewrite is used."""
    if use_model and provider.name != "mock":
        transcript = "\n".join(f"{'Customer' if m.role == 'user' else 'Agent'}: {m.content}" for m in history[-4:])
        try:
            result = provider.chat([LLMMessage(role="user", content=REWRITE_PROMPT.format(history=transcript, question=text))],
                                   [], model, {"task": "rewrite", "temperature": 0,
                                               **({"deadline": deadline} if deadline is not None else {})})
            standalone = result.content.strip().splitlines()[0][:160] if result.content.strip() else ""
            if standalone:
                return Rewrite(standalone, "llm"), result.usage.prompt_tokens, result.usage.completion_tokens
        except Exception:  # rewriting is an optimisation; never fail the turn because of it
            pass
    return Rewrite(rule_rewrite(text, previous), "rule"), 0, 0
