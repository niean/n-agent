from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class SandboxStatus(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class SandboxExecutionRequest:
    code: str
    timeout_seconds: int
    max_tool_calls: int
    enabled_callback_tools: frozenset[str]
    workspace_root: Path
    session_id: str | None
    trusted_metadata: dict[str, Any]
    scratch_dir: Path


@dataclass(frozen=True)
class SandboxExecutionResult:
    status: SandboxStatus
    stdout: str
    stderr: str
    returncode: int
    tool_calls_made: int
    duration_seconds: float
    tool_call_log: list[dict] = field(default_factory=list)


class Sandbox(Protocol):
    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult: ...


@dataclass(frozen=True)
class SandboxCallbackContext:
    workspace_root: Path
    trusted_metadata: dict[str, Any]
    session_id: str | None
    scratch_dir: Path


class SandboxCallbackTool(Protocol):
    name: str
    enabled: bool
    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict: ...


class SandboxCallbackToolRegistry(Protocol):
    def register(self, tool: SandboxCallbackTool) -> None: ...
    def get(self, name: str) -> SandboxCallbackTool | None: ...
    def list_enabled(self) -> list[SandboxCallbackTool]: ...


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    name: str
    def is_available(self) -> bool: ...
    async def search(self, query: str, top_k: int) -> list[SearchResult]: ...


@dataclass(frozen=True)
class ActiveSandboxInfo:
    session_id: str
    sandbox_type: str
    scratch_root: Path
    created_at: datetime
    last_used_at: datetime
    idle_seconds: int
    container_status: str | None = None
    sandbox_id: str | None = None


@dataclass(frozen=True)
class ReleasedSandboxInfo:
    session_id: str
    sandbox_type: str
    sandbox_id: str | None
    created_at: datetime
    released_at: datetime
    reason: str  # "idle" | "force" | "session" | "release"
    id: str | None = None


class ReleasedSandboxRegistry(Protocol):
    def record(self, info: ReleasedSandboxInfo) -> None: ...
    def list_recent(self, limit: int = 100) -> list[ReleasedSandboxInfo]: ...
    def delete(self, entry_id: str) -> bool: ...


@dataclass(frozen=True)
class SandboxExecutionHistoryEntry:
    id: str
    session_id: str
    code_hash: str
    code: str
    result: dict[str, Any] | None
    status: str
    duration_ms: int
    authorized_callback_tools: list[str]
    created_at: datetime


class SandboxExecutionHistoryRegistry(Protocol):
    def record(self, entry: SandboxExecutionHistoryEntry) -> None: ...
    def list_recent(self, session_id: str | None = None, limit: int = 50) -> list[SandboxExecutionHistoryEntry]: ...
    def delete(self, entry_id: str) -> bool: ...
