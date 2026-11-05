import json

import httpx
import pytest

from caseflow.contracts import Principal
from caseflow.planner import ModelPlanner, ScriptedPlanner
from caseflow.repository import Repository
from caseflow.service import CaseFlow
from caseflow.settings import ModelSettings, configured_planner

C = Principal("C100", "customer")


def repo(tmp_path):
    r = Repository(tmp_path / "business.db")
    r.seed()
    return r


def test_environment_live_is_opt_in_and_secret_is_not_displayed(monkeypatch):
    monkeypatch.setenv("CASEFLOW_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("CASEFLOW_MODEL", "fixture-model")
    monkeypatch.setenv("CASEFLOW_API_KEY", "secret-test-only")
    monkeypatch.delenv("CASEFLOW_ALLOW_PAID", raising=False)
    settings = ModelSettings.from_env()
    assert "secret-test-only" not in repr(settings)
    assert "secret-test-only" not in json.dumps(settings.public())
    with pytest.raises(ValueError, match="disabled"):
        configured_planner("live")


def test_actual_billing_never_infers_currency_or_missing_attempts():
    from caseflow.telemetry import reported_cost, sum_costs

    cost = reported_cost({"cost": 0.001, "cost_currency": "USD"})
    assert sum_costs([cost, cost]) == {"amount": "0.002", "currency": "USD"}
    assert reported_cost({"cost": 0.001}) is None
    assert reported_cost({"cost": float("inf"), "currency": "USD"}) is None
    assert sum_costs([cost, None]) is None
    assert sum_costs([cost, reported_cost({"cost": 1, "currency": "CNY"})]) is None


def test_format_repair_is_bounded_and_uses_history_without_keys(tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        content = "bad JSON" if len(requests) == 1 else '{"kind":"clarify","action":"refund","message":"哪一单？"}'
        return httpx.Response(
            200,
            json={
                "model": "fixture-2026",
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 3},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = ModelPlanner(
            "https://fixture.invalid/v1/chat/completions", "fixture", "secret-test-only", enabled=True, client=client
        )
        s = CaseFlow(repo(tmp_path), model)
        first = s.chat(C, "有个东西坏了，能看看吗")
        assert first["usage"]["planner_calls"] == 2
        assert first["usage"]["tokens"] == 6
        s.chat(C, "就是上次说的那个", first["conversation_id"])
        assert json.loads(requests[-1]["messages"][1]["content"])["history"]
        assert "secret-test-only" not in json.dumps(requests)


def test_two_orders_make_independent_active_proposals(tmp_path):
    r = repo(tmp_path)
    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": "get_order", "order_id": "1001"},
            {"kind": "tool", "tool": "get_order", "order_id": "1004"},
            {
                "kind": "proposals",
                "proposals": [
                    {"action": "refund", "order_id": "1001", "amount": "60"},
                    {"action": "cancel", "order_id": "1004"},
                ],
            },
        ]
    )
    result = CaseFlow(r, planner).chat(C, "头盔退60欧，雨罩取消")
    assert result["status"] == "proposals"
    assert len(result["proposals"]) == 2
    for p in result["proposals"]:
        assert r.confirm(C, p["id"], p["version"], p["digest"])["status"] == "CONFIRMED"


def test_repeated_invalid_schema_stops_without_side_effects(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"tool","tool":"get_order"}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        p = ModelPlanner("https://fixture.invalid/v1/chat/completions", "fixture", "fake", enabled=True, client=client)
        result = CaseFlow(repo(tmp_path), p).chat(C, "帮忙")
    assert result["status"] == "handoff"
    assert len(calls) == 2


@pytest.mark.parametrize("code,expected", [(429, 2), (500, 2), (401, 1)])
def test_provider_failures_classified_without_leaking_body(tmp_path, code, expected):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(code, text="sensitive-provider-body")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        p = ModelPlanner("https://fixture.invalid/v1/chat/completions", "fixture", "fake", enabled=True, client=client)
        result = CaseFlow(repo(tmp_path), p).chat(C, "帮忙")
    assert result["status"] == "handoff"
    assert len(calls) == expected
    assert "sensitive-provider-body" not in json.dumps(result)
