import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from caseflow.api import create_app
from caseflow.auth import Auth
from caseflow.contracts import Conflict, Principal
from caseflow.execution import Execution
from caseflow.ledger import Ledger
from caseflow.service import CaseFlow

C = Principal("C100", "customer")
R = Principal("reviewer-a", "reviewer")


@pytest.fixture
def app(tmp_path):
    ledger = Ledger(tmp_path / "business.db")
    ledger.seed()
    execution = Execution(ledger, tmp_path / "checkpoint.db")
    service = CaseFlow(ledger, execution=execution)
    yield service
    execution.close()


def proposal(app, message="refund 1005 full"):
    p = app.chat(C, message)["proposal"]
    app.repository.confirm(C, p["id"], p["version"], p["digest"])
    return p


def count(ledger):
    with ledger.tx() as db:
        return db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


def test_unconfirmed_cannot_submit_and_small_refund_durable(app):
    p = app.chat(C, "refund 1005 full")["proposal"]
    with pytest.raises(Conflict, match="CONFIRMATION_REQUIRED"):
        app.submit(C, p["id"], "small")
    app.repository.confirm(C, p["id"], p["version"], p["digest"])
    result = app.submit(C, p["id"], "small")
    assert result["status"] == "committed"
    assert result["receipt"]["amount_minor"] == 1800
    assert "SIMULATED" in result["reply"]
    restarted = Ledger(app.repository.path)
    assert restarted.order(C, "1005")["refunded_minor"] == 1800
    assert count(restarted) == 1
    assert app.submit(C, p["id"], "small")["receipt"] == result["receipt"]
    with pytest.raises(Conflict, match="OPERATION_EXISTS"):
        app.submit(C, p["id"], "new-key")
    assert app.chat(C, "refund 1005 full")["status"] == "refused"


def test_idempotency_key_content_conflict(app):
    p = proposal(app)
    app.submit(C, p["id"], "key")
    p2 = proposal(app, "cancel 1004")
    with pytest.raises(Conflict, match="IDEMPOTENCY_CONFLICT"):
        app.submit(C, p2["id"], "key")
    assert app.repository.order(C, "1004")["status"] == "processing"


def test_partial_refund_cannot_be_repeated_with_another_proposal(app):
    p1 = proposal(app, "refund 1005 5 EUR")
    p2 = proposal(app, "refund 1005 5 EUR")
    app.submit(C, p1["id"], "one")
    with pytest.raises(Conflict):
        app.submit(C, p2["id"], "two")
    assert app.repository.order(C, "1005")["refunded_minor"] == 500
    assert count(app.repository) == 1


def test_graph_really_pauses_and_saved_approval_resumes(app):
    p = proposal(app, "refund 1001 full")
    waiting = app.submit(C, p["id"], "large")
    oid = waiting["operation_id"]
    assert waiting["status"] == "awaiting_approval"
    assert app.execution.graph.get_state({"configurable": {"thread_id": oid}}).interrupts
    assert count(app.repository) == 0
    # A forged graph resume carries no approval authority.
    app.execution.graph.invoke(Command(resume={"approved": True}), {"configurable": {"thread_id": oid}})
    assert count(app.repository) == 0
    done = app.approve(R, oid, True, 1)
    assert done["status"] == "committed"
    assert app.repository.approval(oid)["reviewer"] == R.subject
    assert app.repository.approval(oid)["consumed"] == 1


def test_two_reviewers_cannot_both_resolve(app):
    p = proposal(app, "refund 1001 full")
    oid = app.submit(C, p["id"], "large")["operation_id"]

    def decide(name):
        try:
            Ledger(app.repository.path).decide(Principal(name, "reviewer"), oid, True, 1)
            return "ok"
        except Conflict as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(decide, ["staff-a", "staff-b"]))
    assert sorted(results) == ["VERSION_CONFLICT", "ok"]
    assert app.execution.run(C, oid)["status"] == "committed"
    assert count(app.repository) == 1


def test_two_separate_connections_cannot_over_refund(app):
    p1 = proposal(app)
    p2 = proposal(app)
    op1 = app.repository.prepare(C, p1["id"], "a")
    op2 = app.repository.prepare(C, p2["id"], "b")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda op: Ledger(app.repository.path).execute(op["id"]), [op1, op2]))
    assert sorted(r["status"] for r in results) == ["committed", "rejected"]
    assert count(app.repository) == 1
    assert app.repository.order(C, "1005")["refunded_minor"] == 1800


def test_old_approval_invalid_after_order_change(app):
    p = proposal(app, "refund 1001 full")
    oid = app.submit(C, p["id"], "large")["operation_id"]
    app.repository.decide(R, oid, True, 1)
    with app.repository.tx() as db:
        db.execute("UPDATE orders SET total_minor=total_minor+100,version=version+1 WHERE id='1001'")
    assert app.execution.run(C, oid)["status"] == "rejected"
    assert count(app.repository) == 0


def test_denial_self_approval_and_old_proposal(app):
    p = proposal(app, "refund 1001 full")
    oid = app.submit(C, p["id"], "large")["operation_id"]
    with pytest.raises(Conflict, match="SELF_APPROVAL"):
        app.repository.decide(Principal("C100", "reviewer"), oid, True, 1)
    assert app.approve(R, oid, False, 1)["status"] == "rejected"
    assert count(app.repository) == 0


