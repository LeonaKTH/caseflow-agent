import json

import pytest
from fastapi.testclient import TestClient

from caseflow.api import create_app
from caseflow.auth import Auth
from caseflow.contracts import Conflict, Principal, minor_units
from caseflow.planner import ScriptedPlanner
from caseflow.repository import Repository
from caseflow.service import CaseFlow

C = Principal("C100", "customer")


@pytest.fixture
def repo(tmp_path):
    repository = Repository(tmp_path / "business.db")
    repository.seed()
    return repository


def test_necessary_clarification_and_no_redundant_question(repo):
    service = CaseFlow(repo)
    missing = service.chat(C, "我想退款")
    assert missing["status"] == "clarify"
    amount = service.chat(C, "订单1005", missing["conversation_id"])
    assert amount["status"] == "clarify"
    ready = service.chat(C, "全额", missing["conversation_id"])
    assert ready["status"] == "proposal"
    assert ready["proposal"]["amount_minor"] == 1800
    assert service.chat(C, "cancel order 1004")["status"] == "proposal"


def test_max_two_questions(repo):
    service = CaseFlow(repo)
    result = service.chat(C, "refund")
    for _ in range(2):
        result = service.chat(C, "refund", result["conversation_id"])
    assert result["status"] == "handoff"
    assert result["clarification_round"] == 2


def test_ambiguous_pair_yesterday_and_largest(repo):
    service = CaseFlow(repo)
    assert service.chat(C, "退款1001和1005")["status"] == "clarify"
    p = service.chat(C, "1001和1005金额较大的那个全额退款")["proposal"]
    assert p["order_id"] == "1001"
    assert service.chat(C, "帮我取消昨天的订单")["proposal"]["order_id"] == "1004"


def test_version_and_intent_changes_invalidate_old_confirmation(repo):
    service = CaseFlow(repo)
    r = service.chat(C, "refund order 1005 5 EUR")
    p = r["proposal"]
    assert repo.confirm(C, p["id"], p["version"], p["digest"])["status"] == "CONFIRMED"
    new = service.chat(C, "改退1001 10 EUR", r["conversation_id"])["proposal"]
    assert new["version"] == 2
    with pytest.raises(Conflict, match="NOT_ACTIVE"):
        repo.confirm(C, p["id"], p["version"], p["digest"])


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-1", "0", "1.001", "1.0000000000000000000000000000001", 1.5, True]
)
def test_invalid_money(value):
    with pytest.raises(Conflict):
        minor_units(value)


def test_expiration_snapshot_and_currency(repo):
    service = CaseFlow(repo)
    p = service.chat(C, "refund 1005 5 EUR")["proposal"]
    with repo.tx() as db:
        db.execute("UPDATE orders SET version=version+1 WHERE id='1005'")
    with pytest.raises(Conflict, match="ORDER_CHANGED"):
        repo.confirm(C, p["id"], p["version"], p["digest"])
    p = service.chat(C, "refund 1005 5 EUR")["proposal"]
    repo.clock = lambda: 10**12
    with pytest.raises(Conflict, match="EXPIRED"):
        repo.confirm(C, p["id"], p["version"], p["digest"])
    repo.clock = lambda: 1
    assert service.chat(C, "1005退款5元")["status"] == "refused"


def test_model_cannot_assert_order_evidence_or_overwrite_identity(repo):
    model = ScriptedPlanner([{"kind": "proposal", "action": "refund", "order_id": "2001", "amount": "1"}])
    result = CaseFlow(repo, model).chat(C, "anything")
    assert result["status"] == "handoff"
    assert result["reply"] == "ORDER_EVIDENCE_REQUIRED"
    loop = ScriptedPlanner([{"kind": "tool", "tool": "list_orders"}])
    bounded = CaseFlow(repo, loop).chat(C, "anything")
    assert bounded["usage"]["planner_calls"] == 5
    assert bounded["status"] == "handoff"


def test_api_auth_and_cross_customer_boundaries(repo):
    auth = Auth("test-only-" * 8)
    c = TestClient(create_app(CaseFlow(repo), auth))
    customer = {"Authorization": "Bearer " + auth.issue("C100")}
    other = {"Authorization": "Bearer " + auth.issue("C200")}
    staff = {"Authorization": "Bearer " + auth.issue("staff", "reviewer")}
    assert c.get("/api/v1/orders", headers={"X-Customer-Id": "C100"}).status_code == 401
    assert c.get("/api/v1/orders", headers={"Authorization": customer["Authorization"] + "x"}).status_code == 401
    assert (
        c.get("/api/v1/orders", headers={"Authorization": "Bearer " + auth.issue("C100", lifetime=-1)}).status_code
        == 401
    )
    assert c.get("/api/v1/orders", headers=staff).status_code == 403
    r = c.post("/api/v1/chat", headers=customer, json={"message": "refund 1005 5 EUR"}).json()
    p = r["proposal"]
    assert (
        c.post(
            "/api/v1/chat", headers=other, json={"message": "help", "conversation_id": r["conversation_id"]}
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/v1/proposals/{p['id']}/confirm", headers=other, json={"version": p["version"], "digest": p["digest"]}
        ).status_code
        == 404
    )
    assert (
        c.post(
            f"/api/v1/proposals/{p['id']}/confirm",
            headers=customer,
            json={"version": p["version"] + 1, "digest": p["digest"]},
        ).status_code
        == 409
    )
    assert c.post("/api/v1/chat", headers=customer, json={"message": "hi", "role": "reviewer"}).status_code == 422
    assert CaseFlow(repo).chat(C, "refund 2001 full")["reply"] == "NOT_FOUND"
    assert json.loads(repo.conversation(C, r["conversation_id"])["context"])["order_id"] == "1005"
