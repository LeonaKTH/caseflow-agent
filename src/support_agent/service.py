"""Runs conversations, enforces conversation ownership and manages refund approvals."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from support_agent.graph import build_graph
from support_agent.models import get_model
from support_agent.policies_kb import PolicyBase
from support_agent.policy import PolicyConfig
from support_agent.store import Store
from support_agent.tools import make_tools

MAX_STEPS_PER_TURN = 16  # model + tool steps; stops a confused model from looping forever


def default_data_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data" / "store.json").is_file():
            return parent / "data"
    # Public distribution adaptation: wheel installs keep the demo fixtures under sys.prefix.
    packaged = Path(sys.prefix) / "share" / "caseflow"
    if (packaged / "store.json").is_file():
        return packaged
    raise FileNotFoundError("CaseFlow demo data not found; install the project with its packaged data")


class ConversationRegistry:
    """Which customer owns each conversation, and which refunds are waiting for approval.

    Stored in SQLite next to LangGraph's checkpoints, so a pending approval survives a restart.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS conversations (thread_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS pending_approvals (thread_id TEXT PRIMARY KEY, details TEXT NOT NULL)"
            )

    def claim(self, thread_id: str, customer_id: str) -> str:
        """Records the owner of a new conversation and returns the owner (which may be someone else)."""
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO conversations VALUES (?, ?)", (thread_id, customer_id))
            return self._db.execute(
                "SELECT customer_id FROM conversations WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]

    def owner(self, thread_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT customer_id FROM conversations WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0] if row else None

    def add_pending(self, thread_id: str, details: dict) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO pending_approvals VALUES (?, ?)", (thread_id, json.dumps(details)))

    def is_pending(self, thread_id: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM pending_approvals WHERE thread_id = ?", (thread_id,)).fetchone()
                is not None
            )

    def take_pending(self, thread_id: str) -> dict | None:
        """Removes and returns a pending approval, so two reviewers can't both resolve it."""
        with self._lock, self._db:
            row = self._db.execute("SELECT details FROM pending_approvals WHERE thread_id = ?", (thread_id,)).fetchone()
            if row:
                self._db.execute("DELETE FROM pending_approvals WHERE thread_id = ?", (thread_id,))
        return json.loads(row[0]) if row else None

    def all_pending(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT thread_id, details FROM pending_approvals ORDER BY rowid").fetchall()
        return [{"thread_id": t, **json.loads(d)} for t, d in rows]


class ConversationError(Exception):
    """The request doesn't fit the conversation (wrong customer, waiting for approval, unknown thread)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ChatResult:
    thread_id: str
    status: str  # answered | awaiting_approval
    reply: str
    tools_used: list[str] = field(default_factory=list)
    approval: dict | None = None


class SupportService:
    def __init__(
        self,
        model: BaseChatModel | None = None,
        store: Store | None = None,
        policy: PolicyConfig | None = None,
        data_dir: Path | None = None,
        db_path: str | None = None,
    ) -> None:
        """db_path: SQLite file for conversations, checkpoints and pending approvals. None keeps them in memory."""
        data_dir = data_dir or default_data_dir()
        self.store = store or Store.load(data_dir / "store.json")
        self.policy = policy or PolicyConfig()
        tools = make_tools(self.store, PolicyBase(data_dir / "policies.md"), self.policy)

        connection = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        checkpointer: BaseCheckpointSaver = SqliteSaver(connection) if db_path else InMemorySaver()
        self.registry = ConversationRegistry(connection)
        self.graph = build_graph(model or get_model(), tools, self.store.customers, checkpointer)

    # ---------- customer side ----------

    def chat(self, customer_id: str, message: str, thread_id: str | None = None) -> ChatResult:
        if customer_id not in self.store.customers:
            raise ConversationError("Unknown customer.", 401)
        thread_id = thread_id or uuid.uuid4().hex
        if self.registry.claim(thread_id, customer_id) != customer_id:
            raise ConversationError("Conversation not found.", 404)  # don't reveal that it exists
        if self.registry.is_pending(thread_id):
            raise ConversationError("A refund in this conversation is waiting for approval.", 409)
        return self._run({"messages": [HumanMessage(message)]}, customer_id, thread_id)

    def history(self, customer_id: str, thread_id: str) -> list[dict]:
        if self.registry.owner(thread_id) != customer_id:
            raise ConversationError("Conversation not found.", 404)
        messages = self.graph.get_state(self._config(customer_id, thread_id)).values.get("messages", [])
        return [
            {"role": "customer" if isinstance(m, HumanMessage) else "assistant", "text": str(m.content)}
            for m in messages
            if isinstance(m, HumanMessage | AIMessage) and m.content
        ]

    # ---------- staff side ----------

    def pending_approvals(self) -> list[dict]:
        return self.registry.all_pending()

    def resolve_approval(self, thread_id: str, approved: bool, reviewer: str, note: str | None = None) -> ChatResult:
        details = self.registry.take_pending(thread_id)
        if details is None:
            raise ConversationError("No refund is waiting for approval in this conversation.", 404)
        decision = {"approved": approved, "reviewer": reviewer, "note": note}
        return self._run(Command(resume=decision), details["customer_id"], thread_id)

    # ---------- internals ----------

    @staticmethod
    def _config(customer_id: str, thread_id: str) -> dict:
        return {
            "configurable": {"thread_id": thread_id, "customer_id": customer_id},
            "recursion_limit": MAX_STEPS_PER_TURN,
        }

    def _run(self, graph_input, customer_id: str, thread_id: str) -> ChatResult:
        config = self._config(customer_id, thread_id)
        before = len(self.graph.get_state(config).values.get("messages", []))
        try:
            self.graph.invoke(graph_input, config)
        except GraphRecursionError:
            ticket = self.store.open_ticket(customer_id, f"Agent loop limit reached in conversation {thread_id}")
            return ChatResult(
                thread_id,
                "answered",
                f"Sorry, I'm having trouble with this one. I've passed it to a colleague (ticket {ticket}).",
            )

        state = self.graph.get_state(config)
        new_messages = state.values.get("messages", [])[before:]
        tools_used = [call["name"] for m in new_messages if isinstance(m, AIMessage) for call in m.tool_calls]

        if state.interrupts:
            details = state.interrupts[0].value
            self.registry.add_pending(thread_id, details)
            reply = (
                f"I've asked a colleague to approve a refund of {details['amount']} {details['currency']} "
                f"for order {details['order_id']}. You'll get an answer here shortly."
            )
            return ChatResult(thread_id, "awaiting_approval", reply, tools_used, details)

        last_ai = next((m for m in reversed(new_messages) if isinstance(m, AIMessage) and not m.tool_calls), None)
        return ChatResult(thread_id, "answered", str(last_ai.content) if last_ai else "", tools_used)
