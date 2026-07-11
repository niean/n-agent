from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EndReason(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"
    ITERATION_LIMIT = "iteration_limit"


@dataclass(frozen=True)
class AgentRun:
    session_id: str
    input_messages: list[dict[str, Any]]
    id: str = field(default_factory=lambda: str(uuid4()))
    status: RunStatus = RunStatus.PENDING
    iteration_count: int = 0
    error: str | None = None
    end_reason: EndReason | None = None


@dataclass
class AgentState:
    session_id: str
    input_messages: list[dict[str, Any]] = field(default_factory=list)
    working_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_tool_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    run_status: RunStatus = RunStatus.PENDING
    iteration_count: int = 0
    error: str | None = None
    final_message: dict[str, Any] | None = None
    finish_reason: str | None = None
    run_options: dict[str, Any] = field(default_factory=dict)
    stream_tool_events: list[Any] = field(default_factory=list)
    context_message_ids: list[str] = field(default_factory=list)
