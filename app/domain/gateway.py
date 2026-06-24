from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from app.domain.platform import Platform


class GatewayConfirmationChoice(str, Enum):
    ONCE = "once"
    TRUST_SESSION = "trust_session"
    CANCEL = "cancel"


class GatewayConfirmationAction(str, Enum):
    NEW = "new"
    RENAME = "rename"
    DELETE = "delete"
    SCHEDULE_REMOVE = "schedule_remove"


@dataclass(frozen=True)
class GatewaySessionKey:
    platform: Platform
    platform_session_id: str
    thread_id: str = ""
    display_name: str = ""

    @property
    def conversation_parts(self) -> tuple[str, str, str]:
        return (self.platform.value, self.platform_session_id, self.thread_id)


@dataclass(frozen=True)
class InteractionMessage:
    id: str
    session_key: GatewaySessionKey
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewayOutboundMessage:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InteractionResponse:
    session_id: str
    messages: list[GatewayOutboundMessage]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GatewaySessionLink:
    conversation_id: str
    session_id: str
    display_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class GatewayConversation:
    id: str
    platform: Platform
    platform_session_id: str
    thread_id: str
    display_name: str
    active_session_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GatewayHomeTarget:
    platform: Platform
    receive_id: str
    receive_id_type: str
    thread_id: str = ""
    display_name: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class GatewayConfirmationRequest:
    id: str
    session_key: GatewaySessionKey
    actor_id: str
    session_id: str
    target_session_id: str
    action: GatewayConfirmationAction
    command: str
    args: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    trusted_metadata: dict[str, Any] = field(default_factory=dict)


class GatewaySessionRegistry(Protocol):
    async def get_active_session(self, key: GatewaySessionKey) -> GatewaySessionLink | None:
        ...

    async def create_session_link(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        ...

    async def set_active_session(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        ...

    async def list_session_links(self, key: GatewaySessionKey) -> list[GatewaySessionLink]:
        ...

    async def delete_session_link(self, session_id: str) -> None:
        ...

    async def mark_event_processed(self, platform: Platform, event_id: str, message_id: str = "") -> bool:
        ...

    async def set_home_target(self, target: GatewayHomeTarget) -> GatewayHomeTarget:
        ...

    async def get_home_target(self, platform: Platform) -> GatewayHomeTarget | None:
        ...

    async def list_conversations(
        self, platform: Platform | None = None, limit: int = 100, offset: int = 0
    ) -> list[GatewayConversation]:
        ...

    async def count_conversations(self, platform: Platform) -> int:
        ...

    async def get_last_active(self, platform: Platform) -> datetime | None:
        ...
