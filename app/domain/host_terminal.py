"""Pure domain values and port for restricted host-terminal execution."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol, TypeAlias


_LOWER_SHA256_LENGTH = 64


def _require_text(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"invalid_{field_name}")


def _require_lower_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _LOWER_SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"invalid_{field_name}")


def _require_canonical_executable(value: str) -> None:
    _require_text(value, "executable")
    parts = value.split("/")
    if (
        not value.startswith("/")
        or value == "/"
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts[1:])
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("invalid_canonical_executable")


def _require_relative_script_path(value: str) -> None:
    _require_text(value, "script_relative_path")
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("invalid_script_relative_path")


def _require_skill_name(value: str) -> None:
    _require_text(value, "skill_name")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("invalid_skill_name")


@dataclass(frozen=True)
class HostCommandTarget:
    """A canonical executable and argv, with no shell-string representation."""

    executable: str
    args: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_canonical_executable(self.executable)
        object.__setattr__(self, "args", tuple(self.args))


@dataclass(frozen=True)
class HostSkillScriptTarget:
    """An enabled Skill script identified by its exact immutable triple."""

    skill_name: str
    script_relative_path: str
    sha256: str
    args: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_skill_name(self.skill_name)
        _require_relative_script_path(self.script_relative_path)
        _require_lower_sha256(self.sha256, "sha256")
        object.__setattr__(self, "args", tuple(self.args))


HostTerminalTarget: TypeAlias = HostCommandTarget | HostSkillScriptTarget


@dataclass(frozen=True)
class HostTerminalExecutionLimits:
    """Concrete limits requested for, and enforced during, one execution."""

    timeout_seconds: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        for name in (
            "timeout_seconds",
            "max_stdout_bytes",
            "max_stderr_bytes",
            "max_concurrency",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid_{name}")


class HostTerminalStatus(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class HostTerminalBridgeRequest:
    protocol_version: str
    request_id: str
    target: HostTerminalTarget
    n_agent_policy_version: str
    n_agent_content_digest: str
    limits: HostTerminalExecutionLimits

    def __post_init__(self) -> None:
        _require_text(self.protocol_version, "protocol_version")
        _require_text(self.request_id, "request_id")
        if not isinstance(self.target, (HostCommandTarget, HostSkillScriptTarget)):
            raise ValueError("invalid_host_target")
        _require_text(self.n_agent_policy_version, "n_agent_policy_version")
        _require_lower_sha256(self.n_agent_content_digest, "n_agent_content_digest")
        if not isinstance(self.limits, HostTerminalExecutionLimits):
            raise ValueError("invalid_execution_limits")


@dataclass(frozen=True)
class HostTerminalBridgeResponse:
    protocol_version: str
    request_id: str
    status: HostTerminalStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.protocol_version, "protocol_version")
        _require_text(self.request_id, "request_id")
        if not isinstance(self.status, HostTerminalStatus):
            raise ValueError("invalid_host_terminal_status")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("invalid_exit_code")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise ValueError("invalid_host_output")
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError("invalid_duration_ms")
        if not isinstance(self.stdout_truncated, bool) or not isinstance(
            self.stderr_truncated, bool
        ):
            raise ValueError("invalid_truncation_flag")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code")


class HostTerminalBridgeClient(Protocol):
    """Domain port implemented by the authenticated Infrastructure client."""

    async def execute(
        self, request: HostTerminalBridgeRequest
    ) -> HostTerminalBridgeResponse: ...
