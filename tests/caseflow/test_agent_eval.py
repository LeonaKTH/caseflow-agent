import json
import sys

import pytest

from caseflow.agent_eval import Quota, fraction, metrics, run_case
from caseflow.planner import ScriptedPlanner
from support_agent.service import default_data_dir


def test_frozen_dataset_groups_never_cross_splits():
    data = json.loads((default_data_dir() / "agent_eval_v1.json").read_text(encoding="utf-8"))
    assert len({c["id"] for c in data["cases"]}) == len(data["cases"])
    groups = {}
    for case in data["cases"]:
        groups.setdefault(case["family"], set()).add(case["split"])
    assert all(len(splits) == 1 for splits in groups.values())


def test_metric_denominators_and_null_values():
    row = {
        "id": "one",
        "passed": False,
        "gold": {
            "operations": [],
            "auto_confirm": True,
            "required_tools": ["get_order"],
            "clarification": False,
            "handoff": True,
        },
        "tools": [],
        "receipts": [{}],
        "unsafe_proposal": True,
        "clarifications": 1,
        "handoff": False,
        "recovery_success": False,
        "turns": [{"status": "clarify", "usage": {"planner_calls": 2, "llm_calls": 0, "tokens": None}}],
        "elapsed_s": 1,
    }
    report = metrics([row])
    assert report["forbidden_commit"] == fraction(1, 1)
    assert report["over_clarification"] == fraction(1, 1)
    assert report["tool_selection"] == fraction(0, 1)
    assert report["wrong_legal_refusal"]["value"] is None
    assert report["handoff_precision"]["value"] is None
    assert report["per_task_usage"][0]["actual_cost"] is None


def test_mechanical_consent_does_not_mask_wrong_model_proposal(tmp_path):
    # Gold is unchanged. The new shared semantic guard must block the wrong10
    # before mechanical consent; judge still marks the intended task incomplete.
    case = {
        "id": "independent-gold",
        "family": "test",
        "split": "dev",
        "turns": ["refund1005 5EUR"],
        "gold": {"operations": [["1005", "refund", 500]], "auto_confirm": True},
    }
    model = ScriptedPlanner(
        [
            {"kind": "tool", "tool": "get_order", "order_id": "1005"},
            {"kind": "proposal", "action": "refund", "order_id": "1005", "amount": "10"},
        ]
    )
    result = run_case(case, "improved", tmp_path, model)
    assert result["receipts"] == []
    assert not result["passed"]
    assert result["turns"][-1]["status"] == "clarify"
    assert result["turns"][-1]["semantic_guard"]["code"] == "AMOUNT_INTENT_MISMATCH"


def test_session_quota_counts_attempts_and_reserves_cost():
    quota = Quota(2, "0.01", 100, 100)
    assert quota.reserve([], 1)
    assert quota.reserve([], 1)
    assert not quota.reserve([], 1)
    assert quota.calls == 2
    costly = Quota(5, "0.00001", 100, 100)
    assert not costly.reserve([{"content": "a" * 100}], 100)
    assert costly.calls == 0


def test_live_run_rejects_price_budget_currency_mismatch_before_provider(monkeypatch):
    from caseflow.agent_eval import main

    monkeypatch.setenv("CASEFLOW_INPUT_PRICE_PER_M", "1")
    monkeypatch.setenv("CASEFLOW_OUTPUT_PRICE_PER_M", "1")
    monkeypatch.setenv("CASEFLOW_PRICE_CURRENCY", "USD")
    monkeypatch.setattr(
        sys, "argv", ["eval", "--mode", "live", "--execute", "--budget-amount", "5", "--budget-currency", "CNY"]
    )
    with pytest.raises(SystemExit, match="matching CASEFLOW_PRICE_CURRENCY"):
        main()
