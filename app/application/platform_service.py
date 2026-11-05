from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.application.schedule_delivery_service import ScheduleDeliveryService
from app.domain.gateway import GatewayConversation, GatewaySessionRegistry
from app.domain.platform import Platform, PlatformDescriptor, PlatformLifecycle, PlatformKind, PlatformRegistry


class PlatformServiceError(Exception):
    pass


class PlatformInvalidError(PlatformServiceError):
    def __init__(self, platform: str):
        super().__init__(platform)
        self.platform = platform


class PlatformNotFoundError(PlatformServiceError):
    def __init__(self, platform: str):
        super().__init__(platform)
        self.platform = platform


@dataclass(frozen=True)
class PlatformView:
    platform: Platform
    display_name: str
    kind: PlatformKind
    status: str
    error_code: str | None
    error_message: str | None
    config_summary: dict[str, Any]
    session_count: int
    last_active_at: datetime | None


@dataclass(frozen=True)
class PlatformDetail:
    platform: PlatformView
    total_sessions: int
    active_sessions: int


@dataclass(frozen=True)
class PaginatedSessions:
    items: list[GatewayConversation]
    total: int
    limit: int
    offset: int
    projections: dict[str, dict[str, Any]] = field(default_factory=dict)


class PlatformService:
    def __init__(self, platform_registry: PlatformRegistry, gateway_registry: GatewaySessionRegistry,
                 delivery_service: ScheduleDeliveryService | None = None):
        self.platform_registry = platform_registry
        self.gateway_registry = gateway_registry
        self.delivery_service = delivery_service

    async def list_platforms(self) -> list[PlatformView]:
        views = []
        for descriptor in self.platform_registry.list():
            views.append(await self._view(descriptor))
        return views

    async def get_platform(self, platform_str: str) -> PlatformDetail:
        platform = self._parse_platform(platform_str)
        descriptor = self.platform_registry.get(platform)
        if descriptor is None:
            raise PlatformNotFoundError(platform_str)
        view = await self._view(descriptor)
        conversations = await self.gateway_registry.list_conversations(platform, limit=100000, offset=0)
        return PlatformDetail(
            platform=view,
            total_sessions=view.session_count,
            active_sessions=sum(1 for conversation in conversations if conversation.active_session_id),
        )

    async def list_platform_sessions(self, platform_str: str, limit: int, offset: int) -> PaginatedSessions:
        platform = self._parse_platform(platform_str)
        if self.platform_registry.get(platform) is None:
            raise PlatformNotFoundError(platform_str)
        items = await self.gateway_registry.list_conversations(platform, limit=limit, offset=offset)
        if self.delivery_service is not None:
            projections = await self.delivery_service.project_conversations(items)
        else:
            projections = {item.id: await self.project_conversation(item) for item in items}
        return PaginatedSessions(
            items=items,
            projections=projections,
            total=await self.gateway_registry.count_conversations(platform),
            limit=limit,
            offset=offset,
        )

    async def get_platform_session(self, platform_str: str, conversation_id: str) -> GatewayConversation:
        platform = self._parse_platform(platform_str)
        if self.platform_registry.get(platform) is None:
            raise PlatformNotFoundError(platform_str)
        row = await self.gateway_registry.get_conversation(conversation_id)
        if row is None or row.platform != platform:
            raise PlatformNotFoundError(platform_str)
        return row

    async def project_conversation(self, row: GatewayConversation) -> dict[str, Any]:
        if self.delivery_service is not None:
            return await self.delivery_service.project_conversation(row)
        descriptor = self.platform_registry.get(row.platform)
        status = self._compose_status(descriptor, self.platform_registry.get_lifecycle(row.platform))[0]
        label = "飞书聊天（发送到父聊天）" if row.thread_id else "飞书聊天"
        return dict(delivery_available=False, unavailable_reason="delivery_unconfigured",
                    label=label, parent_chat=None, is_representative=False,
                    delivery_label=label, delivery_parent_chat=None,
                    delivery_unavailable_reason="delivery_unconfigured", delivery_is_representative=False,
                    platform_status=status)

    async def _view(self, descriptor: PlatformDescriptor) -> PlatformView:
        status, error_code, error_message = self._compose_status(
            descriptor,
            self.platform_registry.get_lifecycle(descriptor.platform),
        )
        return PlatformView(
            platform=descriptor.platform,
            display_name=descriptor.display_name,
            kind=descriptor.kind,
            status=status,
            error_code=error_code,
            error_message=error_message,
            config_summary=dict(descriptor.config_summary),
            session_count=await self.gateway_registry.count_conversations(descriptor.platform),
            last_active_at=await self.gateway_registry.get_last_active(descriptor.platform),
        )

    def _parse_platform(self, platform_str: str) -> Platform:
        try:
            return Platform(platform_str)
        except ValueError as exc:
            raise PlatformInvalidError(platform_str) from exc

    def _compose_status(
        self,
        descriptor: PlatformDescriptor | None,
        lifecycle: PlatformLifecycle | None,
    ) -> tuple[str, str | None, str | None]:
        if descriptor is None:
            return "disabled", None, None
        if lifecycle is None:
            return "configured", None, None
        fatal = lifecycle.fatal_error()
        if fatal is not None:
            return "fatal", fatal[0], fatal[1]
        if lifecycle.is_connected():
            return "connected", None, None
        return "disconnected", None, None
