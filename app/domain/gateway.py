from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


class InteractionSourceType(str, Enum):
    CLI = "cli"
    FEISHU = "feishu"
    DASHBOARD = "dashboard"
    API = "api"


@dataclass(frozen=True)
class GatewaySessionKey:
    source_type: InteractionSourceType
    source_id: str
    thread_id: str = ""
    display_name: str = ""

    @property
    def conversation_parts(self) -> tuple[str, str, str]:
        return (self.source_type.value, self.source_id, self.thread_id)


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

    async def mark_event_processed(self, source_type: InteractionSourceType, event_id: str, message_id: str = "") -> bool:
        ...
