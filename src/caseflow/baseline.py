"""Adapter for the actual upstream OFFLINE model, with the same trusted executor.

This does not benchmark a real LLM. Legacy write requests become proposals; they
never invoke the unsafe legacy store. Both arms share confirmation/approval policy.
"""

import json

from langchain_core.messages import HumanMessage, ToolMessage

from caseflow.contracts import Decision
from support_agent.offline_model import OfflineSupportModel, _intent


class UpstreamOfflinePlanner:
    label = "upstream-offline-rules"

    def __init__(self):
        self.model = OfflineSupportModel()

    def decide(self, state):
        messages = [HumanMessage(state["text"])]
        intent = _intent(state["text"])
        # The upstream already requested cancellation before our extra read guard.
        # Preserve that request rather than feeding a synthetic get_order result
        # back as if the upstream had chosen that different tool itself.
        if intent == "cancel" and state["observations"]:
            for observation in state["observations"]:
                if observation["tool"] == "get_order":
                    return Decision(kind="proposal", action="cancel", order_id=observation["result"]["id"])
        for i, observation in enumerate(state["observations"]):
            tool, result = observation["tool"], observation["result"]
            if tool == "get_order":
                order = result
                if intent == "refund":
                    tool = "check_refund_eligibility"
                    result = {
                        "eligible": True,
                        "order_id": order["id"],
                        "currency": order["currency"],
                        "max_amount": str(order["total_minor"] / 100),
                    }
                else:
                    result = {**order, "order_id": order["id"], "total": str(order["total_minor"] / 100)}
            elif tool == "list_orders":
                tool = "list_my_orders"
                result = {"orders": [{**o, "order_id": o["id"]} for o in result]}
            elif tool == "search_policies":
                result = {"results": [{"section": h, "text": t} for h, t in result]}
            messages.append(ToolMessage(content=json.dumps(result), name=tool, tool_call_id=f"observation_{i}"))
        reply = self.model.invoke(messages)
        if not reply.tool_calls:
            return Decision(kind="answer")
        call = reply.tool_calls[0]
        name, args = call["name"], call["args"]
        if name == "request_refund":
            return Decision(kind="proposal", action="refund", order_id=args["order_id"], amount=str(args["amount"]))
        if name == "cancel_order":
            if not state["observations"]:
                return Decision(kind="tool", tool="get_order", order_id=args["order_id"])
            return Decision(kind="proposal", action="cancel", order_id=args["order_id"])
        if name in {"get_order", "track_shipment", "check_refund_eligibility"}:
            return Decision(kind="tool", tool="get_order", order_id=args["order_id"])
        if name == "list_my_orders":
            return Decision(kind="tool", tool="list_orders")
        if name == "search_policies":
            return Decision(kind="tool", tool="search_policies", query=args["query"])
        return Decision(kind="handoff")
