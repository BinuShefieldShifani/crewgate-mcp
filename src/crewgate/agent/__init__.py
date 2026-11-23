"""A LangGraph agent that works only through the crewgate MCP gateway.

Every reply it gives passes the confirmation guard (reply_guard.py). Install with the `agent`
extra: uv sync --extra agent
"""

from .graph import Agent, TurnResult
from .llm import ChatModel, Completion, LLMError, LLMQuotaExceeded, OpenAICompatibleChat, ToolCall, Usage
from .reply_guard import Facts, GuardDecision, guard_reply
from .settings import AgentSettings
from .tools import GatewayTools, ToolOutcome

__all__ = [
    "Agent",
    "AgentSettings",
    "ChatModel",
    "Completion",
    "Facts",
    "GatewayTools",
    "GuardDecision",
    "LLMError",
    "LLMQuotaExceeded",
    "OpenAICompatibleChat",
    "ToolCall",
    "ToolOutcome",
    "TurnResult",
    "Usage",
    "guard_reply",
]
