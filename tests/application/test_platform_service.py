from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.application.platform_service import PlatformInvalidError, PlatformNotFoundError, PlatformService
from app.domain.gateway import GatewayConversation
from app.domain.platform import Platform, PlatformDescriptor, PlatformKind
from app.infrastructure.registry.in_memory_platform_registry import InMemoryPlatformRegistry


class FakeLifecycle:
    def __init__(self, *, connected: bool = False, fatal: tuple[str, str] | None = None):
        self.connected = connected
        self.fatal = fatal

    def is_connected(self) -> bool:
        return self.connected

    def fatal_error(self) -> tuple[str, str] | None:
        return self.fatal


class FakeGatewayRegistry:
    def __init__(self, conversations: list[GatewayConversation] | None = None):
        self.conversations = list(conversations or [])

    async def get_active_session(self, key):
        return None

    async def create_session_link(self, key, session_id):
        raise NotImplementedError

    async def set_active_session(self, key, session_id):
        raise NotImplementedError

    async def list_session_links(self, key):
        return []

    async def delete_session_link(self, session_id):
        return None

    async def mark_event_processed(self, platform, event_id, message_id=""):
        return True

    async def list_conversations(self, platform=None, limit=100, offset=0):
        rows = [c for c in self.conversations if platform is None or c.platform is platform]
        return rows[offset : offset + limit]

    async def count_conversations(self, platform):
        return len([c for c in self.conversations if c.platform is platform])

    async def get_last_active(self, platform):
        rows = [c for c in self.conversations if c.platform is platform]
        return max((c.updated_at for c in rows), default=None)


def _descriptor(platform=Platform.FEISHU, kind=PlatformKind.IM):
    return PlatformDescriptor(platform, platform.value, kind, {"configured": True})


def _conversation(platform: Platform, platform_session_id: str, *, active_session_id: str | None = "s1"):
    now = datetime.now(timezone.utc)
    return GatewayConversation(
        id=f"conv-{platform_session_id}",
        platform=platform,
        platform_session_id=platform_session_id,
        thread_id="",
        display_name=platform_session_id,
        active_session_id=active_session_id,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_list_platforms_returns_registered_external_platform_counts():
    registry = InMemoryPlatformRegistry([
        _descriptor(Platform.FEISHU),
    ])
    gateway = FakeGatewayRegistry([_conversation(Platform.FEISHU, "oc_a")])
    service = PlatformService(registry, gateway)

    visible = await service.list_platforms()

    assert [view.platform for view in visible] == [Platform.FEISHU]
    assert visible[0].status == "configured"
    assert visible[0].session_count == 1
    assert visible[0].last_active_at is not None


@pytest.mark.asyncio
async def test_platform_statuses_cover_configured_connected_disconnected_and_fatal():
    descriptor = _descriptor()
    gateway = FakeGatewayRegistry()

    configured = PlatformService(InMemoryPlatformRegistry([descriptor]), gateway)
    assert (await configured.list_platforms())[0].status == "configured"

    connected = PlatformService(InMemoryPlatformRegistry([descriptor], {Platform.FEISHU: FakeLifecycle(connected=True)}), gateway)
    assert (await connected.list_platforms())[0].status == "connected"

    disconnected = PlatformService(InMemoryPlatformRegistry([descriptor], {Platform.FEISHU: FakeLifecycle()}), gateway)
    assert (await disconnected.list_platforms())[0].status == "disconnected"

    fatal = PlatformService(
        InMemoryPlatformRegistry([descriptor], {Platform.FEISHU: FakeLifecycle(fatal=("boom", "failed"))}),
        gateway,
    )
    fatal_view = (await fatal.list_platforms())[0]
    assert fatal_view.status == "fatal"
    assert fatal_view.error_code == "boom"
    assert fatal_view.error_message == "failed"


@pytest.mark.asyncio
async def test_get_platform_returns_detail_with_active_session_count():
    conversations = [
        _conversation(Platform.FEISHU, "oc_a", active_session_id="s1"),
        _conversation(Platform.FEISHU, "oc_b", active_session_id=None),
        _conversation(Platform.DINGTALK, "ding-a", active_session_id="s3"),
    ]
    service = PlatformService(InMemoryPlatformRegistry([_descriptor()]), FakeGatewayRegistry(conversations))

    detail = await service.get_platform("feishu")

    assert detail.platform.platform is Platform.FEISHU
    assert detail.total_sessions == 2
    assert detail.active_sessions == 1


@pytest.mark.asyncio
async def test_list_platform_sessions_paginates_registered_platform():
    conversations = [
        replace(_conversation(Platform.FEISHU, "oc_a"), updated_at=datetime(2026, 6, 17, 3, tzinfo=timezone.utc)),
        replace(_conversation(Platform.FEISHU, "oc_b"), updated_at=datetime(2026, 6, 17, 2, tzinfo=timezone.utc)),
    ]
    service = PlatformService(InMemoryPlatformRegistry([_descriptor()]), FakeGatewayRegistry(conversations))

    page = await service.list_platform_sessions("feishu", limit=1, offset=1)

    assert page.total == 2
    assert page.limit == 1
    assert page.offset == 1
    assert [item.platform_session_id for item in page.items] == ["oc_b"]


@pytest.mark.asyncio
async def test_platform_service_rejects_invalid_and_unregistered_platforms():
    service = PlatformService(InMemoryPlatformRegistry([_descriptor()]), FakeGatewayRegistry())

    with pytest.raises(PlatformInvalidError):
        await service.get_platform("unknown")
    with pytest.raises(PlatformNotFoundError):
        await service.get_platform("dingtalk")
