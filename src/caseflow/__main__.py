"""Local-only CLI. No model, public host, reset or publishing command."""

import argparse
import json
import os
import secrets
from pathlib import Path

from caseflow.auth import Auth
from caseflow.contracts import Principal
from caseflow.execution import Execution
from caseflow.ledger import Ledger
from caseflow.service import CaseFlow


def open_service(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(root / "business.db")
    ledger.seed()  # INSERT OR IGNORE, never resets an existing business record.
    execution = Execution(ledger, root / "checkpoint.db")
    return CaseFlow(ledger, execution=execution)


def demo(directory):
    if (Path(directory) / "business.db").exists():
        raise SystemExit("Demo needs a NEW directory; existing data will not be reset.")
    customer = Principal("C100", "customer")
    reviewer = Principal("demo-reviewer", "reviewer")
    service = open_service(directory)

    def show(stage, result):
        print(json.dumps({"stage": stage, **result}, ensure_ascii=False))

    try:
        first = service.chat(customer, "我想退款")
        show("request", first)
        second = service.chat(customer, "订单1001", first["conversation_id"])
        show("identify_order", second)
        third = service.chat(customer, "全额", first["conversation_id"])
        show("explicit_full_amount", third)
        p = third["proposal"]
        show("scripted_user_confirmation", service.repository.confirm(customer, p["id"], p["version"], p["digest"]))
        waiting = service.submit(customer, p["id"], "demo-user-confirmed-operation")
        show("real_graph_pause", waiting)
    finally:
        service.execution.close()
    service = open_service(directory)
    try:
        done = service.approve(reviewer, waiting["operation_id"], True, 1, "Scripted demo approval")
        show("saved_staff_approval_after_restart", done)
    finally:
        service.execution.close()
    service = open_service(directory)
    try:
        show("receipt_after_second_restart", service.execution.run(customer, waiting["operation_id"]))
        show("same_request_receipt_replay", service.submit(customer, p["id"], "demo-user-confirmed-operation"))
        show("rejected_expired_return_window", service.chat(customer, "refund 1003 full"))
    finally:
        service.execution.close()


def main():
    os.environ["LANGSMITH_TRACING"] = "false"
    parser = argparse.ArgumentParser(description="CaseFlow — local SIMULATED support")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--directory", default=".local/demo-" + secrets.token_hex(4))
    s = sub.add_parser("serve")
    s.add_argument("--directory", default=".local/app")
    s.add_argument("--port", type=int, default=8000)
    w = sub.add_parser("web", help="Loopback Mock UI with explicit synthetic role-play identities")
    w.add_argument("--directory", default=".local/web")
    w.add_argument("--port", type=int, default=8000)
    b = sub.add_parser("business", help="Sole business SQLite owner, HTTP service")
    b.add_argument("--directory", default=".local/business")
    b.add_argument("--port", type=int, default=8001)
    a = sub.add_parser("agent", help="Agent/UI service; no business database")
    a.add_argument("--business-url", default="http://127.0.0.1:8001")
    a.add_argument("--mode", choices=["mock", "live"], default="mock")
    a.add_argument("--port", type=int, default=8000)
    t = sub.add_parser("token", help="Mint a signed local test identity; requires CASEFLOW_SECRET")
    t.add_argument("--subject", required=True)
    t.add_argument("--role", choices=["customer", "reviewer"], default="customer")
    args = parser.parse_args()
    if args.command == "demo":
        demo(args.directory)
        return
    import uvicorn

    from caseflow.runtime import business, demo_web

    if args.command == "web":
        with demo_web(args.directory) as app:
            uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
        return
    auth = Auth(os.environ.get("CASEFLOW_SECRET", ""))
    if args.command == "token":
        print(auth.issue(args.subject, args.role))
        return
    from caseflow.api import create_app

    if args.command in {"business", "agent"}:
        key = os.environ.get("CASEFLOW_SERVICE_KEY", "")
        if len(key.encode()) < 32:
            raise SystemExit("CASEFLOW_SERVICE_KEY must contain at least 32 bytes")
        if args.command == "business":
            with business(args.directory, auth, key) as app:
                uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
        else:
            from caseflow.demo_planner import DemoPlanner
            from caseflow.gateway import HTTPBusiness
            from caseflow.settings import configured_planner

            planner = DemoPlanner() if args.mode == "mock" else configured_planner("live")
            gateway = HTTPBusiness(args.business_url, auth, key)
            try:
                uvicorn.run(
                    create_app(CaseFlow(gateway, planner), auth),
                    host="127.0.0.1",
                    port=args.port,
                    workers=1,
                    access_log=False,
                )
            finally:
                gateway.close()
                if hasattr(planner, "close"):
                    planner.close()
        return

    service = open_service(args.directory)
    try:
        uvicorn.run(create_app(service, auth), host="127.0.0.1", port=args.port, workers=1)
    finally:
        service.execution.close()


if __name__ == "__main__":
    main()
