"""Authenticated local API. The upstream demonstration API is not mounted."""

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from caseflow.contracts import Conflict, Principal


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    digest: str = Field(min_length=64, max_length=64)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=128)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool = Field(strict=True)
    version: int = Field(strict=True, ge=1)
    comment: str = Field(default="", max_length=500)


class TicketUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    version: int = Field(strict=True, ge=1)
    resolution: str = Field(default="", max_length=2000)


def create_app(service, auth, *, demo_identities=False):
    if demo_identities and not service.planner.label.startswith("mock"):
        raise ValueError("Role-play identities are only available in explicit local Mock demos")
    app = FastAPI(title="CaseFlow — SIMULATED local support", version="1.0.0")
    web = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.middleware("http")
    async def request_identity(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web / "index.html")

    @app.get("/api/v1/config")
    def configuration():
        return {
            "mode": service.planner.label,
            "model": getattr(service.planner, "model", None),
            "real_llm_verification": "not-verified",
            "demo_identities": demo_identities,
            "simulated": True,
            "budget": {
                **getattr(service.planner, "budget", {}),
                "session_request_cap": getattr(getattr(service.planner, "quota", None), "limit", None),
                "session_reserved_cny": str(getattr(getattr(service.planner, "quota", None), "reserved", 0)),
                "session_budget_cny": str(getattr(getattr(service.planner, "quota", None), "budget", "unknown")),
            },
        }

    @app.post("/api/v1/demo/session/{role}")
    def demo_session(role: str, request: Request):
        # Deliberate synthetic role-play, never enabled by the normal Agent server.
        if not demo_identities or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise Conflict("NOT_FOUND", 404)
        if role not in {"customer", "reviewer"}:
            raise Conflict("FORBIDDEN", 403)
        return {"token": auth.issue("C100" if role == "customer" else "demo-reviewer", role), "role": role}

    @app.exception_handler(Conflict)
    async def error_handler(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code})

    def actor(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            raise Conflict("AUTH_REQUIRED", 401)
        return auth.verify(authorization[7:])

    @app.get("/health")
    def health():
        return {"status": "ok", "simulated": True}

    @app.post("/api/v1/chat")
    def chat(body: ChatRequest, request: Request, principal: Annotated[Principal, Depends(actor)]):
        return service.chat(principal, body.message, body.conversation_id, request.state.request_id)

    @app.get("/api/v1/orders")
    def orders(principal: Annotated[Principal, Depends(actor)]):
        return service.repository.orders(principal)

    @app.post("/api/v1/proposals/{pid}/confirm")
    def confirm(pid: str, body: Confirmation, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.confirm(principal, pid, body.version, body.digest)

    @app.post("/api/v1/proposals/{pid}/submit")
    def submit(pid: str, body: Submission, principal: Annotated[Principal, Depends(actor)]):
        return service.submit(principal, pid, body.idempotency_key)

    @app.get("/api/v1/approvals")
    def pending(principal: Annotated[Principal, Depends(actor)]):
        return service.repository.pending(principal)

    @app.post("/api/v1/approvals/{oid}/decision")
    def decide(oid: str, body: ApprovalDecision, principal: Annotated[Principal, Depends(actor)]):
        return service.approve(principal, oid, body.approved, body.version, body.comment)

    @app.get("/api/v1/operations/{oid}")
    def operation(oid: str, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.render(principal, oid)

    @app.post("/api/v1/operations/{oid}/recover")
    def recover(oid: str, principal: Annotated[Principal, Depends(actor)]):
        return (
            service.execution.run(principal, oid) if service.execution else service.repository.recover(principal, oid)
        )

    @app.post("/api/v1/operations/{oid}/cancel")
    def cancel(oid: str, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.cancel_operation(principal, oid)

    @app.get("/api/v1/operations/{oid}/audit")
    def audit(oid: str, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.audit(principal, oid)

    @app.get("/api/v1/tickets")
    def tickets(principal: Annotated[Principal, Depends(actor)], conversation_id: str | None = None):
        return service.repository.tickets(principal, conversation_id)

    @app.get("/api/v1/tickets/{tid}")
    def ticket(tid: str, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.ticket(principal, tid)

    @app.post("/api/v1/tickets/{tid}")
    def update_ticket(tid: str, body: TicketUpdate, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.update_ticket(principal, tid, body.status, body.version, body.resolution)

    @app.get("/api/v1/traces/{rid}")
    def trace(rid: str, principal: Annotated[Principal, Depends(actor)]):
        return service.repository.trace(principal, rid)

    return app
