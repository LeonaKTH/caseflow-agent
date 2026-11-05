"""Durable simulated effects. Only this executor may commit order changes."""

import uuid

from caseflow.contracts import Conflict, Principal, canonical, digest
from caseflow.repository import Repository


class Ledger(Repository):
    def __init__(self, path, clock=None, fault=None):
        super().__init__(path, **({"clock": clock} if clock else {}))
        self.fault = fault or (lambda point: None)
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id), digest TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT, UNIQUE(owner,idempotency_key));
            CREATE TABLE IF NOT EXISTS approvals(
                id TEXT PRIMARY KEY REFERENCES operations(id), digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING', reviewer TEXT, comment TEXT,
                version INTEGER NOT NULL DEFAULT 1, consumed INTEGER NOT NULL DEFAULT 0,
                expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(
                operation_id TEXT PRIMARY KEY REFERENCES operations(id), order_id TEXT NOT NULL,
                action TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
                simulated INTEGER NOT NULL CHECK(simulated=1), UNIQUE(order_id,action));
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY REFERENCES operations(id), status TEXT NOT NULL, outcome TEXT);
            """)

    def prepare(self, actor, pid, idempotency_key):
        actor.require("customer")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise Conflict("INVALID_IDEMPOTENCY_KEY", 422)
        with self.tx() as db:
            p = self._proposal(db, actor, pid)
            request_digest = digest({"proposal_id": pid, "proposal_digest": p["digest"]})
            old = db.execute(
                "SELECT * FROM operations WHERE owner=? AND idempotency_key=?", (actor.subject, idempotency_key)
            ).fetchone()
            if old:
                if old["digest"] != request_digest:
                    raise Conflict("IDEMPOTENCY_CONFLICT")
                return dict(old)
            if db.execute("SELECT 1 FROM operations WHERE proposal_id=?", (pid,)).fetchone():
                raise Conflict("OPERATION_EXISTS")
            if p["status"] != "CONFIRMED":
                raise Conflict("USER_CONFIRMATION_REQUIRED")
            payload, _ = self._valid(db, p)
            oid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO operations(id,owner,idempotency_key,proposal_id,digest,status) VALUES(?,?,?,?,?,'PREPARED')",
                (oid, actor.subject, idempotency_key, pid, request_digest),
            )
            db.execute("INSERT INTO runs VALUES(?,'RUNNING',NULL)", (oid,))
            if payload["action"] == "refund" and payload["amount_minor"] > 5000:
                db.execute("INSERT INTO approvals(id,digest,expires) VALUES(?,?,?)", (oid, p["digest"], p["expires"]))
            self.event(db, "operation_prepared", actor.subject, oid, {"proposal_id": pid})
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone())

    def approval(self, oid):
        with self.tx() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (oid,)).fetchone()
            return dict(row) if row else None

    def can_wait(self, oid):
        """Stale/expired pending approvals must reconcile to rejection, not wait forever."""
        with self.tx() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
            if not op or op["status"] != "PREPARED":
                return False
            p = self._proposal(db, Principal(op["owner"], "customer"), op["proposal_id"])
            try:
                if p["status"] != "CONFIRMED":
                    return False
                self._valid(db, p)
                return True
            except Conflict:
                return False

    def decide(self, actor, oid, approved, expected_version, comment=""):
        actor.require("reviewer")
        if type(approved) is not bool or type(expected_version) is not int:
            raise Conflict("INVALID_DECISION", 422)
        with self.tx() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
            a = db.execute("SELECT * FROM approvals WHERE id=?", (oid,)).fetchone()
            if not op or not a:
                raise Conflict("NOT_FOUND", 404)
            if actor.subject == op["owner"]:
                raise Conflict("SELF_APPROVAL_DENIED", 403)
            if a["status"] != "PENDING" or a["version"] != expected_version or op["status"] != "PREPARED":
                raise Conflict("VERSION_CONFLICT")
            if a["expires"] <= self.clock():
                raise Conflict("APPROVAL_EXPIRED")
            p = self._proposal(db, Principal(op["owner"], "customer"), op["proposal_id"])
            if p["status"] != "CONFIRMED" or a["digest"] != p["digest"]:
                raise Conflict("APPROVAL_STALE")
            self._valid(db, p)
            db.execute(
                "UPDATE approvals SET status=?,reviewer=?,comment=?,version=version+1 WHERE id=? AND version=?",
                ("APPROVED" if approved else "REJECTED", actor.subject, comment[:500], oid, expected_version),
            )
            self.event(db, "approval_decided", actor.subject, oid, {"approved": approved, "version": expected_version})
        self.fault("after_approval_saved")

    def pending(self, actor):
        actor.require("reviewer")
        with self.tx() as db:
            return [
                dict(r)
                for r in db.execute(
                    """SELECT a.*,p.payload FROM approvals a
                JOIN operations o ON o.id=a.id JOIN proposals p ON p.id=o.proposal_id
                WHERE a.status='PENDING' AND o.status='PREPARED' AND p.status='CONFIRMED' AND a.expires>?
                ORDER BY a.id""",
                    (self.clock(),),
                )
            ]

    def _receipt(self, db, oid):
        r = db.execute("SELECT * FROM receipts WHERE operation_id=?", (oid,)).fetchone()
        return dict(r) if r else None

    def operation(self, actor, oid):
        with self.tx() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
            if not row or (actor.role == "customer" and row["owner"] != actor.subject):
                raise Conflict("NOT_FOUND", 404)
            if actor.role not in {"customer", "reviewer"}:
                raise Conflict("ROLE_DENIED", 403)
            return {**dict(row), "receipt": self._receipt(db, oid)}

    def execute(self, oid):
        # Not an HTTP/tool endpoint. Called by the trusted graph/reconciler, never by the model.
        with self.tx() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (oid,)).fetchone()
            if not op:
                raise Conflict("NOT_FOUND", 404)
            old = self._receipt(db, oid)
            if old:
                return {"status": "committed", "receipt": old}
            if op["status"] != "PREPARED":
                return {"status": "rejected", "reason": op["reason"] or op["status"]}
            p = self._proposal(db, Principal(op["owner"], "customer"), op["proposal_id"])
            try:
                if p["status"] != "CONFIRMED":
                    raise Conflict("PROPOSAL_NOT_ACTIVE")
                payload, order = self._valid(db, p)
                a = db.execute("SELECT * FROM approvals WHERE id=?", (oid,)).fetchone()
                if payload["action"] == "refund" and payload["amount_minor"] > 5000:
                    if not a or a["status"] == "PENDING":
                        raise Conflict("APPROVAL_REQUIRED")
                    if (
                        a["status"] != "APPROVED"
                        or a["digest"] != p["digest"]
                        or a["expires"] <= self.clock()
                        or a["consumed"]
                    ):
                        raise Conflict("APPROVAL_NOT_VALID")
                if payload["action"] == "refund":
                    amount = payload["amount_minor"]
                    if type(amount) is not int or not 0 < amount <= order["total_minor"]:
                        raise Conflict("INVALID_AMOUNT")
                    db.execute(
                        "UPDATE orders SET refunded_minor=?,version=version+1 WHERE id=? AND version=?",
                        (amount, order["id"], order["version"]),
                    )
                else:
                    db.execute(
                        "UPDATE orders SET status='cancelled',version=version+1 WHERE id=? AND version=?",
                        (order["id"], order["version"]),
                    )
                db.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?,?,1)",
                    (oid, order["id"], payload["action"], payload["amount_minor"], payload["currency"]),
                )
                db.execute("UPDATE operations SET status='COMMITTED' WHERE id=?", (oid,))
                db.execute("UPDATE proposals SET status='EXECUTED' WHERE id=?", (p["id"],))
                db.execute("UPDATE approvals SET consumed=1 WHERE id=?", (oid,))
                db.execute("UPDATE runs SET status='TERMINAL',outcome='OK' WHERE id=?", (oid,))
                self.event(db, "business_committed", op["owner"], oid, payload)
                self.fault("before_commit")
                receipt = self._receipt(db, oid)
            except Conflict as error:
                if error.code == "APPROVAL_REQUIRED":
                    return {"status": "awaiting_approval"}
                db.execute("UPDATE operations SET status='REJECTED',reason=? WHERE id=?", (error.code, oid))
                db.execute("UPDATE runs SET status='TERMINAL',outcome='REFUSED' WHERE id=?", (oid,))
                self.event(db, "operation_rejected", op["owner"], oid, {"code": error.code})
                return {"status": "rejected", "reason": error.code}
        self.fault("after_commit")  # Deliberately outside the business transaction and before checkpoint.
        return {"status": "committed", "receipt": receipt}

    def cancel_operation(self, actor, oid):
        actor.require("customer")
        with self.tx() as db:
            op = db.execute("SELECT * FROM operations WHERE id=? AND owner=?", (oid, actor.subject)).fetchone()
            if not op:
                raise Conflict("NOT_FOUND", 404)
            if op["status"] != "PREPARED":
                raise Conflict("OPERATION_TERMINAL")
            db.execute("UPDATE operations SET status='REJECTED',reason='CANCELLED' WHERE id=?", (oid,))
            db.execute("UPDATE proposals SET status='CANCELLED' WHERE id=?", (op["proposal_id"],))
            db.execute("UPDATE runs SET status='TERMINAL',outcome='CANCELLED' WHERE id=?", (oid,))
            self.event(db, "operation_cancelled", actor.subject, oid)
            return {"status": "cancelled"}

    def audit(self, actor, oid):
        self.operation(actor, oid)
        with self.tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit_events WHERE object_id=? ORDER BY seq", (oid,))]

    def render(self, actor, oid):
        op = self.operation(actor, oid)
        if op["receipt"]:
            return {
                "operation_id": oid,
                "status": "committed",
                "receipt": op["receipt"],
                "reply": "SIMULATED: " + canonical(op["receipt"]),
            }
        return {
            "operation_id": oid,
            "status": op["status"].lower(),
            "receipt": None,
            "reply": op["reason"] or "No committed receipt. No payment has been made.",
        }
