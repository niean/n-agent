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
    # Task（Kanban / Manus Task）是目标驱动的异步后台执行入口，与"对话""定时任务"
    # 同级。worker 使用进程内 ChatCompletionService 执行，execution_session_id 用
    # `task-{uuid5(NAMESPACE_URL, task.id)}`：从 task.id 确定性派生完整 UUID（str 形式
    # 带连字符，与 schedule-{uuid4()}/curator-{uuid4()} 一致；跨 run 稳定、无需持久化），
    # 符合模式十六"前缀+UUID"规则。非 IM 平台，不进 im_platforms。
    TASK = "task"
    # Delegation（多 Agent 委派）child/aggregator 的隔离执行入口，内部触发（非平台、
    # 非外部 HTTP）。execution_session_id 用 `delegation-{uuid5(NAMESPACE_URL,
    # f"{delegation_id}/{member_id}")}`：从委派与成员确定性派生，跨 run 稳定、member
    # 重试复用同一 session。同 CURATOR/TASK，非 IM 平台，不进 im_platforms（模式十六）。
    DELEGATION = "delegation"

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
    source: str | None = None
    card: dict[str, Any] | None = None


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
