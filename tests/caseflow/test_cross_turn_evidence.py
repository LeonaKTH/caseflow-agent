"""Cross-turn evidence recovery: protocol fixtures, never model-quality claims."""

import json

import httpx
import pytest

from caseflow.agent_eval import run_case
from caseflow.contracts import Conflict, Decision, Principal
from caseflow.planner import ModelPlanner, ScriptedPlanner
from caseflow.service import CaseFlow
from caseflow.support_business import SupportLedger


def reply(decision):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(decision)}}]})


@pytest.mark.parametrize("strategy", ["baseline", "improved"])
@pytest.mark.parametrize("model_limit", [1, 8])
def test_order_then_amount_requires_current_turn_snapshot(tmp_path, strategy, model_limit):
    def handle(request):
        body = json.loads(request.content)
        assert "Observations expire at the end of each user turn" in body["messages"][0]["content"]
        state = json.loads(body["messages"][1]["content"])
        if not state["history"]:
            if not state["observations"]:
                return reply({"kind": "tool", "tool": "get_order", "order_id": "1001"})
            return reply({"kind": "clarify", "action": "refund", "order_id": "1001", "message": "准确退多少？"})
        # Deliberately omit the fresh lookup. Controller must fetch and REPLAN, not commit this decision.
        return reply({"kind": "proposal", "action": "refund", "order_id": "1001", "amount": "8"})

    planner = ModelPlanner(
        "https://fixture.invalid/chat/completions",
        "fixture",
        "fake-only",
        enabled=True,
        strategy=strategy,
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    planner.label = "mock-cross-turn"
    # First turn needs two model calls; shrink only the SECOND turn for the exhausted-budget case.
    original = planner.decide

    def decide(state):
        if state["history"]:
            planner.budget["model_calls"] = model_limit
        return original(state)

    planner.decide = decide
    case = {
        "id": "cross-turn-fixture",
        "family": "contract",
        "split": "dev",
        "turns": ["头盔订单1001要部分退款，金额稍后说", "准确退8 EUR"],
        "gold": {"operations": [["1001", "refund", 800]], "auto_confirm": True},
    }
    try:
        result = run_case(case, strategy, tmp_path, planner)
    finally:
        planner.close()
    last = result["turns"][-1]
    recoveries = [e for e in last["trace"] if e["kind"] == "evidence_recovery"]
    if model_limit == 8:
        assert result["passed"] and result["receipts"][0]["amount_minor"] == 800
        assert len(recoveries) == 1
        assert last["usage"]["planner_calls"] == 2 and last["usage"]["tool_calls"] == 1
    else:
        assert last["status"] == "handoff" and result["receipts"] == []
        assert not recoveries and last["usage"]["tool_calls"] == 0


def test_recovery_reads_changed_state_and_never_uses_history_as_snapshot(tmp_path):
    customer = Principal("C100", "customer")
    staff = Principal("staff", "reviewer")
    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": "get_order", "order_id": "1006"},
            {"kind": "clarify", "action": "cancel", "order_id": "1006", "message": "要取消吗？"},
            {"kind": "proposal", "order_id": "1006", "action": "cancel"},
            {"kind": "proposal", "order_id": "1006", "action": "cancel"},
        ]
    )
    service = CaseFlow(ledger, planner)
    first = service.chat(customer, "请查备用雨罩订单1006")
    ledger.change_logistics(staff, "1006", "shipped", 1)
    last = service.chat(customer, "取消1006", first["conversation_id"])
    assert last["status"] == "refused" and last["reply"] == "CANNOT_CANCEL"
    fresh = [e for e in last["trace"] if e["kind"] == "tool_result"]
    assert fresh[0]["result"]["status"] == "shipped"
    assert sum(e["kind"] == "evidence_recovery" for e in last["trace"]) == 1
    with ledger.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_recovery_cannot_repeat_for_changed_target_or_bypass_version_check(tmp_path):
    customer = Principal("C100", "customer")
    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    planner = ScriptedPlanner(
        [
            {"kind": "clarify", "message": "哪一单？"},
            {"kind": "proposal", "order_id": "1004", "action": "cancel"},
            {"kind": "proposal", "order_id": "1006", "action": "cancel"},
        ]
    )
    service = CaseFlow(ledger, planner)
    first = service.chat(customer, "查一下雨罩")
    last = service.chat(customer, "取消备用那件", first["conversation_id"])
    assert last["status"] == "handoff"
    assert sum(e["kind"] == "evidence_recovery" for e in last["trace"]) == 1
    assert last["usage"]["tool_calls"] == 1
    p = ledger.propose(customer, first["conversation_id"], Decision(kind="proposal", action="cancel", order_id="1004"))
    ledger.change_logistics(Principal("staff", "reviewer"), "1004", "shipped", 1)
    with pytest.raises(Conflict, match="ORDER_CHANGED"):
        ledger.confirm(customer, p["id"], p["version"], p["digest"])


@pytest.mark.parametrize("target,tool_limit,code", [("2001", 6, "NOT_FOUND"), ("1004", 0, "ORDER_EVIDENCE_REQUIRED")])
def test_recovery_cannot_bypass_identity_or_tool_budget(tmp_path, target, tool_limit, code):
    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    planner = ScriptedPlanner(
        [
            {"kind": "clarify", "message": "需要哪项操作？"},
            {"kind": "proposal", "order_id": target, "action": "cancel"},
        ]
    )
    planner.budget = {"model_calls": 5, "tool_calls": tool_limit}
    service = CaseFlow(ledger, planner)
    customer = Principal("C100", "customer")
    first = service.chat(customer, "帮我看看售后")
    last = service.chat(customer, "取消订单" + target, first["conversation_id"])
    assert last["status"] == "handoff"
    assert last["ticket"]["reason"] == code
    assert last["usage"]["tool_calls"] <= tool_limit
    with ledger.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
