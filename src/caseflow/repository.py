"""SQLite business truth. Connections are short-lived; writes use BEGIN IMMEDIATE."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from caseflow.contracts import POLICY_VERSION, Conflict, canonical, digest, minor_units
from support_agent.service import default_data_dir


class Repository:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, total_minor INTEGER NOT NULL,
                refunded_minor INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL,
                status TEXT NOT NULL, ordered TEXT NOT NULL, delivered TEXT,
                version INTEGER NOT NULL DEFAULT 1, item TEXT NOT NULL,
                CHECK(refunded_minor >= 0 AND refunded_minor <= total_minor));
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, context TEXT NOT NULL DEFAULT '{}',
                clarifications INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS proposals(
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, version INTEGER NOT NULL,
                owner TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL, expires REAL NOT NULL, UNIQUE(conversation_id, version));
            CREATE TABLE IF NOT EXISTS audit_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL,
                object_id TEXT NOT NULL, detail TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seed(self):
        data = json.loads((default_data_dir() / "store.json").read_text(encoding="utf-8"))
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES('today', ?)", (data["demo_today"],))
            for o in data["orders"]:
                db.execute(
                    """INSERT OR IGNORE INTO orders
                    (id,owner,total_minor,currency,status,ordered,delivered,item) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        o["id"],
                        o["customer_id"],
                        minor_units(o["total"]),
                        o["currency"],
                        o["status"],
                        o["ordered"],
                        o.get("delivered"),
                        o["item"],
                    ),
                )

    def event(self, db, kind, subject, object_id, detail=None):
        db.execute(
            "INSERT INTO audit_events(kind,subject,object_id,detail,at) VALUES(?,?,?,?,?)",
            (kind, subject, object_id, canonical(detail or {}), self.clock()),
        )

    def conversation(self, actor, conversation_id=None):
        actor.require("customer")
        with self.tx() as db:
            if not db.execute("SELECT 1 FROM orders WHERE owner=?", (actor.subject,)).fetchone():
                raise Conflict("UNKNOWN_CUSTOMER", 401)
            if conversation_id:
                row = db.execute(
                    "SELECT * FROM conversations WHERE id=? AND owner=?", (conversation_id, actor.subject)
                ).fetchone()
                if not row:
                    raise Conflict("NOT_FOUND", 404)
            else:
                conversation_id = uuid.uuid4().hex
                db.execute("INSERT INTO conversations(id,owner) VALUES(?,?)", (conversation_id, actor.subject))
                row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            return dict(row)

    def orders(self, actor):
        actor.require("customer")
        with self.tx() as db:
            return [
                dict(o) for o in db.execute("SELECT * FROM orders WHERE owner=? ORDER BY ordered,id", (actor.subject,))
            ]

    def order(self, actor, order_id):
        actor.require("customer")
        with self.tx() as db:
            return self._order(db, actor.subject, order_id)

    @staticmethod
    def _order(db, owner, order_id):
        row = db.execute("SELECT * FROM orders WHERE id=? AND owner=?", (order_id, owner)).fetchone()
        if not row:
            raise Conflict("NOT_FOUND", 404)
        return dict(row)

    def _eligible(self, db, order, action):
        if order["refunded_minor"]:
            raise Conflict("ALREADY_REFUNDED")
        if action == "cancel":
            if order["status"] != "processing":
                raise Conflict("CANNOT_CANCEL")
        else:
            today = date.fromisoformat(db.execute("SELECT value FROM metadata WHERE key='today'").fetchone()[0])
            if order["status"] != "delivered" or not order["delivered"]:
                raise Conflict("NOT_DELIVERED")
            days = (today - date.fromisoformat(order["delivered"])).days
            if not 0 <= days <= 30:
                raise Conflict("OUTSIDE_RETURN_WINDOW")

    def propose(self, actor, conversation_id, decision, supersede=True):
        actor.require("customer")
        with self.tx() as db:
            c = db.execute(
                "SELECT * FROM conversations WHERE id=? AND owner=?", (conversation_id, actor.subject)
            ).fetchone()
            if not c:
                raise Conflict("NOT_FOUND", 404)
            o = self._order(db, actor.subject, decision.order_id)
            if decision.action not in {"refund", "cancel"}:
                raise Conflict("INVALID_ACTION", 422)
            self._eligible(db, o, decision.action)
            if decision.currency != o["currency"] or decision.currency != "EUR":
                raise Conflict("CURRENCY_MISMATCH", 422)
            if decision.action == "refund" and decision.full_refund and decision.amount is not None:
                raise Conflict("CONFLICTING_REFUND_FIELDS", 422)
            amount = (
                (o["total_minor"] if decision.full_refund else minor_units(decision.amount))
                if decision.action == "refund"
                else 0
            )
            if amount > o["total_minor"]:
                raise Conflict("AMOUNT_EXCEEDS_PAID", 422)
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0]
            # Even an invalidating draft cannot leave a previous confirmation executable.
            if supersede:
                db.execute(
                    "UPDATE proposals SET status='SUPERSEDED' WHERE conversation_id=? AND status IN ('DRAFT','CONFIRMED')",
                    (conversation_id,),
                )
            payload = {
                "action": decision.action,
                "order_id": o["id"],
                "order_version": o["version"],
                "amount_minor": amount,
                "currency": o["currency"],
                "policy_version": POLICY_VERSION,
                "evidence_digest": digest(o),
                "proposal_version": version,
            }
            pid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?)",
                (
                    pid,
                    conversation_id,
                    version,
                    actor.subject,
                    canonical(payload),
                    digest(payload),
                    "DRAFT",
                    self.clock() + 900,
                ),
            )
            db.execute(
                "UPDATE conversations SET context=?,clarifications=0 WHERE id=?",
                (canonical({"action": decision.action, "order_id": o["id"]}), conversation_id),
            )
            self.event(db, "proposal_created", actor.subject, pid, payload)
            return {"id": pid, "version": version, "digest": digest(payload), **payload}

    def invalidate(self, actor, conversation_id):
        # Called before a new natural-language turn. A changed/ambiguous intent invalidates old consent.
        with self.tx() as db:
            db.execute(
                "UPDATE proposals SET status='SUPERSEDED' WHERE conversation_id=? AND owner=? AND status IN ('DRAFT','CONFIRMED')",
                (conversation_id, actor.subject),
            )

    def clarify(self, actor, conversation_id, context):
        with self.tx() as db:
            row = db.execute(
                "SELECT clarifications FROM conversations WHERE id=? AND owner=?", (conversation_id, actor.subject)
            ).fetchone()
            if not row:
                raise Conflict("NOT_FOUND", 404)
            count = row[0] + 1
            db.execute(
                "UPDATE conversations SET context=?,clarifications=? WHERE id=?",
                (canonical(context), min(count, 2), conversation_id),
            )
            self.event(db, "clarification" if count <= 2 else "handoff", actor.subject, conversation_id)
            return count

    def _proposal(self, db, actor, pid):
        row = db.execute("SELECT * FROM proposals WHERE id=? AND owner=?", (pid, actor.subject)).fetchone()
        if not row:
            raise Conflict("NOT_FOUND", 404)
        return dict(row)

    def _valid(self, db, p):
        payload = json.loads(p["payload"])
        if p["expires"] <= self.clock():
            raise Conflict("PROPOSAL_EXPIRED")
        if payload["policy_version"] != POLICY_VERSION:
            raise Conflict("POLICY_CHANGED")
        o = self._order(db, p["owner"], payload["order_id"])
        if o["version"] != payload["order_version"] or digest(o) != payload["evidence_digest"]:
            raise Conflict("ORDER_CHANGED")
        self._eligible(db, o, payload["action"])
        return payload, o

    def confirm(self, actor, pid, version, expected_digest):
        actor.require("customer")
        with self.tx() as db:
            p = self._proposal(db, actor, pid)
            if p["status"] not in {"DRAFT", "CONFIRMED"}:
                raise Conflict("PROPOSAL_NOT_ACTIVE")
            if version != p["version"] or expected_digest != p["digest"]:
                raise Conflict("VERSION_CONFLICT")
            self._valid(db, p)
            db.execute("UPDATE proposals SET status='CONFIRMED' WHERE id=?", (pid,))
            self.event(db, "user_confirmed", actor.subject, pid, {"version": version, "digest": expected_digest})
            return {"proposal_id": pid, "status": "CONFIRMED"}

    def today(self):
        with self.tx() as db:
            return db.execute("SELECT value FROM metadata WHERE key='today'").fetchone()[0]

    def history(self, actor, cid):
        self.conversation(actor, cid)
        with self.tx() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT role,content FROM (SELECT * FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 12) ORDER BY seq",
                    (cid,),
                )
            ]

    def record_turn(self, actor, cid, text, result):
        self.conversation(actor, cid)
        with self.tx() as db:
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
