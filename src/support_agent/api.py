"""HTTP API.

Customers:  POST /chat, GET /chat/{thread_id}         identified by the X-Customer-Id header
Staff:      GET /approvals, POST /approvals/{thread_id}  require the X-Staff-Key header

The headers stand in for real authentication (a session or JWT for customers, SSO for staff). What
matters is that the customer id comes from the verified identity and is passed to the tools through
the run's config, never through the conversation.

Run: uvicorn support_agent.api:app --reload
"""

# No "from __future__ import annotations" here: FastAPI must resolve the Depends() inside create_app at runtime.
import hmac
import os
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from support_agent.service import ConversationError, SupportService


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, max_length=64)


class ApprovalDecision(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=500)


def create_app(service: SupportService | None = None, staff_key: str | None = None) -> FastAPI:
    service = service or SupportService(db_path=os.getenv("SUPPORT_DB"))
    staff_key = staff_key or os.getenv("STAFF_API_KEY", "dev-staff-key")
    app = FastAPI(title="Support agent API", version="0.1.0")

    def customer(x_customer_id: Annotated[str, Header()]) -> str:
        return x_customer_id

    def staff(x_staff_key: Annotated[str, Header()]) -> None:
        if not hmac.compare_digest(x_staff_key, staff_key):
            raise HTTPException(status_code=401, detail="Invalid staff key.")

    def run(action):
        try:
            return asdict(action())
        except ConversationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/chat")
    def chat(request: ChatRequest, customer_id: Annotated[str, Depends(customer)]) -> dict:
        return run(lambda: service.chat(customer_id, request.message, request.thread_id))

    @app.get("/chat/{thread_id}")
    def history(thread_id: str, customer_id: Annotated[str, Depends(customer)]) -> list[dict]:
        try:
            return service.history(customer_id, thread_id)
        except ConversationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/approvals", dependencies=[Depends(staff)])
    def approvals() -> list[dict]:
        return service.pending_approvals()

    @app.post("/approvals/{thread_id}", dependencies=[Depends(staff)])
    def decide(thread_id: str, decision: ApprovalDecision) -> dict:
        return run(lambda: service.resolve_approval(thread_id, decision.approved, decision.reviewer, decision.note))

    return app


app = create_app()
