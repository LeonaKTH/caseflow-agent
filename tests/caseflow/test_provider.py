import json

import httpx
import pytest

from caseflow.planner import ModelPlanner


def state():
    return {"text": "refund", "context": {}, "observations": [], "today": "2025-10-01"}


def test_live_calls_are_disabled_without_explicit_opt_in():
    with pytest.raises(ValueError, match="authorization"):
        ModelPlanner("https://provider.invalid/chat/completions", "test", "not-a-real-key")


def test_provider_schema_and_timeout_use_mock_transport_only():
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["model"] == "fixture-model"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"kind":"clarify"}'}}], "usage": {"total_tokens": 12}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        planner = ModelPlanner(
            "https://provider.invalid/chat/completions", "fixture-model", "fake-test-only", enabled=True, client=client
        )
        assert planner.decide(state()).kind == "clarify"
        assert len(calls) == 1
        assert planner.last_usage == {"total_tokens": 12}

    def timeout(request):
        raise httpx.ReadTimeout("fixture timeout")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        planner = ModelPlanner(
            "https://provider.invalid/chat/completions", "fixture", "fake-test-only", enabled=True, client=client
        )
        with pytest.raises(TimeoutError):
            planner.decide(state())


def test_available_usage_is_reported_without_inventing_price(tmp_path):
    from caseflow.contracts import Principal
    from caseflow.repository import Repository
    from caseflow.service import CaseFlow

    def response(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"kind":"clarify"}'}}], "usage": {"total_tokens": 12}}
        )

    repository = Repository(tmp_path / "usage.db")
    repository.seed()
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        planner = ModelPlanner(
            "https://provider.invalid/chat/completions", "fixture", "fake-test-only", enabled=True, client=client
        )
        result = CaseFlow(repository, planner).chat(Principal("C100", "customer"), "refund")
    assert result["usage"]["tokens"] == 12
    assert result["usage"]["cost"] is None
