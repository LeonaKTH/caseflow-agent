"""Application boundary: trusted actor in, model proposals out, no model authority."""

import json
import threading
import time
import uuid

from caseflow.contracts import MAX_CLARIFICATIONS, Conflict, Decision
from caseflow.planner import OfflinePlanner, build_planning_graph
from caseflow.semantic_guard import VERSION as SEMANTIC_VERSION
from caseflow.semantic_guard import proposal_issue
from caseflow.telemetry import reported_cost, sum_costs


class CaseFlow:
    def __init__(self, repository, planner=None, execution=None):
        self.repository = repository
        self.planner = planner or OfflinePlanner()
        self.execution = execution
        self._planning_lock = threading.RLock()

    def submit(self, actor, pid, key):
        if self.execution is None:
            return self.repository.submit(actor, pid, key)
        operation = self.repository.prepare(actor, pid, key)
        return self.execution.run(actor, operation["id"])

    def approve(self, actor, oid, approved, version, comment=""):
        if self.execution is None:
            return self.repository.approve(actor, oid, approved, version, comment)
        self.repository.decide(actor, oid, approved, version, comment)
        return self.execution.run(actor, oid)

    def chat(self, actor, text, conversation_id=None, request_id=None):
        # One local planner instance owns mutable provider telemetry. Serialize turns.
        with self._planning_lock:
            return self._chat(actor, text, conversation_id, request_id)

    def _chat(self, actor, text, conversation_id, request_id):
        started = time.perf_counter()
        actor.require("customer")
        c = self.repository.conversation(actor, conversation_id)
        self.repository.invalidate(actor, c["id"])
        today = self.repository.today()
        context = json.loads(c["context"])
        history = self.repository.history(actor, c["id"])
        if hasattr(self.repository, "tickets"):
            context["tickets"] = [
                {k: t.get(k) for k in ("id", "status", "resolution", "version")}
                for t in self.repository.tickets(actor, c["id"])
            ]
        state = build_planning_graph(self.planner, self.repository, actor).invoke(
            {
                "text": text,
                "context": context,
                "observations": [],
                "today": today,
                "calls": 0,
                "tools": 0,
                "trace": [],
                "decision": {},
                "finished": False,
                "history": history,
                "repair_attempts": 0,
                "evidence_recoveries": 0,
            },
            {"recursion_limit": max(16, 2 * getattr(self.planner, "budget", {}).get("model_calls", 5) + 4)},
        )
        d = Decision.model_validate(state["decision"])
        usage_records = [
            e.get("provider_usage") for e in state["trace"] if e["kind"] in {"planner_decision", "planner_error"}
        ]
        known_tokens = [
            u["total_tokens"]
            for u in usage_records
            if isinstance(u, dict) and type(u.get("total_tokens")) is int and u["total_tokens"] >= 0
        ]
        result = {
            "conversation_id": c["id"],
            "status": d.kind,
            "trace": state["trace"],
            "usage": {
                "planner_calls": state["calls"],
                "llm_calls": sum(bool(e.get("request_sent")) for e in state["trace"])
                if self.planner.label.startswith("live")
                else 0,
                "tool_calls": state["tools"],
                "tokens": sum(known_tokens) if known_tokens and len(known_tokens) == state["calls"] else None,
                "cost": sum_costs([reported_cost(u) for u in usage_records]),
                "mode": self.planner.label,
                "session_requests_used": getattr(getattr(self.planner, "quota", None), "calls", None),
                "reserved_estimated_cost_cny": str(getattr(getattr(self.planner, "quota", None), "reserved", 0))
                if getattr(self.planner, "quota", None)
                else None,
            },
        }
        intents = [d] if d.kind == "proposal" else d.proposals if d.kind == "proposals" else []
        for intent in intents:
            seen = any(o["tool"] == "get_order" and o["result"]["id"] == intent.order_id for o in state["observations"])
            order = next(
                (
                    o["result"]
                    for o in state["observations"]
                    if o["tool"] == "get_order" and o["result"]["id"] == intent.order_id
                ),
                None,
            )
            issue = proposal_issue(intent, text, context, history, order) if seen else None
            if issue:
                # Check the entire batch before creating any peer proposal.
                result["semantic_guard"] = {"version": SEMANTIC_VERSION, "code": issue}
                d = Decision(
                    kind="clarify",
                    action=intent.action,
                    order_id=None if "ORDER" in issue else intent.order_id,
                    message="请明确每项操作对应的订单，以及准确退款金额或明确全额；不会代选金额。",
                )
                break
        if d.kind == "proposal":
            # Write proposals must be grounded in an actually observed order, not model assertions.
            seen = any(o["tool"] == "get_order" and o["result"]["id"] == d.order_id for o in state["observations"])
            if not seen:
                result.update(status="handoff", reply="ORDER_EVIDENCE_REQUIRED")
            else:
                try:
                    proposal = self.repository.propose(actor, c["id"], d, supersede=False)
                    result.update(proposal=proposal, reply="请确认此版本提案 / Confirm this proposal version.")
                except Conflict as error:
                    result.update(status="refused", reply=error.code)
        elif d.kind == "proposals":
            proposals, errors = [], []
            for intent in d.proposals:
                seen = any(
                    o["tool"] == "get_order" and o["result"]["id"] == intent.order_id for o in state["observations"]
                )
                try:
                    if not seen:
                        raise Conflict("ORDER_EVIDENCE_REQUIRED")
                    proposals.append(
                        self.repository.propose(
                            actor, c["id"], Decision(kind="proposal", **intent.model_dump()), supersede=False
                        )
                    )
                except Conflict as error:
                    errors.append({"order_id": intent.order_id, "code": error.code})
            result.update(
                status="proposals" if proposals else "refused",
                proposals=proposals,
                errors=errors,
                reply="每项操作需要分别确认；未提交任何退款或取消。"
                if proposals
                else "没有可安全确认的操作，请查看原因。",
            )
        elif d.kind == "clarify":
            count = self.repository.clarify(actor, c["id"], {"action": d.action, "order_id": d.order_id})
            result.update(
                status="clarify" if count <= MAX_CLARIFICATIONS else "handoff",
                reply=(d.message or "请提供明确的订单和操作；退款还需要准确金额或明确全额。")
                if count <= MAX_CLARIFICATIONS
                else "CLARIFICATION_LIMIT",
                clarification_round=min(count, MAX_CLARIFICATIONS),
            )
        elif d.kind == "answer":
            tickets = [o["result"] for o in state["observations"] if o["tool"] == "get_ticket"]
            if tickets:
                result["reply"] = "\n".join(
                    f"工单 {t['id']}：{t['status']}。" + (t.get("resolution") or "正在等待人工处理。") for t in tickets
                )
            else:
                result["reply"] = json.dumps(state["observations"], ensure_ascii=False)
        else:
            result["reply"] = d.message or "HUMAN_HANDOFF"
        result["run_id"] = uuid.uuid4().hex
        result["request_id"] = request_id or result["run_id"]
        result["usage"]["elapsed_s"] = time.perf_counter() - started
        if result["status"] == "handoff" and hasattr(self.repository, "create_ticket"):
            order_ids = sorted({o["result"]["id"] for o in state["observations"] if o["tool"] == "get_order"})
            try:
                result["ticket"] = self.repository.create_ticket(
                    actor,
                    c["id"],
                    text,
                    result["reply"],
                    state["observations"],
                    order_ids,
                    "handoff-" + result["run_id"],
                )
                result["reply"] = "已创建人工客服工单，尚未执行任何业务操作。工单：" + result["ticket"]["id"]
            except Conflict as error:
                result["ticket_creation_error"] = error.code
                result["reply"] = "转人工请求尚未保存，请重试；未执行业务操作。"
        self.repository.record_turn(actor, c["id"], text, result)
        return result
