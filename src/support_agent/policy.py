"""Refund and cancellation rules. Plain code, so no prompt can talk its way around them."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from support_agent.store import Order


@dataclass(frozen=True)
class PolicyConfig:
    return_window_days: int = 30
    approval_threshold: Decimal = Decimal("50.00")  # refunds above this need a human


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str
    max_amount: Decimal = Decimal("0")


def refund_eligibility(order: Order, today, config: PolicyConfig) -> Eligibility:
    if order.refunded > 0:
        return Eligibility(False, f"Order {order.id} was already refunded ({order.refunded} {order.currency}).")
    if order.status == "cancelled":
        return Eligibility(False, "Cancelled orders are refunded automatically when cancelled.")
    if order.status != "delivered" or order.delivered is None:
        return Eligibility(
            False,
            f"Order {order.id} hasn't been delivered yet (status: {order.status}). It can be returned once it arrives.",
        )
    days = (today - order.delivered).days
    if days > config.return_window_days:
        return Eligibility(
            False,
            f"Order {order.id} was delivered {days} days ago; the return window is {config.return_window_days} days.",
        )
    return Eligibility(
        True, f"Delivered {days} days ago, within the {config.return_window_days}-day window.", max_amount=order.total
    )


def needs_approval(amount: Decimal, config: PolicyConfig) -> bool:
    return amount > config.approval_threshold


def can_cancel(order: Order) -> tuple[bool, str]:
    if order.status == "processing":
        return True, "The order hasn't shipped yet."
    if order.status == "cancelled":
        return False, "The order is already cancelled."
    return False, f"The order is already {order.status}, so it can't be cancelled. It can be returned after delivery."
