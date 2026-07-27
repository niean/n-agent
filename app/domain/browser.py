"""Browser subdomain: models, state machine, actions, ports. Pure domain, no SDK/IO."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class BrowserBackendType(str, Enum):
    HOST_CDP = "host_cdp"
    CONTAINER = "container"


class BrowserSessionStatus(str, Enum):
    PENDING_AUTHORIZATION = "pending_authorization"
    ACTIVE = "active"
    PAUSED = "paused"
    TAKEOVER = "takeover"
    DEGRADED = "degraded"
    CLOSED = "closed"


# Legal transitions (source -> set of allowed targets).
_TRANSITIONS: dict[BrowserSessionStatus, frozenset[BrowserSessionStatus]] = {
    BrowserSessionStatus.PENDING_AUTHORIZATION: frozenset({
        BrowserSessionStatus.ACTIVE,
        BrowserSessionStatus.CLOSED,
    }),
    BrowserSessionStatus.ACTIVE: frozenset({
        BrowserSessionStatus.PAUSED,
        BrowserSessionStatus.TAKEOVER,
        BrowserSessionStatus.DEGRADED,
        BrowserSessionStatus.CLOSED,
    }),
    BrowserSessionStatus.PAUSED: frozenset({
        BrowserSessionStatus.ACTIVE,
        BrowserSessionStatus.TAKEOVER,
        BrowserSessionStatus.DEGRADED,
        BrowserSessionStatus.CLOSED,
    }),
    BrowserSessionStatus.TAKEOVER: frozenset({
        BrowserSessionStatus.ACTIVE,
        BrowserSessionStatus.PAUSED,
        BrowserSessionStatus.DEGRADED,
        BrowserSessionStatus.CLOSED,
    }),
    BrowserSessionStatus.DEGRADED: frozenset({BrowserSessionStatus.CLOSED}),
    BrowserSessionStatus.CLOSED: frozenset(),
}


_UNSET = object()  # sentinel: distinguish "keep existing" from "clear to None"


@dataclass(frozen=True)
class BrowserSessionId:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or "/" in self.value or "\x00" in self.value:
            raise ValueError("invalid_browser_session_id")


@dataclass(frozen=True)
class BrowserProfileRef:
    """Opaque logical profile reference, not a filesystem path."""
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or "/" in self.value or "\x00" in self.value:
            raise ValueError("invalid_browser_profile_ref")


@dataclass(frozen=True)
class BrowserSession:
    id: str
    bound_n_agent_session_id: str
    backend_type: BrowserBackendType
    status: BrowserSessionStatus
    profile_ref: str
    document_revision: int = 0
    pre_takeover_status: BrowserSessionStatus | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None

    @classmethod
    def create_for_container(cls, sid: str, nagent_sid: str, profile_ref: str) -> "BrowserSession":
        return cls(
            id=sid,
            bound_n_agent_session_id=nagent_sid,
            backend_type=BrowserBackendType.CONTAINER,
            status=BrowserSessionStatus.ACTIVE,
            profile_ref=profile_ref,
        )

    @classmethod
    def create_for_host(cls, sid: str, nagent_sid: str, profile_ref: str) -> "BrowserSession":
        return cls(
            id=sid,
            bound_n_agent_session_id=nagent_sid,
            backend_type=BrowserBackendType.HOST_CDP,
            status=BrowserSessionStatus.PENDING_AUTHORIZATION,
            profile_ref=profile_ref,
        )

    def can_transition_to(self, target: BrowserSessionStatus) -> bool:
        return target in _TRANSITIONS[self.status]

    def transition_to(
        self, target: BrowserSessionStatus, *, reason: str = ""
    ) -> "BrowserSession":
        if not self.can_transition_to(target):
            raise ValueError(f"invalid_state_transition:{self.status}->{target}")
        if target is BrowserSessionStatus.TAKEOVER:
            # Save current status as pre_takeover_status.
            return self.with_status(target, pre_takeover_status=self.status)
        if self.status is BrowserSessionStatus.TAKEOVER:
            # Release: restore to target (caller passes pre_takeover_status), clear it.
            return self.with_status(target, pre_takeover_status=None)
        return self.with_status(target)

    def with_status(
        self,
        status: BrowserSessionStatus,
        *,
        pre_takeover_status: object = _UNSET,
        document_revision: object = _UNSET,
    ) -> "BrowserSession":
        return BrowserSession(
            id=self.id,
            bound_n_agent_session_id=self.bound_n_agent_session_id,
            backend_type=self.backend_type,
            status=status,
            profile_ref=self.profile_ref,
            document_revision=self.document_revision if document_revision is _UNSET else document_revision,
            pre_takeover_status=(
                self.pre_takeover_status if pre_takeover_status is _UNSET else pre_takeover_status
            ),
            created_at=self.created_at,
            updated_at=self.updated_at,
            closed_at=self.closed_at,
        )

    @property
    def is_active(self) -> bool:
        return self.status is BrowserSessionStatus.ACTIVE


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"invalid_{field_name}")


@dataclass(frozen=True)
class NavigateAction:
    url: str

    def __post_init__(self) -> None:
        _require_text(self.url, "url")


@dataclass(frozen=True)
class ObserveAction:
    max_text_chars: int = 4000
    max_elements: int = 80

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_text_chars, int)
            or isinstance(self.max_text_chars, bool)
            or self.max_text_chars <= 0
            or self.max_text_chars > 20000
        ):
            raise ValueError("invalid_max_text_chars")
        if (
            not isinstance(self.max_elements, int)
            or isinstance(self.max_elements, bool)
            or self.max_elements <= 0
            or self.max_elements > 200
        ):
            raise ValueError("invalid_max_elements")


@dataclass(frozen=True)
class ClickAction:
    element_ref: str
    document_revision: int

    def __post_init__(self) -> None:
        _require_text(self.element_ref, "element_ref")
        if not isinstance(self.document_revision, int) or isinstance(self.document_revision, bool) or self.document_revision < 0:
            raise ValueError("invalid_document_revision")


@dataclass(frozen=True)
class TypeAction:
    element_ref: str
    document_revision: int
    text: str
    clear_first: bool = False

    def __post_init__(self) -> None:
        _require_text(self.element_ref, "element_ref")
        if not isinstance(self.document_revision, int) or isinstance(self.document_revision, bool) or self.document_revision < 0:
            raise ValueError("invalid_document_revision")
        if not isinstance(self.text, str) or "\x00" in self.text:
            raise ValueError("invalid_type_text")


@dataclass(frozen=True)
class ScrollAction:
    element_ref: str | None
    document_revision: int
    dx: int = 0
    dy: int = 0

    def __post_init__(self) -> None:
        if self.element_ref is not None:
            _require_text(self.element_ref, "element_ref")
        if not isinstance(self.document_revision, int) or isinstance(self.document_revision, bool) or self.document_revision < 0:
            raise ValueError("invalid_document_revision")


@dataclass(frozen=True)
class ScreenshotAction:
    full_page: bool = False


@dataclass(frozen=True)
class BrowserElementSummary:
    element_ref: str
    role: str
    accessible_name: str
    text_excerpt: str
    disabled: bool = False
    # No DOM HTML, no form value, no hidden/script/style, no executable attrs.


@dataclass(frozen=True)
class BrowserActionResult:
    action_type: str
    status: str  # success | error | timeout
    url: str | None = None
    title: str | None = None
    text: str | None = None
    elements: tuple[BrowserElementSummary, ...] = ()
    screenshot_ref: str | None = None
    warning_code: str | None = None
    error_code: str | None = None
    duration_ms: int = 0
    document_revision: int = 0


@dataclass(frozen=True)
class BrowserState:
    safe_url: str | None
    title: str | None
    status: BrowserSessionStatus
    document_revision: int
    latest_screenshot_ref: str | None
    last_action_at: datetime | None = None


class BrowserScreenshotConsumer(str, Enum):
    DASHBOARD_INTERNAL = "dashboard_internal"
    LLM_PROVIDER = "llm_provider"
    EXTERNAL_TOOL = "external_tool"
    EXTERNAL_MEMORY = "external_memory"
    OBSERVATION_LOG = "observation_log"
    USAGE_RETENTION = "usage_retention"
    CLIENT_RESPONSE = "client_response"


class BrowserBackend(Protocol):
    async def create_session(self, session: BrowserSession) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
    async def execute_action(self, session_id: str, action: Any) -> BrowserActionResult: ...
    async def get_state(self, session_id: str) -> BrowserState: ...
    async def begin_takeover(self, session_id: str) -> str | None: ...
    async def end_takeover(self, session_id: str) -> None: ...


class BrowserSessionRegistry(Protocol):
    async def create(self, session: BrowserSession) -> None: ...
    async def get(self, session_id: str) -> BrowserSession | None: ...
    async def list_by_n_agent_session(self, n_agent_session_id: str) -> list[BrowserSession]: ...
    async def compare_and_set_status(
        self,
        session_id: str,
        expected: BrowserSessionStatus,
        next_status: BrowserSessionStatus,
        *,
        pre_takeover_status: BrowserSessionStatus | None = None,
        document_revision: int | None = None,
    ) -> BrowserSession | None: ...
    async def acquire_profile_lease(self, profile_ref: str, session_id: str) -> bool: ...
    async def release_profile_lease(self, profile_ref: str) -> None: ...
    async def append_action_summary(self, session_id: str, summary: dict[str, Any]) -> None: ...
    async def list_actions(self, session_id: str, limit: int) -> list[dict[str, Any]]: ...
    async def count_actions(self, session_id: str) -> int: ...
    async def close(self, session_id: str) -> None: ...

