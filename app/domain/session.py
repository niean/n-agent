from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4


DEFAULT_SESSION_TITLE = "New Session"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: Any
    id: str = field(default_factory=lambda: str(uuid4()))
    tool_call_id: str | None = None
    name: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ToolCall:
    id: str
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    status: str = "pending"
    duration_ms: int = 0
    message_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Summary:
    session_id: str
    summary: str
    source_message_id: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class TaskState:
    session_id: str
    status: str = "idle"
    iteration_count: int = 0
    last_error: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ConversationSession:
    id: str
    title: str = DEFAULT_SESSION_TITLE
    source: str = "api"
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def has_default_title(self) -> bool:
        return self.title == DEFAULT_SESSION_TITLE


class TitleGenerator(Protocol):
    async def generate(self, user_message: str) -> str:
        ...


class SessionNotFoundError(Exception):
    def __init__(self, session_id: str):
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class SessionValidationError(Exception):
    pass
