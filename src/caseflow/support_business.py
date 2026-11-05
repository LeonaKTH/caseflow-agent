"""Additive support domain. Uses the SAME existing Ledger and business database."""

import json
import uuid

from caseflow.contracts import Conflict, canonical, digest, minor_units
from caseflow.ledger import Ledger
from support_agent.service import default_data_dir


class SupportLedger(Ledger):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS shipments(order_id TEXT PRIMARY KEY REFERENCES orders(id),
                tracking TEXT, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                conversation_id TEXT NOT NULL, request TEXT NOT NULL, reason TEXT NOT NULL,
                evidence TEXT NOT NULL, order_ids TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
                assignee TEXT, resolution TEXT, version INTEGER NOT NULL DEFAULT 1,
                idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
                UNIQUE(owner,idempotency_key));
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_conversation
                ON tickets(conversation_id) WHERE status IN ('OPEN','ASSIGNED');
            CREATE TABLE IF NOT EXISTS agent_traces(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                conversation_id TEXT NOT NULL, result TEXT NOT NULL);
            """)

    def seed(self):
        super().seed()
        data = json.loads((default_data_dir() / "business_extra.json").read_text(encoding="utf-8"))
        with self.tx() as db:
            for o in data["orders"]:
                db.execute(
                    "INSERT OR IGNORE INTO orders(id,owner,total_minor,currency,status,ordered,delivered,item) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        o["id"],
                        o["owner"],
                        minor_units(o["total"]),
                        "EUR",
                        o["status"],
                        o["ordered"],
                        o.get("delivered"),
                        o["item"],
                    ),
                )
            for o in db.execute("SELECT * FROM orders").fetchall():
                db.execute(
                    "INSERT OR IGNORE INTO shipments(order_id,tracking,status) VALUES(?,?,?)",
                    (
                        o["id"],
                        "DEMO-" + o["id"] if o["status"] != "processing" else None,
                        "pending" if o["status"] == "processing" else o["status"],
                    ),
                )

    def logistics(self, actor, order_id):
        self.order(actor, order_id)
        with self.tx() as db:
            return dict(db.execute("SELECT * FROM shipments WHERE order_id=?", (order_id,)).fetchone())

    def refund_info(self, actor, order_id):
        with self.tx() as db:
            o = self._order(db, actor.subject, order_id)
            actor.require("customer")
            try:
                self._eligible(db, o, "refund")
                allowed, reason = True, "DEMO_RETURN_WINDOW"
            except Conflict as error:
                allowed, reason = False, error.code
            return {
                "order_id": order_id,
                "allowed": allowed,
                "reason": reason,
                "remaining_minor": o["total_minor"] - o["refunded_minor"],
                "currency": o["currency"],
                "approval_threshold_minor": 5000,
                "policy_version": "demo-eur-v1",
            }

    def change_logistics(self, actor, order_id, status, version):
        actor.require("reviewer")
        if status not in {"pending", "shipped", "delivered", "lost"}:
            raise Conflict("INVALID_SHIPMENT_STATUS", 422)
        with self.tx() as db:
            s = db.execute("SELECT * FROM shipments WHERE order_id=?", (order_id,)).fetchone()
            if not s:
                raise Conflict("NOT_FOUND", 404)
            if s["version"] != version:
                raise Conflict("VERSION_CONFLICT")
            db.execute("UPDATE shipments SET status=?,version=version+1 WHERE order_id=?", (status, order_id))
            order_status = "processing" if status == "pending" else "shipped" if status == "lost" else status
            today = db.execute("SELECT value FROM metadata WHERE key='today'").fetchone()[0]
            db.execute(
                "UPDATE orders SET status=?,delivered=?,version=version+1 WHERE id=?",
                (order_status, today if status == "delivered" else None, order_id),
            )
            self.event(db, "shipment_changed", actor.subject, order_id, {"status": status, "version": version})
            return {"order_id": order_id, "status": status, "version": version + 1}

    def create_ticket(self, actor, cid, request, reason, evidence, order_ids, key):
        from caseflow.redaction import redact

        request, reason, evidence = redact(request), redact(reason), redact(evidence)
        actor.require("customer")
        self.conversation(actor, cid)
        body = {
            "conversation_id": cid,
            "request": request,
            "reason": reason,
            "evidence": evidence,
            "order_ids": order_ids,
        }
        with self.tx() as db:
            old = db.execute(
                "SELECT * FROM tickets WHERE owner=? AND idempotency_key=?", (actor.subject, key)
            ).fetchone()
            if old:
                if old["request_digest"] != digest(body):
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(old)
            for oid in order_ids:
                self._order(db, actor.subject, oid)
            current = db.execute(
                "SELECT * FROM tickets WHERE conversation_id=? AND status IN ('OPEN','ASSIGNED')", (cid,)
            ).fetchone()
            if current:
                return dict(current)
            tid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO tickets(id,owner,conversation_id,request,reason,evidence,order_ids,idempotency_key,request_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    actor.subject,
                    cid,
                    request[:2000],
                    reason[:500],
                    canonical(evidence),
                    canonical(order_ids),
                    key,
                    digest(body),
                ),
            )
            self.event(db, "ticket_opened", actor.subject, tid, {"conversation_id": cid, "reason": reason})
            return dict(db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone())

    def ticket(self, actor, tid):
        with self.tx() as db:
            row = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not row or (actor.role == "customer" and row["owner"] != actor.subject):
                raise Conflict("NOT_FOUND", 404)
            if actor.role not in {"customer", "reviewer"}:
                raise Conflict("ROLE_DENIED", 403)
            return dict(row)

    def tickets(self, actor, cid=None):
        with self.tx() as db:
            if actor.role == "customer":
                return [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM tickets WHERE owner=? AND (? IS NULL OR conversation_id=?) ORDER BY rowid",
                        (actor.subject, cid, cid),
                    )
                ]
            actor.require("reviewer")
            return [dict(r) for r in db.execute("SELECT * FROM tickets ORDER BY rowid")]

    def update_ticket(self, actor, tid, status, version, resolution=""):
        actor.require("reviewer")
        with self.tx() as db:
            t = db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
            if not t:
                raise Conflict("NOT_FOUND", 404)
            allowed = (t["status"] == "OPEN" and status in {"ASSIGNED", "REJECTED"}) or (
                t["status"] == "ASSIGNED" and t["assignee"] == actor.subject and status in {"RESOLVED", "REJECTED"}
            )
            if t["version"] != version or not allowed:
                raise Conflict("TICKET_VERSION_OR_TRANSITION_CONFLICT")
            if status in {"RESOLVED", "REJECTED"} and not resolution.strip():
                raise Conflict("RESOLUTION_REQUIRED", 422)
            db.execute(
                "UPDATE tickets SET status=?,assignee=?,resolution=?,version=version+1 WHERE id=?",
                (status, actor.subject, resolution[:2000], tid),
            )
            self.event(db, "ticket_updated", actor.subject, tid, {"status": status, "version": version + 1})
            return dict(db.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone())

    def record_turn(self, actor, cid, text, result):
        from caseflow.redaction import redact

        text, result = redact(text), redact(result)
        self.conversation(actor, cid)
        with self.tx() as db:
            if db.execute("SELECT 1 FROM agent_traces WHERE id=?", (result["run_id"],)).fetchone():
                return
            db.execute(
                "INSERT INTO agent_traces VALUES(?,?,?,?)", (result["run_id"], actor.subject, cid, canonical(result))
            )
            db.execute("INSERT INTO messages(conversation_id,role,content) VALUES(?,'user',?)", (cid, text[:2000]))
            db.execute(
                "INSERT INTO messages(conversation_id,role,content) VALUES(?,'assistant',?)",
                (cid, canonical({k: v for k, v in result.items() if k != "trace"})[:8000]),
            )
            self.event(
                db,
                "turn_finished",
                actor.subject,
                result["run_id"],
                {
                    "conversation_id": cid,
                    "status": result["status"],
                    "trace": result["trace"],
                    "usage": result["usage"],
                },
            )

    def trace(self, actor, run_id):
        if actor.role not in {"customer", "reviewer"}:
            raise Conflict("FORBIDDEN", 403)
        with self.tx() as db:
            r = db.execute("SELECT * FROM agent_traces WHERE id=?", (run_id,)).fetchone()
            if not r or (actor.role == "customer" and r["owner"] != actor.subject):
                raise Conflict("NOT_FOUND", 404)
            return json.loads(r["result"])
