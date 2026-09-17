"""Deterministic offline model.

It implements the same tool-calling contract as a real LLM, so the whole platform
(agent loop, approvals, verifier, evals, UI) runs and is testable without an API key.
Behaviour is rule-based and reproducible: the same input always yields the same
trajectory, which is exactly what CI regression tests need.

It is NOT a stand-in for model quality. Numbers measured with it validate the pipeline,
not answer quality; re-run the eval suite with a real model for that.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from ..textutil import NEGATED_PERSON_RE, ORDER_ID_RE, estimate_tokens, expand_query, split_sentences, token_set
from .base import ChatResult, LLMMessage, LLMProvider, StreamEvent, ToolCall, ToolSpec, Usage

_HANDOFF = re.compile(r"\b(?:human|real person|live agent|human agent|complaint|complain|escalate)\b",
                      re.IGNORECASE)
# Negated requests ("don't transfer me") are cut out before matching, the way a real model reads
# them; otherwise the offline model (the demo path and the eval baseline) inflates the handoff rate.
_NEGATED_HANDOFF = NEGATED_PERSON_RE
_REFUND = re.compile(r"\b(?:refund|reimburse|money back|send (?:it|them|this) back|return)\w*", re.IGNORECASE)
_SHIPMENT = re.compile(r"\b(?:shipping|delivery|deliver|parcel|track|tracking|arrive|dispatch|posted|postage|"
                       r"where is my order|where's my order)\w*", re.IGNORECASE)
_TICKET = re.compile(r"\b(?:broken|faulty|not working|stopped working|error|bug|crash|repair|ticket|defect|"
                     r"won'?t (?:turn on|start|connect)|can'?t log in|cannot log in|login issue|outage|"
                     r"keeps? failing)\b", re.IGNORECASE)
_URGENT = re.compile(r"\b(?:urgent|asap|immediately|right away|emergency|as soon as possible)\b", re.IGNORECASE)
_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:dollars?|bucks?|aud)\b",
                      re.IGNORECASE)

HALLUCINATION = " Also, every order comes with a 90-day change-of-mind guarantee and refunds land within 2 hours."
REFUSAL = (
    "Sorry, I couldn't find anything in the knowledge base that directly covers that. "
    "To avoid giving you the wrong information, I'd suggest speaking with a human agent."
)


# "SO… I'd like a refund, and also how do I get a tax invoice?" carries two requests. The clause
# after a connector is answered on its own instead of being silently dropped.
_EXTRA = re.compile(r"[,.!?;\s]+(?:also|and also|by the way|another thing|one more thing|plus)[,:]?\s+",
                    re.IGNORECASE)
MULTI_REFUND_TEXT = "I can only file a refund for one order at a time — which order would you like to refund?"
REJECTED_NEXT_STEP = ("If you can add more evidence, you can file the refund again, or reply \"human\" and "
                      "an agent will take it further.")


def split_intents(text: str) -> tuple[str, str | None]:
    """(main request, an extra question or None)."""
    match = _EXTRA.search(text)
    if not match:
        return text, None
    extra = text[match.end():].strip()
    if len(extra) < 2 or ORDER_ID_RE.search(extra):
        return text, None
    return text[:match.start()].strip(), extra


def wants_human(text: str) -> bool:
    return bool(_HANDOFF.search(_NEGATED_HANDOFF.sub("", text)))


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"raw": text}
    return value if isinstance(value, dict) else {"value": value}


def _clean(sentence: str) -> str:
    return sentence.lstrip("-*• ").strip()


def _money(amount: float) -> str:
    return f"${amount:,.2f} AUD"


class MockLLM(LLMProvider):
    name = "mock"

    def __init__(self, stream_delay: float = 0.0):
        self.stream_delay = stream_delay

    # ------------------------------------------------------------------ public API
    def chat(self, messages: list[LLMMessage], tools: list[ToolSpec], model: str,
             options: dict[str, Any] | None = None) -> ChatResult:
        options = options or {}
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        if options.get("task") == "kb_patch":
            content = self._kb_patch(messages[-1].content)
            return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

        available = {t.name for t in tools}
        user_idx = max(i for i, m in enumerate(messages) if m.role == "user")
        user_text = messages[user_idx].content
        turn = messages[user_idx + 1 :]
        called = [c.name for m in turn if m.role == "assistant" for c in m.tool_calls]
        results: dict[str, list[dict[str, Any]]] = {}
        for m in turn:
            if m.role == "tool" and m.name:
                results.setdefault(m.name, []).append(_loads(m.content))

        call = self._next_call(user_text, called, results, available)
        if call is not None:
            name, args = call
            tool_call = ToolCall(id=f"mock_{len(called) + 1}_{name}", name=name, arguments=args)
            completion = estimate_tokens(json.dumps(args, ensure_ascii=False))
            return ChatResult("", [tool_call], Usage(prompt_tokens, completion, True), model, "tool_calls")

        content = self._compose(user_text, results)
        if options.get("inject_hallucination"):
            content += HALLUCINATION
        return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

    def stream(self, messages, tools, model, options=None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, model, options)
        text = result.content
        delay = 0.0 if (options or {}).get("dry_run") else self.stream_delay
        for start in range(0, len(text), 4):
            if delay:
                time.sleep(delay)
            yield StreamEvent(type="delta", text=text[start : start + 4])
        yield StreamEvent(type="done", result=result)

    # ------------------------------------------------------------------ planning
    def _next_call(self, text: str, called: list[str], results: dict, available: set[str]):
        main, extra = split_intents(text)
        call = self._main_call(main, called, results, available)
        if call is not None:
            return call
        searched = {r.get("query") for r in results.get("search_knowledge") or []}
        if extra and "search_knowledge" in available and extra not in searched:
            return "search_knowledge", {"query": extra}
        return None

    def _main_call(self, text: str, called: list[str], results: dict, available: set[str]):
        def can(name: str) -> bool:
            return name in available and name not in called

        order_ids = list(dict.fromkeys(m.upper() for m in ORDER_ID_RE.findall(text)))
        if wants_human(text) and can("handoff_to_human"):
            return "handoff_to_human", {"reason": text[:120]}

        if len(order_ids) > 1:  # look every order up; track each if asked; never guess which to refund
            orders = results.get("get_order") or []
            if len(orders) < len(order_ids) and "get_order" in available:
                return "get_order", {"order_id": order_ids[len(orders)]}
            found = [o["order_id"] for o in orders if not o.get("error")]
            tracked = results.get("track_shipment") or []
            if (_SHIPMENT.search(text) and not _REFUND.search(text) and "track_shipment" in available
                    and len(tracked) < len(found)):
                return "track_shipment", {"order_id": found[len(tracked)]}
            return None

        if order_ids:
            order_id = order_ids[0]
            if can("get_order"):
                return "get_order", {"order_id": order_id}
            order = (results.get("get_order") or [{}])[-1]
            if order.get("error"):
                return None
            if _REFUND.search(text):
                if not order.get("refundable", False):
                    return None
                if can("search_knowledge"):
                    return "search_knowledge", {"query": "refund payment business days"}
                if can("request_refund"):
                    amount_match = _AMOUNT.search(text)  # a gullible model takes the user's number
                    amount = float((amount_match.group(1) or amount_match.group(2)).replace(",", "")) \
                        if amount_match else float(order.get("amount", 0))
                    return "request_refund", {"order_id": order_id, "amount": amount, "reason": text[:120]}
                return None
            if _SHIPMENT.search(text) and can("track_shipment"):
                return "track_shipment", {"order_id": order_id}
            return None

        if _TICKET.search(text):
            if can("search_knowledge"):
                return "search_knowledge", {"query": text}
            if can("create_ticket"):
                priority = "high" if _URGENT.search(text) else "normal"
                return "create_ticket", {"title": text[:60], "description": text, "priority": priority}
            return None

        if not called and can("search_knowledge"):
            return "search_knowledge", {"query": text}
        return None

    # ------------------------------------------------------------------ answering
    def _compose(self, text: str, results: dict[str, list[dict[str, Any]]]) -> str:
        text, extra = split_intents(text)
        searches = results.get("search_knowledge") or []
        extra_search = next((r for r in searches if extra and r.get("query") == extra), None)
        results = {**results, "search_knowledge": [r for r in searches if r is not extra_search]}
        parts: list[str] = []

        def last(name: str) -> dict[str, Any] | None:
            return results[name][-1] if results.get(name) else None

        handoff = last("handoff_to_human")
        if handoff:
            parts.append(handoff.get("message") or handoff.get("error", ""))

        orders = results.get("get_order") or []
        refund = last("request_refund")
        shipments = results.get("track_shipment") or []
        tracked = {s.get("order_id") for s in shipments if not s.get("error")}
        for order in orders:
            if order.get("error"):
                parts.append(f"I couldn't look that order up: {order['error']}. Please check the number and try again.")
            elif not refund and order.get("order_id") not in tracked:
                items = ", ".join(f"{i['name']} x{i['qty']}" for i in order.get("items", []))
                parts.append(
                    f"Order {order['order_id']} is currently “{order['status_text']}”. "
                    f"Total {_money(order['amount'])}, items: {items}."
                )
                if len(orders) == 1 and _REFUND.search(text) and not order.get("refundable", False):
                    parts.append(f"This order can't be refunded right now: {order.get('refund_note', '')}.")
        if len(orders) > 1 and _REFUND.search(text):
            parts.append(MULTI_REFUND_TEXT)

        for shipment in shipments:
            if shipment.get("error"):
                parts.append(f"The tracking lookup failed: {shipment['error']}.")
            elif shipment.get("message"):
                parts.append(shipment["message"])

        # Only a refund that went through gets the payout timing; a rejected or failed one
        # gets its reason and a next step, never "arrives in 1-3 business days".
        refund_failed = bool(refund) and (bool(refund.get("error")) or refund.get("status") == "rejected")
        if refund:
            if refund.get("error"):
                parts.append(f"The refund request wasn't submitted: {refund['error']}.")
            elif refund.get("status") == "rejected":
                parts.append(refund.get("message", ""))
                parts.append(REJECTED_NEXT_STEP)
            else:
                parts.append(refund.get("message", ""))

        ticket = last("create_ticket")
        knowledge = last("search_knowledge")
        if knowledge:
            query = "refund payment business days" if refund and not refund_failed else expand_query(text)
            if not refund_failed:
                limit = 1 if (refund or ticket) else 3
                sentences = self._extract(query, knowledge.get("results") or [], limit)
                if sentences:
                    parts.extend(sentences)
                elif not parts and not ticket:
                    parts.append(REFUSAL)
        if ticket:
            parts.append(ticket.get("message") or f"The ticket could not be created: {ticket.get('error', '')}.")

        if extra_search is not None:
            sentences = self._extract(expand_query(extra), extra_search.get("results") or [], 2)
            parts.append(f"Also, {''.join(sentences)}" if sentences
                         else f"I couldn't find anything in the knowledge base about “{extra}”.")

        if not parts:
            parts.append(REFUSAL)
        return " ".join(p.strip() for p in parts if p and p.strip())

    @staticmethod
    def _extract(query: str, hits: list[dict[str, Any]], limit: int) -> list[str]:
        """Pick the sentences that best cover the query, only from strongly relevant hits."""
        query_tokens = token_set(query)
        if not hits or not query_tokens:
            return []
        top = max(float(h.get("relevance", 0)) for h in hits)
        scored: list[tuple[float, int, int, str]] = []
        for rank, hit in enumerate(hits):
            if float(hit.get("relevance", 0)) < 0.6 * top:
                continue
            heading_tokens = token_set(hit.get("heading", ""))
            for pos, raw in enumerate(split_sentences(hit.get("text", ""))):
                sentence = _clean(raw)
                if len(sentence) < 6:
                    continue
                sentence_tokens = token_set(sentence)
                direct = len(query_tokens & sentence_tokens) / len(query_tokens)
                context = len(query_tokens & (heading_tokens - sentence_tokens)) / len(query_tokens)
                score = direct + 0.3 * context
                if score >= 0.34:
                    scored.append((score - rank * 0.05, hit["ref"], pos, sentence))
        scored.sort(key=lambda s: -s[0])
        picked: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for _, ref, pos, sentence in scored:
            if sentence in seen:
                continue
            seen.add(sentence)
            picked.append((ref, pos, sentence))
            if len(picked) == limit:
                break
        picked.sort()
        return [f"{sentence.rstrip('.;')}[{ref}]." for ref, _, sentence in picked]

    @staticmethod
    def _kb_patch(prompt: str) -> str:
        match = re.search(r"\{.*\}", prompt, re.S)
        data = _loads(match.group(0)) if match else {}
        question = str(data.get("question", "")).strip().rstrip("?")
        correction = str(data.get("correction", "")).strip()
        return f"## {question}\n\n{correction}\n"
