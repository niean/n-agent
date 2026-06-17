from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

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


class PlatformService:
    def __init__(self, platform_registry: PlatformRegistry, gateway_registry: GatewaySessionRegistry):
        self.platform_registry = platform_registry
        self.gateway_registry = gateway_registry

    async def list_platforms(self, include_local: bool = False) -> list[PlatformView]:
        views = []
        for descriptor in self.platform_registry.list():
            if not include_local and descriptor.kind is PlatformKind.LOCAL:
                continue
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
        return PaginatedSessions(
            items=await self.gateway_registry.list_conversations(platform, limit=limit, offset=offset),
            total=await self.gateway_registry.count_conversations(platform),
            limit=limit,
            offset=offset,
        )

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
