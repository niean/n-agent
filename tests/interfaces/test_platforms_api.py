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


def _client():
    platform_registry = InMemoryPlatformRegistry(
        [
            PlatformDescriptor(Platform.CLI, "CLI", PlatformKind.LOCAL, {}),
            PlatformDescriptor(Platform.FEISHU, "飞书", PlatformKind.IM, {"app_id_suffix": "cli_****"}),
        ],
        {Platform.FEISHU: FakeLifecycle(connected=True)},
    )
    app = FastAPI()
    app.include_router(create_platforms_router(PlatformService(platform_registry, FakeGatewayRegistry())))
    return TestClient(app)


def test_list_gateways_excludes_local_by_default():
    response = _client().get("/chat/gateways")

    assert response.status_code == 200
    payload = response.json()
    assert [item["platform"] for item in payload["platforms"]] == ["feishu"]
    assert payload["platforms"][0]["status"] == "connected"
    assert payload["platforms"][0]["session_count"] == 2


def test_list_gateways_can_include_local():
    response = _client().get("/chat/gateways?include_local=true")

    assert response.status_code == 200
    assert {item["platform"] for item in response.json()["platforms"]} == {"cli", "feishu"}


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
