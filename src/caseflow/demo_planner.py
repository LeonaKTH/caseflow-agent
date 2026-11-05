"""Interactive MOCK ONLY. Generic lexical routing, never presented as LLM reasoning."""

import re

from caseflow.contracts import Decision, ProposalIntent
from caseflow.planner import OfflinePlanner
from caseflow.semantic_guard import explicit_full


class DemoPlanner(OfflinePlanner):
    label = "mock-lexical-v1"
    budget = {"model_calls": 8, "tool_calls": 7}

    def decide(self, state):
        text = state["text"]
        context = state.get("context", {})
        observations = state["observations"]
        if re.search(r"进展|处理结果|工单|ticket status", text, re.I) and context.get("tickets"):
            ticket = context["tickets"][-1]
            if not any(o["tool"] == "get_ticket" for o in observations):
                return Decision(kind="tool", tool="get_ticket", ticket_id=ticket["id"])
            return Decision(kind="answer")
        # Split independent clauses, then resolve each through the same read tools.
        clauses = [c.strip() for c in re.split(r"[；;，,]|\band\b", text) if c.strip()]
        intents = []
        for clause in clauses:
            ids = re.findall(r"(?<!\d)(\d{4})(?!\d)", clause)
            action = (
                "cancel"
                if re.search(r"取消|cancel", clause, re.I)
                else "refund"
                if re.search(r"退款|退掉|refund", clause, re.I)
                else None
            )
            if len(ids) == 1 and action:
                amount = re.search(r"(\d+(?:\.\d+)?)\s*(?:欧|EUR|€)", clause, re.I)
                intents.append(
                    ProposalIntent(
                        action=action,
                        order_id=ids[0],
                        amount=amount.group(1) if amount else None,
                        full_refund=explicit_full(clause),
                    )
                )
        if len(intents) > 1:
            for intent in intents:
                if not any(o["tool"] == "get_order" and o["result"]["id"] == intent.order_id for o in observations):
                    return Decision(kind="tool", tool="get_order", order_id=intent.order_id)
                for read_tool in ["get_logistics", "refund_info"] if intent.action == "refund" else ["get_logistics"]:
                    if not any(
                        o["tool"] == read_tool and o["result"].get("order_id") == intent.order_id for o in observations
                    ):
                        return Decision(kind="tool", tool=read_tool, order_id=intent.order_id)
                if intent.action == "refund" and intent.amount is None and not intent.full_refund:
                    return Decision(
                        kind="clarify",
                        action="refund",
                        order_id=intent.order_id,
                        message="请明确部分退款金额，或说明全额退款。",
                    )
            if not any(o["tool"] == "search_policies" for o in observations):
                return Decision(kind="tool", tool="search_policies", query="退款 取消 审批")
            return Decision(kind="proposals", proposals=intents)
        if re.search(r"争议|丢失|人工|human|warranty claim", text, re.I):
            return Decision(kind="handoff", message="该问题需要人工客服调查")
        return super().decide(state)
