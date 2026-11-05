"""Durable LangGraph approval pause; SQLite business truth repairs checkpoint gaps."""

import sqlite3
import threading
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class ExecutionState(TypedDict):
    operation_id: str
    result: dict


class Execution:
    def __init__(self, ledger, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

        def approval(state):
            oid = state["operation_id"]
            a = ledger.approval(oid)
            if a and a["status"] == "PENDING" and ledger.can_wait(oid):
                with ledger.tx() as db:
                    db.execute("UPDATE runs SET status='WAITING_APPROVAL' WHERE id=? AND outcome IS NULL", (oid,))
                # Resume value carries no authority. Executor re-reads the saved decision.
                interrupt({"operation_id": oid, "approval_version": a["version"], "digest": a["digest"]})
            return {}

        def execute(state):
            return {"result": ledger.execute(state["operation_id"])}

        graph = StateGraph(ExecutionState)
        graph.add_node("approval", approval)
        graph.add_node("commit", execute)
        graph.add_edge(START, "approval")
        graph.add_edge("approval", "commit")
        graph.add_edge("commit", END)
        self.graph = graph.compile(checkpointer=SqliteSaver(self.connection))

    def run(self, actor, oid):
        # Reconcile BEFORE touching graph state: a saved receipt wins over a stale checkpoint.
        op = self.ledger.operation(actor, oid)
        if op["status"] != "PREPARED":
            return self.ledger.render(actor, oid)
        with self.lock:
            config = {"configurable": {"thread_id": oid}, "recursion_limit": 6}
            state = self.graph.get_state(config)
            if not state.values:
                self.graph.invoke({"operation_id": oid}, config)
            elif state.interrupts:
                a = self.ledger.approval(oid)
                if not a or a["status"] != "PENDING" or not self.ledger.can_wait(oid):
                    self.graph.invoke(Command(resume={"reconcile": True}), config)
            elif state.next:
                self.graph.invoke(None, config)
            else:
                self.graph.invoke({"operation_id": oid}, config)
            result = self.ledger.render(actor, oid)
            if result["status"] == "prepared":
                result.update(status="awaiting_approval", reply="Awaiting saved staff approval; no committed receipt.")
            return result

    def close(self):
        self.connection.close()