def test_transaction_rollback_then_recovery(app):
    p = proposal(app)
    op = app.repository.prepare(C, p["id"], "rollback")

    def fault(point):
        if point == "before_commit":
            raise RuntimeError("injected")

    app.repository.fault = fault
    with pytest.raises(RuntimeError):
        app.execution.run(C, op["id"])
    assert count(app.repository) == 0
    assert app.repository.order(C, "1005")["refunded_minor"] == 0
    app.repository.fault = lambda point: None
    assert app.execution.run(C, op["id"])["status"] == "committed"
    assert count(app.repository) == 1


@pytest.mark.parametrize("point", ["after_approval_saved", "after_commit", "before_commit"])
def test_real_process_exit_at_business_checkpoint_gaps(app, point):
    p = proposal(app, "refund 1001 full" if point == "after_approval_saved" else "refund 1005 full")
    if point == "after_approval_saved":
        oid = app.submit(C, p["id"], "exit")["operation_id"]
    else:
        oid = app.repository.prepare(C, p["id"], "exit")["id"]
    code = """
import os, sys
from caseflow.ledger import Ledger
from caseflow.contracts import Principal
from caseflow.execution import Execution
path, checkpoint, oid, point = sys.argv[1:]
def crash(here):
    if here == point: os._exit(73)
ledger=Ledger(path, fault=crash)
if point == 'after_approval_saved':
    ledger.decide(Principal('staff-process','reviewer'),oid,True,1)
else:
    Execution(ledger,checkpoint).run(Principal('C100','customer'),oid)
"""
    env = {**os.environ, "LANGSMITH_TRACING": "false", "SUPPORT_MODEL_PROVIDER": "offline"}
    child = subprocess.run(
        [sys.executable, "-c", code, app.repository.path, str(app.repository.path) + ".child-checkpoint", oid, point],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    if point == "after_approval_saved":
        assert app.repository.approval(oid)["status"] == "APPROVED"
        assert count(app.repository) == 0
    elif point == "after_commit":
        assert count(app.repository) == 1
    else:
        assert count(app.repository) == 0
    assert app.execution.run(C, oid)["status"] == "committed"
    assert app.execution.run(C, oid)["status"] == "committed"
    assert count(app.repository) == 1


def test_cancel_and_commit_race_is_serialized(app):
    p = proposal(app)
    oid = app.repository.prepare(C, p["id"], "race")["id"]

    def cancel():
        try:
            return Ledger(app.repository.path).cancel_operation(C, oid)["status"]
        except Conflict:
            return "terminal"

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(cancel)
        b = pool.submit(lambda: Ledger(app.repository.path).execute(oid))
        cancelled, executed = a.result(), b.result()
    assert (cancelled, executed["status"]) in {("cancelled", "rejected"), ("terminal", "committed")}
    assert count(app.repository) == (executed["status"] == "committed")


def test_api_approval_identity_and_receipt_consistency(app):
    auth = Auth("ephemeral-test-" * 4)
    client = TestClient(create_app(app, auth))
    customer = {"Authorization": "Bearer " + auth.issue("C100")}
    reviewer = {"Authorization": "Bearer " + auth.issue("signed-reviewer", "reviewer")}
    other = {"Authorization": "Bearer " + auth.issue("C200")}
    p = proposal(app, "refund 1001 full")
    waiting = client.post(
        f"/api/v1/proposals/{p['id']}/submit", headers=customer, json={"idempotency_key": "http"}
    ).json()
    oid = waiting["operation_id"]
    assert client.get(f"/api/v1/operations/{oid}", headers=other).status_code == 404
    assert (
        client.post(
            f"/api/v1/approvals/{oid}/decision", headers=customer, json={"approved": True, "version": 1}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/approvals/{oid}/decision",
            headers=reviewer,
            json={"approved": True, "version": 1, "reviewer": "forged"},
        ).status_code
        == 422
    )
    result = client.post(
        f"/api/v1/approvals/{oid}/decision", headers=reviewer, json={"approved": True, "version": 1}
    ).json()
    assert result["receipt"]["operation_id"] == oid
    assert result["status"] == "committed"
    assert app.repository.approval(oid)["reviewer"] == "signed-reviewer"
    assert client.get(f"/api/v1/operations/{oid}/audit", headers=customer).json()


def test_audit_is_append_only(app):
    proposal(app)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), app.repository.tx() as db:
        db.execute("DELETE FROM audit_events")


def test_expired_or_superseded_pending_operation_reconciles_to_rejection(app):
    p = proposal(app, "refund 1001 full")
    oid = app.submit(C, p["id"], "expiring")["operation_id"]
    app.repository.clock = lambda: 10**12
    assert app.execution.run(C, oid)["status"] == "rejected"
    assert count(app.repository) == 0


def test_changed_intent_prevents_old_approved_operation_from_committing(app):
    result = app.chat(C, "refund 1001 full")
    p = result["proposal"]
    app.repository.confirm(C, p["id"], p["version"], p["digest"])
    oid = app.submit(C, p["id"], "changed")["operation_id"]
    app.repository.decide(R, oid, True, 1)
    app.chat(C, "改退1005 5 EUR", result["conversation_id"])
    assert app.execution.run(C, oid)["status"] == "rejected"
    assert count(app.repository) == 0
