from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


DEFAULT_SESSION_TITLE = "New Session"


class SessionSource(str, Enum):
    # 会话来源（一级），与 session_id 前缀一一对应；详见 .harness/knowledge/05-key-patterns.md 模式十六
    DASHBOARD = "dashboard"
    API = "api"
    CLI = "cli"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    ACP = "acp"
    SCHEDULE = "schedule"
    # Curator 周期维护的 consolidation fork 是内部触发（非平台、非外部 HTTP），
    # 独立成一级并配 curator- 前缀，避免误回落 api 导致来源与前缀脱节（模式十六）
    CURATOR = "curator"

    @classmethod
    def im_platforms(cls) -> set[str]:
        # IM 平台来源字符串集合，供 SQLite 迁移与边界判断复用
        return {cls.FEISHU.value, cls.DINGTALK.value, cls.WECOM.value}


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
    is_summary: bool = False
    is_summarized: bool = False


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
    source: str = SessionSource.API.value
    external_memory_enabled: list[str] | None = None
    external_memory_slots: dict[str, str] | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    acp_metadata: dict[str, Any] | None = None

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
