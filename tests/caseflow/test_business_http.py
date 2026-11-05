import httpx
import pytest
from http_support import running

from caseflow.auth import Auth
from caseflow.business_api import create_business_app
from caseflow.contracts import Conflict, Principal
from caseflow.execution import Execution
from caseflow.gateway import HTTPBusiness
from caseflow.planner import ScriptedPlanner
from caseflow.policy_search import PolicySearch
from caseflow.service import CaseFlow
from caseflow.support_business import SupportLedger

C = Principal("C100", "customer")
R = Principal("staff-a", "reviewer")


@pytest.fixture
def business(tmp_path):
    ledger = SupportLedger(tmp_path / "business.db")
    ledger.seed()
    execution = Execution(ledger, tmp_path / "checkpoint.db")
    auth = Auth("business-test-only-" * 4)
    key = "service-test-only-" * 4
    with running(create_business_app(ledger, execution, auth, key)) as url:
        gateway = HTTPBusiness(url, auth, key)
        try:
            yield ledger, execution, gateway, url, auth, key
        finally:
            gateway.close()
    execution.close()


def confirmed(service, message="refund 1005 5 EUR"):
    p = service.chat(C, message)["proposal"]
    service.repository.confirm(C, p["id"], p["version"], p["digest"])
    return p


def test_real_http_queries_and_same_ledger_contract(business):
    ledger, _, g, url, auth, _ = business
    assert g.order(C, "1001") == ledger.order(C, "1001")
    assert g.logistics(C, "1004")["status"] == "pending"
    assert g.refund_info(C, "1003")["allowed"] is False
    with pytest.raises(Conflict, match="NOT_FOUND"):
        g.order(C, "2001")
    # Customer bearer alone cannot forge internal Agent history/audit.
    r = httpx.post(url + "/internal/conversations", headers={"Authorization": "Bearer " + auth.issue("C100")}, json={})
    assert r.status_code == 401


def test_lost_http_response_retries_same_operation_without_second_write(business):
    ledger, _, g, url, auth, key = business
    service = CaseFlow(g)
    p = confirmed(service)

    class Lossy(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()
            self.lost = False

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            response.read()
            if request.url.path.endswith("/submit") and not self.lost:
                self.lost = True
                response.close()
                raise httpx.ReadError("response lost AFTER real server commit")
            return response

        def close(self):
            self.inner.close()

    with httpx.Client(transport=Lossy()) as client:
        retry = HTTPBusiness(url, auth, key, client)
        result = retry.submit(C, p["id"], "same-key")
        assert result["status"] == "committed"
        assert retry.submit(C, p["id"], "same-key")["receipt"] == result["receipt"]
    with ledger.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert ledger.order(C, "1005")["refunded_minor"] == 500


def test_http_approval_and_shipment_change_invalidates_old_authorization(business):
    ledger, _, g, _, _, _ = business
    p = confirmed(CaseFlow(g), "refund 1001 60 EUR")
    waiting = g.submit(C, p["id"], "approval")
    assert waiting["status"] == "awaiting_approval"
    ledger.decide(R, waiting["operation_id"], True, 1)
    g.call("POST", "/api/v1/orders/1001/logistics", R, {"status": "lost", "version": 1})
    assert g.recover(C, waiting["operation_id"])["status"] == "rejected"
    assert ledger.order(C, "1001")["refunded_minor"] == 0


def test_ticket_lifecycle_and_agent_continuation_are_real_persisted_state(business):
    ledger, _, g, _, _, _ = business
    handoff = CaseFlow(g, ScriptedPlanner([{"kind": "handoff", "message": "物流信息冲突，需要人工核实"}])).chat(
        C, "快递找不到了"
    )
    t = handoff["ticket"]
    assert t["status"] == "OPEN"
    assert ledger.ticket(C, t["id"])["request"] == "快递找不到了"
    with pytest.raises(Conflict):
        g.ticket(Principal("C200", "customer"), t["id"])
    with pytest.raises(Conflict):
        g.update_ticket(C, t["id"], "RESOLVED", 1, "伪造员工")
    assigned = g.update_ticket(R, t["id"], "ASSIGNED", 1)
    with pytest.raises(Conflict):
        g.update_ticket(Principal("staff-b", "reviewer"), t["id"], "RESOLVED", assigned["version"], "抢占")
    done = g.update_ticket(R, t["id"], "RESOLVED", assigned["version"], "已联系物流，客户选择等待；没有退款")
    planner = ScriptedPlanner([{"kind": "tool", "tool": "get_ticket", "ticket_id": t["id"]}, {"kind": "answer"}])
    response = CaseFlow(g, planner).chat(C, "现在有什么进展？", handoff["conversation_id"])
    assert "RESOLVED" in response["reply"]
    assert "没有退款" in response["reply"]
    assert SupportLedger(ledger.path).ticket(C, t["id"])["status"] == "RESOLVED"
    assert g.trace(C, response["run_id"])["run_id"] == response["run_id"]
    assert done["resolution"]


def test_ticket_idempotency_and_rejection_do_not_approve_money(business):
    ledger, _, g, _, _, _ = business
    cid = g.conversation(C)["id"]
    one = g.create_ticket(C, cid, "保修争议", "需要员工", [], [], "ticket-key")
    assert g.create_ticket(C, cid, "保修争议", "需要员工", [], [], "ticket-key")["id"] == one["id"]
    with pytest.raises(Conflict, match="IDEMPOTENCY_CONFLICT"):
        g.create_ticket(C, cid, "换请求", "需要员工", [], [], "ticket-key")
    rejected = g.update_ticket(R, one["id"], "REJECTED", 1, "不符合演示保修条件")
    assert rejected["status"] == "REJECTED"
    with ledger.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "query,heading", [("退钱能只退一部分吗", "退款"), ("快递发出还可以撤单吗", "取消"), ("保修多久", "保修")]
)
def test_chinese_lexical_policy_grounding(query, heading):
    assert heading in PolicySearch().search(query)[0][0]


def test_business_gateway_rejects_nonlocal_host():
    with pytest.raises(ValueError, match="loopback"):
        HTTPBusiness("http://example.com", None, "fake")
