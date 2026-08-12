from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.policy_dashboard_service import PolicyDashboardService
from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    ContextPolicyConfig,
    DelegationPolicyConfig,
    GatewayPolicyConfig,
    InformationFlowPolicyConfig,
    LLMPolicyConfig,
    MemoryPolicyConfig,
    ResolvedPolicyProfile,
    SandboxPolicyConfig,
    SchedulePolicyConfig,
    ToolPolicyConfig,
    TurnPolicyConfig,
)
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import ModelInfo
from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import create_dashboard_router
from app.interfaces.http.policy_routes import register_policy_routes


class FakeProvider:
    def __init__(self, profile, fail=False):
        self._profile = profile
        self._fail = fail

    def resolve(self, scope_ref, facts):
        if self._fail:
            raise RuntimeError("boom-secret")
        return self._profile


def _full_profile() -> ResolvedPolicyProfile:
    return ResolvedPolicyProfile(
        version="route-v1",
        turn=TurnPolicyConfig(iteration_limit=10, turn_timeout_seconds=900),
        context=ContextPolicyConfig(),
        llm=LLMPolicyConfig(fallback_enabled=False),
        tool=ToolPolicyConfig(),
        memory=MemoryPolicyConfig(),
        sandbox=SandboxPolicyConfig(),
        gateway=GatewayPolicyConfig(),
        schedule=SchedulePolicyConfig(),
        budget=BudgetPolicyConfig(max_usd_cost=Decimal("0.5")),
        information_flow=InformationFlowPolicyConfig(),
        delegation=DelegationPolicyConfig(),
    )


def _route_client(service) -> TestClient:
    app = FastAPI()
    register_policy_routes(app.router, service)
    return TestClient(app)


def test_get_policies_success_contract():
    service = PolicyDashboardService(FakeProvider(_full_profile()))
    res = _route_client(service).get("/chat/policies")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    data = res.json()
    assert data["profile_version"] == "route-v1"
    keys = [p["key"] for p in data["policies"]]
    assert keys == [
        "turn", "context", "llm", "tool", "memory",
        "sandbox", "gateway", "schedule", "budget", "information_flow",
        "delegation",
    ]
    for p in data["policies"]:
        assert set(p.keys()) == {
            "key", "name", "display_name", "dimension", "execution_point", "domain_file", "config",
        }
        for c in p["config"]:
            assert set(c.keys()) == {"key", "label", "value"}


def test_get_policies_failure_returns_fixed_500():
    service = PolicyDashboardService(FakeProvider(None, fail=True))
    res = _route_client(service).get("/chat/policies")
    assert res.status_code == 500
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert body == {"error": {"code": "policy_load_failed",
                              "message": "Policy profile could not be loaded"}}
    # never leak original exception text, config values or env info
    assert "boom" not in res.text
    assert "secret" not in res.text.lower().replace("policy profile could not be loaded", "")


def test_post_policies_returns_405():
    service = PolicyDashboardService(FakeProvider(_full_profile()))
    res = _route_client(service).post("/chat/policies")
    assert res.status_code == 405


def test_put_patch_delete_not_registered():
    service = PolicyDashboardService(FakeProvider(_full_profile()))
    client = _route_client(service)
    for method in ("put", "patch", "delete"):
        res = client.request(method, "/chat/policies", json={})
        assert res.status_code == 405


class _StubExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    async def list_models(self):
        return [ModelInfo("real-1", "Real 1", "openai-compatible", True, True)]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _dashboard_client(tmp_path, policy_dashboard_service):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = ModelService(_StubProvider(), "real-1")
    app = FastAPI()
    app.include_router(create_dashboard_router(
        SessionService(store), tool_service, model_service,
        lambda: {"provider": {"status": "ok"}},
        policy_dashboard_service=policy_dashboard_service,
    ))
    return TestClient(app)


def test_security_shell_and_policy_route_when_wired(tmp_path):
    service = PolicyDashboardService(FakeProvider(_full_profile()))
    client = _dashboard_client(tmp_path, service)
    shell_security = client.get("/security")
    shell_chat = client.get("/chat")
    assert shell_security.status_code == 200
    assert shell_security.text == shell_chat.text  # same index.html shell
    assert 'id="app-sidebar"' in shell_security.text
    res = client.get("/chat/policies")
    assert res.status_code == 200
    assert len(res.json()["policies"]) == 11


def test_policy_route_404_when_not_wired(tmp_path):
    client = _dashboard_client(tmp_path, None)
    # /security shell still loads even without the service...
    assert client.get("/security").status_code == 200
    # ...but /chat/policies is not registered
    assert client.get("/chat/policies").status_code == 404
