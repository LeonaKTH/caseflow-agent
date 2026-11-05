import json
from datetime import date
from decimal import Decimal

from support_agent.policy import PolicyConfig, can_cancel, needs_approval, refund_eligibility
from support_agent.service import default_data_dir
from support_agent.store import Store

POLICY = PolicyConfig()


def store() -> Store:
    return Store.load(default_data_dir() / "store.json")


def test_refunds_are_allowed_within_the_return_window_only():
    s = store()
    assert refund_eligibility(s.orders["1001"], s.today, POLICY).eligible
    assert not refund_eligibility(s.orders["1003"], s.today, POLICY).eligible  # 83 days ago
    assert not refund_eligibility(s.orders["1002"], s.today, POLICY).eligible  # not delivered yet
    assert refund_eligibility(s.orders["1003"], date(2025, 8, 1), POLICY).eligible  # same order, earlier date


def test_an_order_can_only_be_refunded_once():
    s = store()
    s.record_refund(s.orders["1005"], Decimal("18.00"), "broken", "policy")
    assert not refund_eligibility(s.orders["1005"], s.today, POLICY).eligible


def test_approval_threshold():
    assert not needs_approval(Decimal("50.00"), POLICY)
    assert needs_approval(Decimal("50.01"), POLICY)


def test_only_unshipped_orders_can_be_cancelled():
    s = store()
    assert can_cancel(s.orders["1004"])[0]
    assert not can_cancel(s.orders["1002"])[0]


def test_other_customers_orders_are_invisible():
    s = store()
    assert s.order_for("C100", "1001") is not None
    assert s.order_for("C100", "2001") is None
    assert s.order_for("C200", "2001") is not None


def test_tools_never_expose_the_customer_id_to_the_model(service):
    tools = {t.name: t for t in service.graph.nodes["tools"].bound.tools_by_name.values()}
    for tool in tools.values():
        assert "customer_id" not in json.dumps(tool.tool_call_schema.model_json_schema())
        assert "config" not in tool.tool_call_schema.model_json_schema().get("properties", {})
