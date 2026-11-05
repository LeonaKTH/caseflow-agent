"""Shared wire/controller fixtures, not an LLM effectiveness benchmark."""

import json

import httpx
import pytest
from pydantic import ValidationError

from caseflow.agent_eval import run_case
from caseflow.contracts import Decision
from caseflow.planner import ModelPlanner
from support_agent.graph import SYSTEM_PROMPT


def model(strategy, handler):
    planner = ModelPlanner(
        "https://fixture.invalid/chat/completions",
        "same-fixture-model",
        "fake-only",
        enabled=True,
        strategy=strategy,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    planner.label = "mock-fairness-protocol"
    return planner


def reply(decision):
    return httpx.Response(
        200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}]}
    )


def test_same_wire_schema_input_and_budget_but_distinct_upstream_policy():
    captured = {}
    for strategy in ("baseline", "improved"):

        def handle(request, strategy=strategy):
            captured[strategy] = json.loads(request.content)
            return reply({"kind": "clarify", "message": "Which order?"})

        planner = model(strategy, handle)
        try:
            planner.decide(
                {"text": "我要退款", "context": {}, "history": [], "observations": [], "today": "2025-10-01"}
            )
            assert planner.budget == {"model_calls": 8, "tool_calls": 7}
        finally:
            planner.close()
    b, i = captured["baseline"], captured["improved"]
    assert {k: v for k, v in b.items() if k != "messages"} == {k: v for k, v in i.items() if k != "messages"}
    assert b["messages"][1] == i["messages"][1]
    bs, ins = b["messages"][0]["content"], i["messages"][0]["content"]
    assert SYSTEM_PROMPT.format(customer_name="the authenticated customer") in bs
    assert "Before a proposal collect the relevant order, logistics/refund eligibility" not in bs
    assert "Before a proposal collect the relevant order, logistics/refund eligibility" in ins
    for text in (bs, ins):
        assert json.dumps(Decision.model_json_schema()) in text
        assert "Every proposed operation needs a get_order observation" in text
        assert "Missing/ambiguous amount or order binding requires clarification" in text
    assert "escalate_to_human becomes kind=handoff" in bs


@pytest.mark.parametrize("strategy", ["baseline", "improved"])
@pytest.mark.parametrize("grounded", [False, True])
def test_identical_snapshot_consent_approval_and_sqlite_judge(tmp_path, strategy, grounded):
    # Same scripted provider output for both strategies, through real Agent/business HTTP.
    def handle(request):
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        required = "get_order" if grounded else "refund_info"
        if not state["observations"]:
            return reply({"kind": "tool", "tool": required, "order_id": "1001"})
        return reply({"kind": "proposal", "action": "refund", "order_id": "1001", "amount": "60"})

    planner = model(strategy, handle)
    case = {
        "id": "protocol-only",
        "family": "fixture",
        "split": "dev",
        "turns": ["refund1001 60EUR"],
        "gold": {"operations": [["1001", "refund", 6000]], "auto_confirm": True, "expect_approval": True},
    }
    try:
        result = run_case(case, strategy, tmp_path, planner)
    finally:
        planner.close()
    if grounded:
        assert result["passed"]
        assert result["receipts"][0]["amount_minor"] == 6000
        assert result["checks"]["approval"]
    else:
        assert not result["passed"] and result["receipts"] == []
        assert result["turns"][-1]["status"] == "handoff"
        assert result["tickets"][0]["reason"] == "ORDER_EVIDENCE_REQUIRED"


def test_mutually_exclusive_proposal_branches_and_no_silent_strategy_fallback():
    with pytest.raises(ValidationError):
        Decision(
            kind="proposals", full_refund=True, proposals=[{"order_id": "1001", "action": "refund", "amount": "5"}]
        )
    with pytest.raises(ValidationError):
        Decision(kind="proposal", tool="get_order", order_id="1004", action="cancel")
    with pytest.raises(ValueError, match="Unknown planner strategy"):
        ModelPlanner("https://fixture.invalid/chat/completions", "fake", "fake", enabled=True, strategy="baseline-typo")
