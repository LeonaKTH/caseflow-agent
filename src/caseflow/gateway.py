"""Typed HTTP adapter to the sole business authority. No database connection."""

from urllib.parse import quote, urlsplit

import httpx

from caseflow.contracts import Conflict


def segment(value):
    return quote(str(value), safe="")


class HTTPBusiness:
    def __init__(self, base_url, auth, service_key, client=None):
        u = urlsplit(base_url)
        if (
            u.hostname not in {"127.0.0.1", "localhost", "::1"}
            or u.scheme not in {"http", "https"}
            or u.username
            or u.query
            or u.fragment
        ):
            raise ValueError("Business API must be a local loopback URL")
        self.base = base_url.rstrip("/")
        self.auth, self.service_key = auth, service_key
        self.client = client or httpx.Client(timeout=5, follow_redirects=False)

    def call(self, method, path, actor=None, body=None):
        headers = {"X-Caseflow-Service": self.service_key}
        if actor:
            headers["Authorization"] = "Bearer " + self.auth.issue(actor.subject, actor.role)
        retry_safe = (
            method == "GET"
            or path.endswith(("/submit", "/confirm", "/record", "/invalidate"))
            or (path == "/api/v1/tickets" and body and "key" in body)
        )
        for attempt in range(2 if retry_safe else 1):
            try:
                response = self.client.request(method, self.base + path, headers=headers, json=body, timeout=5)
                if response.status_code >= 500 and attempt == 0 and retry_safe:
                    continue
                if response.is_error:
                    try:
                        code = response.json().get("code", "BUSINESS_REQUEST_REJECTED")
                    except ValueError:
                        code = "BUSINESS_REQUEST_REJECTED"
                    raise Conflict(code, response.status_code)
                return response.json()
            except httpx.TransportError:
                if attempt or not retry_safe:
                    raise Conflict("BUSINESS_UNAVAILABLE", 503) from None
        raise Conflict("BUSINESS_UNAVAILABLE", 503)

    def today(self):
        return self.call("GET", "/api/v1/metadata")["today"]

    def orders(self, a):
        return self.call("GET", "/api/v1/orders", a)

    def order(self, a, oid):
        return self.call("GET", f"/api/v1/orders/{segment(oid)}", a)

    def logistics(self, a, oid):
        return self.call("GET", f"/api/v1/orders/{segment(oid)}/logistics", a)

    def refund_info(self, a, oid):
        return self.call("GET", f"/api/v1/orders/{segment(oid)}/refund-info", a)

    def conversation(self, a, cid=None):
        return self.call("POST", "/internal/conversations", a, {"conversation_id": cid})

    def invalidate(self, a, cid):
        return self.call("POST", f"/internal/conversations/{segment(cid)}/invalidate", a)

    def history(self, a, cid):
        return self.call("GET", f"/internal/conversations/{segment(cid)}/history", a)

    def clarify(self, a, cid, context):
        return self.call("POST", f"/internal/conversations/{segment(cid)}/clarify", a, {"context": context})

    def propose(self, a, cid, decision, supersede=True):
        return self.call(
            "POST",
            "/internal/proposals",
            a,
            {"conversation_id": cid, "decision": decision.model_dump(), "supersede": supersede},
        )

    def confirm(self, a, pid, version, expected_digest):
        return self.call(
            "POST", f"/api/v1/proposals/{segment(pid)}/confirm", a, {"version": version, "digest": expected_digest}
        )

    def submit(self, a, pid, key):
        return self.call("POST", f"/api/v1/proposals/{segment(pid)}/submit", a, {"idempotency_key": key})

    def approve(self, a, oid, approved, version, comment=""):
        return self.call(
            "POST",
            f"/api/v1/approvals/{segment(oid)}/decision",
            a,
            {"approved": approved, "version": version, "comment": comment},
        )

    def pending(self, a):
        return self.call("GET", "/api/v1/approvals", a)

    def render(self, a, oid):
        return self.call("GET", f"/api/v1/operations/{segment(oid)}", a)

    def recover(self, a, oid):
        return self.call("POST", f"/api/v1/operations/{segment(oid)}/recover", a)

    def cancel_operation(self, a, oid):
        return self.call("POST", f"/api/v1/operations/{segment(oid)}/cancel", a)

    def audit(self, a, oid):
        return self.call("GET", f"/api/v1/operations/{segment(oid)}/audit", a)

    def create_ticket(self, a, cid, request, reason, evidence, order_ids, key):
        return self.call(
            "POST",
            "/api/v1/tickets",
            a,
            {
                "conversation_id": cid,
                "request": request,
                "reason": reason,
                "evidence": evidence,
                "order_ids": order_ids,
                "key": key,
            },
        )

    def ticket(self, a, tid):
        return self.call("GET", f"/api/v1/tickets/{segment(tid)}", a)

    def tickets(self, a, cid=None):
        return self.call("GET", "/api/v1/tickets" + ("?conversation_id=" + segment(cid) if cid else ""), a)

    def update_ticket(self, a, tid, status, version, resolution=""):
        return self.call(
            "POST",
            f"/api/v1/tickets/{segment(tid)}",
            a,
            {"status": status, "version": version, "resolution": resolution},
        )

    def record_turn(self, a, cid, text, result):
        return self.call("POST", f"/internal/conversations/{segment(cid)}/record", a, {"text": text, "result": result})

    def trace(self, a, rid):
        return self.call("GET", f"/api/v1/traces/{segment(rid)}", a)

    def close(self):
        self.client.close()
