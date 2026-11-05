from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from support_agent.service import SupportService


class ScriptedToolModel(BaseChatModel):
    """Test double that replays a fixed list of AI messages, including tool calls.

    Lets tests act like a confused or manipulated model that calls tools it shouldn't.
    """

    replies: list[AIMessage]
    position: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedToolModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        template = self.replies[min(self.position, len(self.replies) - 1)]
        self.position += 1
        # A fresh message with unique tool-call ids each time, like a real model. Reusing one message object
        # would make LangGraph's message reducer treat it as an update to the earlier message.
        calls = [{**c, "id": f"{c['id']}_{self.position}"} for c in template.tool_calls]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=template.content, tool_calls=calls))])


def call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


@pytest.fixture
def service() -> SupportService:
    return SupportService()
