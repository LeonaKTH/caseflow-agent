"""A rule-based stand-in for a tool-calling LLM, so the agent, API and tests run without API keys.

It recognises a few intents (track, refund, cancel, list orders, policies, ask for a person), calls the
same tools a real model would, and turns tool results into short replies. It has no real language
understanding: use OpenAI or Azure OpenAI for real conversations. The tools, policy checks, approvals
and checkpointing are the same code in both modes.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_ORDER_ID = re.compile(r"(?<!\d)#?(\d{4})(?!\d)")
_AMOUNT = re.compile(r"(?:€\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:€|eur\b|euros?\b))", re.I)
_INTENTS = [
    ("human", r"\b(human|person|agent|someone|complain|manager|speak to)\b"),
    ("cancel", r"\bcancel"),
    ("refund", r"\b(refund|money back|return|broken|damaged|faulty|defective)\b"),
    ("track", r"\b(where|track|status|arriv|deliver|shipped|shipping|when will)"),
    ("policy", r"\b(policy|policies|warranty|how long|how many days|return window)\b"),
    ("orders", r"\b(my orders|order history|what did i order|list)\b"),
]


def _intent(text: str) -> str | None:
    lower = text.lower()
    if re.search(r"\b(policy|policies|warranty|how long|how many days)\b", lower) and not _ORDER_ID.search(text):
        return "policy"
    return next((name for name, pattern in _INTENTS if re.search(pattern, lower)), None)


def _call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}])


class OfflineSupportModel(BaseChatModel):
    tool_names: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "offline-support-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> OfflineSupportModel:
        return self.model_copy(update={"tool_names": [getattr(t, "name", str(t)) for t in tools]})

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._respond(messages))])

    def _respond(self, messages: list[BaseMessage]) -> AIMessage:
        last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        text = str(messages[last_human].content)
        this_turn = messages[last_human + 1 :]
        results = {
            m.name: json.loads(m.content) for m in this_turn if isinstance(m, ToolMessage) and _is_json(m.content)
        }
        intent = _intent(text)
        match = _ORDER_ID.search(text)
        order_id = match.group(1) if match else None

        if not results:
            return self._first_step(intent, order_id, text)

        eligibility = results.get("check_refund_eligibility")
        if eligibility and eligibility.get("eligible") and "request_refund" not in results:
            amount = _requested_amount(text) or eligibility["max_amount"]
            return _call("request_refund", order_id=eligibility["order_id"], amount=float(amount), reason=text[:200])

        return AIMessage(content=_reply(results, intent))

    @staticmethod
    def _first_step(intent: str | None, order_id: str | None, text: str) -> AIMessage:
        if intent == "human":
            return _call("escalate_to_human", summary=text[:300])
        if intent == "policy":
            return _call("search_policies", query=text)
        if order_id:
            tool = {"cancel": "cancel_order", "refund": "check_refund_eligibility", "track": "track_shipment"}
            return _call(tool.get(intent, "get_order"), order_id=order_id)
        if intent in {"orders", "refund", "track", "cancel"}:
            return _call("list_my_orders")
        return AIMessage(
            content="I can help with orders, deliveries, refunds and cancellations. "
            "What would you like to do? If it's about an order, please include the order number."
        )


def _is_json(content: Any) -> bool:
    try:
        json.loads(content)
        return True
    except (TypeError, ValueError):
        return False


def _requested_amount(text: str) -> str | None:
    match = _AMOUNT.search(text)
    return (match.group(1) or match.group(2)).replace(",", ".") if match else None


def _reply(results: dict[str, dict], intent: str | None) -> str:
    for result in results.values():
        if "error" in result:
            return f"{result['error']} Could you check the order number?"

    if refund := results.get("request_refund"):
        status = refund["status"]
        if status == "refunded":
            return (
                f"Done: I've refunded {refund['amount']} {refund['currency']} for order {refund['order_id']} "
                f"(refund {refund['refund_id']}). It will arrive {refund['arrives']}."
            )
        if status == "denied":
            return f"I'm sorry, the refund wasn't approved: {refund['note']}"
        return f"I couldn't process that refund: {refund['reason']}"
    if (eligibility := results.get("check_refund_eligibility")) and not eligibility["eligible"]:
        return f"I'm sorry, order {eligibility['order_id']} can't be refunded: {eligibility['reason']}"
    if cancel := results.get("cancel_order"):
        if cancel["status"] == "cancelled":
            return f"Order {cancel['order_id']} ({cancel['item']}) is cancelled. {cancel['note']}"
        return f"I couldn't cancel order {cancel['order_id']}: {cancel['reason']}"
    if track := results.get("track_shipment"):
        if track["status"] == "delivered":
            return f"Order {track['order_id']} ({track['item']}) was delivered on {track['delivered']}."
        if track["status"] == "shipped":
            return (
                f"Order {track['order_id']} ({track['item']}) is on its way with tracking number "
                f"{track['tracking']}, expected on {track['eta']}."
            )
        return f"Order {track['order_id']} ({track['item']}) is {track['status']} and hasn't shipped yet."
    if order := results.get("get_order"):
        return f"Order {order['order_id']}: {order['item']}, {order['total']} {order['currency']}, {order['status']}."
    if (orders := results.get("list_my_orders")) is not None:
        lines = [f"#{o['order_id']} {o['item']} ({o['status']})" for o in orders["orders"]]
        ask = " Which order do you mean?" if intent in {"refund", "track", "cancel"} else ""
        return "Here are your orders: " + "; ".join(lines) + "." + ask
    if policy := results.get("search_policies"):
        if not policy["results"]:
            return "I couldn't find a policy about that. Would you like me to ask a colleague?"
        return policy["results"][0]["text"]
    if escalation := results.get("escalate_to_human"):
        return f"I've passed this to a colleague (ticket {escalation['ticket_id']}). {escalation['note']}"
    return "Sorry, something went wrong on my side. Please try again."
