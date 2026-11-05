from fastapi.testclient import TestClient

from support_agent.api import create_app
from support_agent.service import SupportService

STAFF = {"X-Staff-Key": "test-key"}


def client() -> TestClient:
    return TestClient(create_app(SupportService(), staff_key="test-key"))


def test_chat_and_history():
    c = client()
    reply = c.post("/chat", json={"message": "Where is order 1002?"}, headers={"X-Customer-Id": "C100"}).json()

    assert reply["status"] == "answered"
    history = c.get(f"/chat/{reply['thread_id']}", headers={"X-Customer-Id": "C100"}).json()
    assert history[0] == {"role": "customer", "text": "Where is order 1002?"}
    assert c.get(f"/chat/{reply['thread_id']}", headers={"X-Customer-Id": "C200"}).status_code == 404


def test_refund_approval_flow_over_http():
    c = client()
    waiting = c.post(
        "/chat", json={"message": "Refund order 1001, it's damaged"}, headers={"X-Customer-Id": "C100"}
    ).json()
    assert waiting["status"] == "awaiting_approval"

    queue = c.get("/approvals", headers=STAFF).json()
    assert queue[0]["thread_id"] == waiting["thread_id"]

    done = c.post(f"/approvals/{waiting['thread_id']}", json={"approved": True, "reviewer": "maria"}, headers=STAFF)
    assert "refunded 89.00 EUR" in done.json()["reply"]


def test_staff_endpoints_need_the_staff_key():
    c = client()
    assert c.get("/approvals", headers={"X-Staff-Key": "wrong"}).status_code == 401
    assert c.get("/approvals").status_code == 422


def test_unknown_customers_are_rejected():
    assert client().post("/chat", json={"message": "hi"}, headers={"X-Customer-Id": "C999"}).status_code == 401


def test_all_offline_scenarios_pass():
    from support_agent.evaluate import run_scenarios

    failed = [r["name"] for r in run_scenarios("offline") if not r["passed"]]
    assert not failed
