"""Independent local business HTTP service; owns Ledger + Execution exclusively."""

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from caseflow.api import ApprovalDecision, Confirmation, Submission
from caseflow.contracts import Conflict, Decision, Principal


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str | None = None
    decision: Decision | None = None
    supersede: bool = True
    context: dict = Field(default_factory=dict)
    text: str = Field(default="", max_length=2000)
    result: dict = Field(default_factory=dict)


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    request: str = Field(max_length=2000)
    reason: str = Field(max_length=500)
    evidence: list = Field(default_factory=list, max_length=20)
    order_ids: list[str] = Field(default_factory=list, max_length=10)
    key: str = Field(min_length=1, max_length=128)


class StateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    version: int = Field(strict=True, ge=1)
    resolution: str = Field(default="", max_length=2000)


def create_business_app(ledger, execution, auth, service_key):
    if len(service_key) < 32:
        raise ValueError("Business service key must contain at least32 characters")
    app = FastAPI(title="CaseFlow business service — simulated", version="1.0.0")

    @app.exception_handler(Conflict)
    async def error(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code})

    def actor(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise Conflict("AUTH_REQUIRED", 401)
        return auth.verify(authorization[7:])

    def internal(x_caseflow_service: Annotated[str | None, Header()] = None):
        if not x_caseflow_service or not hmac.compare_digest(x_caseflow_service, service_key):
            raise Conflict("SERVICE_AUTH_REQUIRED", 401)

    @app.get("/health")
    def health():
        return {"service": "business", "simulated": True}

    @app.get("/api/v1/metadata")
    def metadata():
        return {"today": ledger.today(), "policy_version": "demo-eur-v1"}

    @app.get("/api/v1/orders")
    def orders(a: Annotated[Principal, Depends(actor)]):
        return ledger.orders(a)

    @app.get("/api/v1/orders/{oid}")
    def order(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.order(a, oid)

    @app.get("/api/v1/orders/{oid}/logistics")
    def logistics(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.logistics(a, oid)

    @app.get("/api/v1/orders/{oid}/refund-info")
    def refund_info(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.refund_info(a, oid)

    @app.post("/api/v1/orders/{oid}/logistics")
    def change(oid: str, body: StateUpdate, a: Annotated[Principal, Depends(actor)]):
        return ledger.change_logistics(a, oid, body.status, body.version)

    @app.post("/internal/conversations", dependencies=[Depends(internal)])
    def conversation(body: Body, a: Annotated[Principal, Depends(actor)]):
        return ledger.conversation(a, body.conversation_id)

    @app.post("/internal/conversations/{cid}/invalidate", dependencies=[Depends(internal)])
    def invalidate(cid: str, a: Annotated[Principal, Depends(actor)]):
        ledger.conversation(a, cid)
        ledger.invalidate(a, cid)
        return {"ok": True}

    @app.post("/internal/conversations/{cid}/clarify", dependencies=[Depends(internal)])
    def clarify(cid: str, body: Body, a: Annotated[Principal, Depends(actor)]):
        return ledger.clarify(a, cid, body.context)

    @app.get("/internal/conversations/{cid}/history", dependencies=[Depends(internal)])
    def history(cid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.history(a, cid)

    @app.post("/internal/conversations/{cid}/record", dependencies=[Depends(internal)])
    def record(cid: str, body: Body, a: Annotated[Principal, Depends(actor)]):
        ledger.record_turn(a, cid, body.text, body.result)
        return {"ok": True}

    @app.post("/internal/proposals", dependencies=[Depends(internal)])
    def proposal(body: Body, a: Annotated[Principal, Depends(actor)]):
        if body.decision is None or body.conversation_id is None:
            raise Conflict("PROPOSAL_ARGUMENTS_REQUIRED", 422)
        return ledger.propose(a, body.conversation_id, body.decision, body.supersede)

    @app.post("/api/v1/proposals/{pid}/confirm")
    def confirm(pid: str, body: Confirmation, a: Annotated[Principal, Depends(actor)]):
        return ledger.confirm(a, pid, body.version, body.digest)

    @app.post("/api/v1/proposals/{pid}/submit")
    def submit(pid: str, body: Submission, a: Annotated[Principal, Depends(actor)]):
        op = ledger.prepare(a, pid, body.idempotency_key)
        return execution.run(a, op["id"])

    @app.get("/api/v1/approvals")
    def approvals(a: Annotated[Principal, Depends(actor)]):
        return ledger.pending(a)

    @app.post("/api/v1/approvals/{oid}/decision")
    def approve(oid: str, body: ApprovalDecision, a: Annotated[Principal, Depends(actor)]):
        ledger.decide(a, oid, body.approved, body.version, body.comment)
        return execution.run(a, oid)

    @app.get("/api/v1/operations/{oid}")
    def operation(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.render(a, oid)

    @app.post("/api/v1/operations/{oid}/recover")
    def recover(oid: str, a: Annotated[Principal, Depends(actor)]):
        return execution.run(a, oid)

    @app.post("/api/v1/operations/{oid}/cancel")
    def cancel(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.cancel_operation(a, oid)

    @app.get("/api/v1/operations/{oid}/audit")
    def audit(oid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.audit(a, oid)

    @app.post("/api/v1/tickets")
    def ticket_create(body: TicketRequest, a: Annotated[Principal, Depends(actor)]):
        return ledger.create_ticket(
            a, body.conversation_id, body.request, body.reason, body.evidence, body.order_ids, body.key
        )

    @app.get("/api/v1/tickets")
    def tickets(a: Annotated[Principal, Depends(actor)], conversation_id: str | None = None):
        return ledger.tickets(a, conversation_id)

    @app.get("/api/v1/tickets/{tid}")
    def ticket(tid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.ticket(a, tid)

    @app.post("/api/v1/tickets/{tid}")
    def update(tid: str, body: StateUpdate, a: Annotated[Principal, Depends(actor)]):
        return ledger.update_ticket(a, tid, body.status, body.version, body.resolution)

    @app.get("/api/v1/traces/{rid}")
    def trace(rid: str, a: Annotated[Principal, Depends(actor)]):
        return ledger.trace(a, rid)

    return app
