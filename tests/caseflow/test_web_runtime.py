import json

import httpx
import pytest
from fastapi.testclient import TestClient

from caseflow.api import create_app
from caseflow.auth import Auth
from caseflow.local_server import running
from caseflow.planner import ScriptedPlanner
from caseflow.redaction import redact
from caseflow.runtime import demo_web
from caseflow.service import CaseFlow


def test_web_two_order_approval_ticket_and_trace_over_real_http(tmp_path):
    with demo_web(tmp_path) as app, running(app) as url, httpx.Client(base_url=url, timeout=20) as client:
        assert client.get("/").status_code == 200
        assert "CaseFlow" in client.get("/assets/app.js").text or "api(" in client.get("/assets/app.js").text
        assert client.get("/api/v1/config").json()["real_llm_verification"] == "not-verified"
        customer = client.post("/api/v1/demo/session/customer").json()["token"]
        staff = client.post("/api/v1/demo/session/reviewer").json()["token"]
        c = {"Authorization": "Bearer " + customer}
        r = {"Authorization": "Bearer " + staff}
        assert client.get("/api/v1/orders").status_code == 401
        response = client.post("/api/v1/chat", headers=c, json={"message": "1001退款60 EUR，1004取消"})
        turn = response.json()
        assert response.headers["X-Request-ID"] == turn["request_id"]
        assert turn["status"] == "proposals", turn
        assert len(turn["proposals"]) == 2
        assert {t.get("tool") for t in turn["trace"]} >= {
            "get_order",
            "get_logistics",
            "refund_info",
            "search_policies",
        }
        assert all(t["elapsed_s"] >= 0 for t in turn["trace"])
        for p in turn["proposals"]:
            pid = p["id"]
            assert (
                client.post(f"/api/v1/proposals/{pid}/submit", headers=c, json={"idempotency_key": pid}).status_code
                == 409
            )
            assert (
                client.post(
                    f"/api/v1/proposals/{pid}/confirm", headers=c, json={"version": p["version"], "digest": p["digest"]}
                ).status_code
                == 200
            )
            result = client.post(f"/api/v1/proposals/{pid}/submit", headers=c, json={"idempotency_key": pid}).json()
            if p["action"] == "refund":
                assert result["status"] == "awaiting_approval"
                queue = client.get("/api/v1/approvals", headers=r).json()
                waiting = queue[0]
                approved = client.post(
                    f"/api/v1/approvals/{waiting['id']}/decision",
                    headers=r,
                    json={"approved": True, "version": waiting["version"]},
                ).json()
                assert approved["receipt"]["amount_minor"] == 6000
            else:
                assert result["status"] == "committed"
        assert client.get("/api/v1/approvals", headers=c).status_code == 403
        trace = client.get("/api/v1/traces/" + turn["run_id"], headers=c).json()
        assert trace["usage"]["llm_calls"] == 0
        assert customer not in json.dumps(trace)
        handoff = client.post("/api/v1/chat", headers=c, json={"message": "快递丢失，请转人工"}).json()
        ticket = handoff["ticket"]
        path = "/api/v1/tickets/" + ticket["id"]
        assigned = client.post(path, headers=r, json={"status": "ASSIGNED", "version": ticket["version"]}).json()
        assert assigned["status"] == "ASSIGNED"
        assert (
            client.post(
                path,
                headers=r,
                json={
                    "status": "RESOLVED",
                    "version": assigned["version"],
                    "resolution": "已联系物流，请等待；未执行退款",
                },
            ).status_code
            == 200
        )
        followup = client.post(
            "/api/v1/chat", headers=c, json={"message": "工单处理结果？", "conversation_id": handoff["conversation_id"]}
        ).json()
        assert "RESOLVED" in followup["reply"]


def test_demo_auth_is_opt_in_and_never_available_for_real_model():
    auth = Auth("test-only-no-default-secret-" * 3)
    planner = ScriptedPlanner([{"kind": "answer"}])
    with TestClient(create_app(CaseFlow(None, planner), auth)) as client:
        assert client.post("/api/v1/demo/session/reviewer").status_code == 404
    planner.label = "live-api-not-verified"
    with pytest.raises(ValueError, match="Mock"):
        create_app(CaseFlow(None, planner), auth, demo_identities=True)


def test_redaction_removes_credentials_without_hiding_business_results():
    result = redact(
        {
            "authorization": "private",
            "api_key": "private",
            "result": "Bearer test.secret",
            "tokens": 12,
            "trace": ["sk-demoNotARealKey12345678"],
            "receipt": {"amount_minor": 500},
        }
    )
    assert "private" not in json.dumps(result)
    assert "test.secret" not in json.dumps(result)
    assert result["tokens"] == 12
    assert result["receipt"]["amount_minor"] == 500
