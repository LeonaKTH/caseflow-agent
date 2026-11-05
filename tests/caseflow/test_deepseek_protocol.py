"""Official-wire-shape fixtures only. MockTransport never reaches DeepSeek."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from caseflow.api import create_app
from caseflow.auth import Auth
from caseflow.business_api import create_business_app
from caseflow.contracts import Principal
from caseflow.execution import Execution
from caseflow.gateway import HTTPBusiness
from caseflow.local_server import running
from caseflow.planner import ModelPlanner
from caseflow.service import CaseFlow
from caseflow.settings import ModelSettings, configured_planner
from caseflow.spending import Quota
from caseflow.support_business import SupportLedger


def wire(content='{"kind":"answer"}', finish="stop"):
    return {
        "id": "mock-request-01",
        "model": "deepseek-flash",
        "system_fingerprint": "mock-build-only",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 20,
            "total_tokens": 50,
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 20,
        },
    }


def configure(monkeypatch):
    monkeypatch.setenv("CASEFLOW_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("CASEFLOW_MODEL", "deepseek-flash")
    monkeypatch.setenv("CASEFLOW_API_KEY", "protocol-fixture-not-a-real-key")
    monkeypatch.setenv("CASEFLOW_ALLOW_PAID", "1")
    for name in [
        "CASEFLOW_INPUT_PRICE_PER_M",
        "CASEFLOW_OUTPUT_PRICE_PER_M",
        "CASEFLOW_PRICE_CURRENCY",
        "CASEFLOW_MAX_REQUESTS",
        "CASEFLOW_BUDGET_CNY",
    ]:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "payload,code",
    [
        (wire("   "), "MODEL_OUTPUT_EMPTY"),
        (wire(None), "MODEL_OUTPUT_EMPTY"),
        (wire('{"kind":"answer"}', "length"), "MODEL_OUTPUT_TRUNCATED"),
        (wire('{"kind":"tool","tool":"get_order"}'), "MODEL_SCHEMA_INVALID"),
        (wire('{"kind":"answer","kind":"proposal"}'), "MODEL_SCHEMA_INVALID"),
        ({"choices": []}, "PROVIDER_RESPONSE_INVALID"),
        ([], "PROVIDER_RESPONSE_INVALID"),
    ],
)
def test_invalid_wire_output_is_bounded_and_never_creates_proposals(tmp_path, payload, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = ModelPlanner(
            "https://api.deepseek.com/chat/completions", "deepseek-flash", "fake-only", enabled=True, client=client
        )
        model.label = "mock-deepseek-protocol"
        result = CaseFlow(ledger, model).chat(Principal("C100", "customer"), "refund1005 5EUR")
    assert len(requests) == 2
    assert result["status"] == "handoff"
    assert result["trace"][0]["code"] == code
    assert not result.get("proposal")
    assert result["usage"]["llm_calls"] == 0
    with ledger.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_deepseek_model_drives_existing_agent_http_approval_and_receipt(tmp_path, monkeypatch):
    configure(monkeypatch)
    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    execution = Execution(ledger, tmp_path / "checkpoint.db")
    auth = Auth("protocol-local-signer-fixture-" * 3)
    key = "protocol-service-fixture-" * 3
    requests = []

    def provider(request):
        body = json.loads(request.content)
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False and body["max_tokens"] == 700
        assert "tools" not in body  # Uses the existing JSON Decision tool-loop protocol.
        state = json.loads(body["messages"][1]["content"])
        observations = {o["tool"] for o in state["observations"]}
        for tool in ["get_order", "get_logistics", "refund_info", "search_policies"]:
            if tool not in observations:
                decision = {
                    "kind": "tool",
                    "tool": tool,
                    **({"query": "partial refund"} if tool == "search_policies" else {"order_id": "1001"}),
                }
                break
        else:
            decision = {"kind": "proposal", "action": "refund", "order_id": "1001", "amount": "60"}
        requests.append(body)
        return httpx.Response(200, json=wire(json.dumps(decision)))

    try:
        with (
            httpx.Client(transport=httpx.MockTransport(provider)) as provider_client,
            running(create_business_app(ledger, execution, auth, key)) as business_url,
        ):
            model = configured_planner("live", client=provider_client)
            model.label = "mock-deepseek-protocol"
            gateway = HTTPBusiness(business_url, auth, key)
            try:
                with (
                    running(create_app(CaseFlow(gateway, model), auth)) as agent_url,
                    httpx.Client(base_url=agent_url, timeout=30) as client,
                ):
                    customer = {"Authorization": "Bearer " + auth.issue("C100")}
                    staff = {"Authorization": "Bearer " + auth.issue("staff", "reviewer")}
                    turn = client.post(
                        "/api/v1/chat", headers=customer, json={"message": "Refund1001 for60EUR, not full."}
                    ).json()
                    p = turn["proposal"]
                    assert p["amount_minor"] == 6000
                    assert model.quota.calls == len(requests) == 5
                    assert turn["usage"]["tokens"] == 250 and turn["usage"]["cost"] is None
                    assert turn["trace"][0]["provider"]["system_fingerprint"] == "mock-build-only"
                    client.post(
                        f"/api/v1/proposals/{p['id']}/confirm",
                        headers=customer,
                        json={"version": p["version"], "digest": p["digest"]},
                    ).raise_for_status()
                    waiting = client.post(
                        f"/api/v1/proposals/{p['id']}/submit",
                        headers=customer,
                        json={"idempotency_key": "protocol-confirmed"},
                    ).json()
                    assert waiting["status"] == "awaiting_approval"
                    final = client.post(
                        f"/api/v1/approvals/{waiting['operation_id']}/decision",
                        headers=staff,
                        json={"version": 1, "approved": True},
                    ).json()
                    assert final["receipt"]["amount_minor"] == 6000
                    trace = client.get("/api/v1/traces/" + turn["run_id"], headers=customer).json()
                    assert "protocol-fixture-not-a-real-key" not in json.dumps(trace)
                    (tmp_path / "representative-trace.json").write_text(
                        json.dumps(
                            {"verification": "mock-protocol-only", "trace": trace, "operation": final},
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
            finally:
                gateway.close()
    finally:
        execution.close()


def test_live_runtime_and_eval_share_budget_even_on_timeout(monkeypatch):
    configure(monkeypatch)
    quota = Quota(1, 5, 2, 8, max_input_bytes=24_000, input_overhead=512)
    attempts = []

    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("fixture timeout, billing unknown")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        model = configured_planner("live", quota=quota, client=client)
        state = {"text": "help", "context": {}, "observations": [], "history": []}
        with pytest.raises(TimeoutError):
            model.decide(state)
        reserved = quota.reserved
        with pytest.raises(ValueError, match="SESSION_BUDGET"):
            model.decide(state)
        assert len(attempts) == quota.calls == 1
        assert quota.reserved == reserved > 0
        assert not model.last_request_sent and "provider_request_id" not in model.last_meta


def test_input_cap_and_peak_price_floor_reject_before_any_request(monkeypatch):
    configure(monkeypatch)
    quota = Quota(48, 5, 2, 8, max_input_bytes=10, input_overhead=512)
    assert not quota.reserve([{"role": "user", "content": "x" * 11}], 700)
    assert quota.calls == 0 and quota.last_error == "INPUT_BUDGET"
    monkeypatch.setenv("CASEFLOW_INPUT_PRICE_PER_M", "1")
    with pytest.raises(ValueError, match="peak"):
        Quota.from_env(ModelSettings.from_env())


def test_keepalive_whitespace_and_malformed_outer_json_are_distinct():
    bodies = ["\n\n" + json.dumps(wire()), "\n{broken"]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=bodies.pop(0)))) as client:
        model = ModelPlanner(
            "https://api.deepseek.com/chat/completions", "deepseek-flash", "fake-only", enabled=True, client=client
        )
        state = {"text": "help", "context": {}, "history": [], "observations": []}
        assert model.decide(state).kind == "answer"
        assert model.last_meta["provider_request_id"] == "mock-request-01"
        with pytest.raises(ValueError, match="PROVIDER_JSON_INVALID"):
            model.decide(state)
        assert "provider_request_id" not in model.last_meta


def test_keepalive_body_deadline_closes_request_and_keeps_budget_reserved(monkeypatch):
    closed = []

    class Keepalive(httpx.SyncByteStream):
        def __iter__(self):
            yield b"\n"

        def close(self):
            closed.append(True)

    ticks = iter([0, 21])
    monkeypatch.setattr("caseflow.planner.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    quota = Quota(48, 5, 2, 8)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Keepalive()))) as client:
        model = ModelPlanner(
            "https://api.deepseek.com/chat/completions",
            "deepseek-flash",
            "fake-only",
            enabled=True,
            client=client,
            timeout=20,
        )
        model.quota = quota
        with pytest.raises(TimeoutError):
            model.decide({"text": "help", "context": {}, "history": [], "observations": []})
        assert closed and quota.calls == 1 and quota.reserved > 0
        assert model.last_error == "PROVIDER_TIMEOUT"


def test_reviewed_smoke_rejects_changed_tasks_caps_and_model_config_before_provider(monkeypatch):
    from caseflow.agent_eval import main

    configure(monkeypatch)
    monkeypatch.setenv("CASEFLOW_INPUT_PRICE_PER_M", "2")
    monkeypatch.setenv("CASEFLOW_OUTPUT_PRICE_PER_M", "8")
    monkeypatch.setenv("CASEFLOW_PRICE_CURRENCY", "CNY")

    def forbidden_provider(*args, **kwargs):
        pytest.fail("Admission rejection must happen before provider construction")

    monkeypatch.setattr("caseflow.agent_eval.configured_planner", forbidden_provider)
    common = ["eval", "--mode", "live", "--execute", "--strategy", "both", "--ids", "cancel-a,refund-b,fill-slots"]
    for tail in [
        ["--max-model-calls", "49", "--budget-amount", "5"],
        ["--budget-amount", "6"],
        ["--ids", "cancel-a", "--budget-amount", "5"],
    ]:
        monkeypatch.setattr(sys, "argv", common + tail)
        with pytest.raises(SystemExit, match="First live smoke"):
            main()
    monkeypatch.setenv("CASEFLOW_MAX_TOKENS", "1000")
    monkeypatch.setattr(sys, "argv", common + ["--budget-amount", "5"])
    with pytest.raises(SystemExit, match="Reviewed smoke requires"):
        main()
