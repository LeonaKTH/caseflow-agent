"""The shop's data: customers, orders, refunds, cancellations and escalation tickets.

In-memory and thread-safe for the demo; in production these are calls to your order system and CRM.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass
class Order:
    id: str
    customer_id: str
    item: str
    total: Decimal
    currency: str
    status: str  # processing | shipped | delivered | cancelled
    ordered: date
    delivered: date | None = None
    tracking: str | None = None
    eta: date | None = None
    refunded: Decimal = Decimal("0")


@dataclass(frozen=True)
class Refund:
    id: str
    order_id: str
    amount: Decimal
    reason: str
    approved_by: str  # "policy" for automatic refunds, otherwise the reviewer


@dataclass
class Store:
    today: date
    customers: dict[str, dict]
    orders: dict[str, Order]
    refunds: list[Refund] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, path: str | Path, today: date | None = None) -> Store:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        def as_date(value: str | None) -> date | None:
            return date.fromisoformat(value) if value else None

        orders = {
            o["id"]: Order(
                id=o["id"],
                customer_id=o["customer_id"],
                item=o["item"],
                total=Decimal(o["total"]),
                currency=o["currency"],
                status=o["status"],
                ordered=as_date(o["ordered"]),
                delivered=as_date(o.get("delivered")),
                tracking=o.get("tracking"),
                eta=as_date(o.get("eta")),
            )
            for o in raw["orders"]
        }
        return cls(today=today or date.fromisoformat(raw["demo_today"]), customers=raw["customers"], orders=orders)

    def order_for(self, customer_id: str, order_id: str) -> Order | None:
        """Only returns the order if it belongs to this customer. Other customers' orders don't 'exist'."""
        order = self.orders.get(str(order_id).strip().lstrip("#"))
        return order if order and order.customer_id == customer_id else None

    def orders_for(self, customer_id: str) -> list[Order]:
        return sorted((o for o in self.orders.values() if o.customer_id == customer_id), key=lambda o: o.ordered)

    def record_refund(self, order: Order, amount: Decimal, reason: str, approved_by: str) -> Refund:
        with self._lock:
            if order.refunded > 0:
                raise ValueError(f"Order {order.id} has already been refunded.")
            refund = Refund(f"R-{len(self.refunds) + 1:04d}", order.id, amount, reason, approved_by)
            order.refunded = amount
            self.refunds.append(refund)
            return refund

    def cancel(self, order: Order) -> None:
        with self._lock:
            order.status = "cancelled"

    def open_ticket(self, customer_id: str, summary: str) -> str:
        with self._lock:
            ticket_id = f"T-{len(self.tickets) + 1:04d}"
            self.tickets.append({"id": ticket_id, "customer_id": customer_id, "summary": summary})
            return ticket_id
