"""Shared application contracts. Models cannot carry authentication or approval."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_LLM_CALLS = 5
MAX_TOOL_CALLS = 6
MAX_CLARIFICATIONS = 2
POLICY_VERSION = "demo-eur-v1"


class Conflict(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Principal:
    subject: str
    role: Literal["customer", "reviewer"]

    def require(self, role):
        if self.role != role:
            raise Conflict("ROLE_DENIED", 403)


class ProposalIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["refund", "cancel"]
    order_id: str
    amount: str | None = None
    currency: str = "EUR"
    full_refund: bool = False


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["tool", "proposal", "proposals", "clarify", "answer", "handoff", "refuse"]
    tool: (
        Literal["get_order", "list_orders", "search_policies", "get_logistics", "refund_info", "get_ticket"] | None
    ) = None
    order_id: str | None = None
    query: str = Field(default="", max_length=2000)
    action: Literal["refund", "cancel"] | None = None
    amount: str | None = None
    currency: str = "EUR"
    full_refund: bool = False
    message: str = Field(default="", max_length=2000)
    ticket_id: str | None = None
    proposals: list[ProposalIntent] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_tool(self):
        if self.kind == "tool":
            if self.tool is None:
                raise ValueError("tool is required")
            if self.tool in {"get_order", "get_logistics", "refund_info"} and not self.order_id:
                raise ValueError("order_id is required")
            if self.tool == "get_ticket" and not self.ticket_id:
                raise ValueError("ticket_id is required")
        if self.kind == "proposals" and not self.proposals:
            raise ValueError("proposals must not be empty")
        if self.kind == "proposal" and (
            not self.order_id or self.action is None or self.proposals or self.tool is not None
        ):
            raise ValueError("single proposal requires top-level order_id/action and no proposals list")
        if self.kind == "proposals" and (
            self.order_id is not None
            or self.action is not None
            or self.amount is not None
            or self.full_refund
            or self.tool is not None
        ):
            raise ValueError("batch proposals must carry their order/action/amount inside the list")
        return self


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def minor_units(value):
    if isinstance(value, (float, bool)):
        raise Conflict("AMOUNT_MUST_BE_DECIMAL_STRING", 422)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
            raise ValueError
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, TypeError):
        raise Conflict("INVALID_AMOUNT", 422) from None
