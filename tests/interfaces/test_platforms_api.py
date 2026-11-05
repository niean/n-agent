from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.platform_service import PlatformService
from app.domain.gateway import GatewayConversation
from app.domain.platform import Platform, PlatformDescriptor, PlatformKind
from app.infrastructure.registry.in_memory_platform_registry import InMemoryPlatformRegistry
from app.interfaces.http.platforms import create_platforms_router


class FakeLifecycle:
    def __init__(self, *, connected=False, fatal=None):
        self.connected = connected
        self.fatal = fatal

    def is_connected(self):
        return self.connected

    def fatal_error(self):
        return self.fatal


class FakeGatewayRegistry:
    def __init__(self):
        now = datetime(2026, 6, 17, tzinfo=timezone.utc)
        self.conversations = [
            GatewayConversation("conv-1", Platform.FEISHU, "oc_123456789", "", "群聊", "s1", now, now),
            GatewayConversation("conv-2", Platform.FEISHU, "oc_987654321", "thread-1", "Thread", None, now, now),
        ]

    async def get_conversation(self, identifier):
        return next((row for row in self.conversations if row.id == identifier), None)

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


def _client(*, with_delivery=False):
    platform_registry = InMemoryPlatformRegistry(
        [
            PlatformDescriptor(Platform.FEISHU, "飞书", PlatformKind.IM, {"app_id_suffix": "cli_****"}),
        ],
        {Platform.FEISHU: FakeLifecycle(connected=True)},
    )
    gateway = FakeGatewayRegistry()
    delivery = None
    if with_delivery:
        from app.application.schedule_delivery_service import ScheduleDeliveryService
        from dataclasses import replace
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        gateway.conversations = [replace(row, receive_id_type='chat_id') for row in gateway.conversations]
        gateway.get_home_target = AsyncMock(return_value=None)
        gateway.find_conversations = AsyncMock(side_effect=lambda platform, address: [
            row for row in gateway.conversations
            if row.platform == platform and row.platform_session_id == address
        ])
        delivery = ScheduleDeliveryService(gateway, SimpleNamespace(list=AsyncMock(return_value=[])), lambda _: 'connected')
    app = FastAPI()
    app.include_router(create_platforms_router(PlatformService(platform_registry, gateway, delivery)))
    return TestClient(app)


def test_list_gateways_excludes_local_by_default():
    response = _client().get("/chat/gateways")

    assert response.status_code == 200
    payload = response.json()
    assert [item["platform"] for item in payload["platforms"]] == ["feishu"]
    assert payload["platforms"][0]["status"] == "connected"
    assert payload["platforms"][0]["session_count"] == 2


def test_list_gateways_returns_only_registered_external_platforms():
    response = _client().get("/chat/gateways")

    assert response.status_code == 200
    assert [item["platform"] for item in response.json()["platforms"]] == ["feishu"]


def test_get_gateway_detail_and_sessions_mask_platform_session_id():
    client = _client()

    detail = client.get("/chat/gateways/feishu")
    sessions = client.get("/chat/gateways/feishu/sessions?limit=1&offset=0")

    assert detail.status_code == 200
    assert detail.json()["total_sessions"] == 2
    assert detail.json()["active_sessions"] == 1
    assert sessions.status_code == 200
    assert sessions.json()["items"][0]["platform_session_id"] == "oc_12345****"
    assert sessions.json()["total"] == 2


def test_gateway_api_returns_404_and_422():
    client = _client()

    assert client.get("/chat/gateways/dingtalk").status_code == 404
    assert client.get("/chat/gateways/not-a-platform").status_code == 422


def test_single_session_has_candidate_fields_and_missing_is_404():
    client = _client()
    response = client.get('/chat/gateways/feishu/sessions/conv-2')
    assert response.status_code == 200
    assert response.json()['id'] == 'conv-2'
    assert response.json()['delivery_available'] is False
    assert 'label' in response.json()
    assert client.get('/chat/gateways/feishu/sessions/missing').status_code == 404
    assert client.get('/chat/gateways/not-a-platform/sessions/conv-2').status_code == 422


def test_delivery_alias_contract_is_identical_in_real_list_and_single_responses():
    for configured in (False, True):
        client = _client(with_delivery=configured)
        page = client.get('/chat/gateways/feishu/sessions').json()
        if configured:
            assert len({item['delivery_parent_chat'] for item in page['items']}) == 2
        for item in page['items']:
            single = client.get('/chat/gateways/feishu/sessions/' + item['id']).json()
            assert single == item
            assert item['delivery_available'] is configured
            for field in ('label', 'unavailable_reason', 'parent_chat', 'is_representative'):
                assert item['delivery_' + field] == item[field]
            assert item['delivery_unavailable_reason'] == (None if configured else 'delivery_unconfigured')
            assert 'receive_id' not in item
            assert 'oc_123456789' not in str(item)
            assert 'oc_987654321' not in str(item)
