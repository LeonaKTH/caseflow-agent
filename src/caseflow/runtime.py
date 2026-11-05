"""Local runtime composition: the Agent only talks HTTP to the business owner."""

import secrets
from contextlib import contextmanager
from pathlib import Path

from caseflow.api import create_app
from caseflow.auth import Auth
from caseflow.business_api import create_business_app
from caseflow.demo_planner import DemoPlanner
from caseflow.execution import Execution
from caseflow.gateway import HTTPBusiness
from caseflow.local_server import running
from caseflow.service import CaseFlow
from caseflow.support_business import SupportLedger


@contextmanager
def business(directory, auth, service_key):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    ledger = SupportLedger(root / "business.db")
    ledger.seed()
    execution = Execution(ledger, root / "checkpoint.db")
    try:
        yield create_business_app(ledger, execution, auth, service_key)
    finally:
        execution.close()


@contextmanager
def demo_web(directory):
    # Secrets are ephemeral, process-local and never printed or written to disk.
    auth = Auth(secrets.token_urlsafe(48))
    key = secrets.token_urlsafe(48)
    with business(directory, auth, key) as backend, running(backend) as url:
        gateway = HTTPBusiness(url, auth, key)
        try:
            yield create_app(CaseFlow(gateway, DemoPlanner()), auth, demo_identities=True)
        finally:
            gateway.close()
