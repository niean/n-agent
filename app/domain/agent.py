from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from app.domain.context_policy import ContextPlan


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
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    BUDGET_EXHAUSTED = "budget_exhausted"


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
    run_id: str = field(default_factory=lambda: str(uuid4()))
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
    context_plan: ContextPlan | None = None
    budget_exhausted: bool = False
    # When False, suppress persisting assistant/tool messages and tool_call
    # audit records to the session store (tools still execute, LLM context
    # still includes results). Used by goal_mode judge fork to keep its
    # internal control-flow signals out of user-visible Chat history.
    # Mirrored into run_options by AgentGraphRunner.run for central access.
    persist_messages: bool = True
    # Source to tag on persisted assistant messages for process-origin runs
    # (task/schedule/curator workers), so the dashboard can hide the worker's
    # internal chain-of-thought reasoning. Regression: worker CoT
    # "The task requires querying weather..." leaked to the dashboard chat as a
    # normal assistant bubble because assistant messages were persisted with
    # source=None and rendered like a realtime reply. The reasoning stays in
    # the store/LLM context for goal-mode continuation (unlike the judge fork,
    # which uses persist_messages=False to drop its reasoning entirely); only
    # the dashboard rendering hides it. None for realtime chat (api/dashboard)
    # preserves existing assistant rendering. Set by ChatCompletionService from
    # session_source; read by AgentGraphRunner when persisting assistant msgs.
    message_source: str | None = None
