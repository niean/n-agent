from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.model_service import ModelService
from app.application.provider_service import ProviderCreateInput, ProviderService, ProviderUpdateInput
from app.application.schedule_service import ScheduledTaskNotFoundError, ScheduleValidationError
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import LLMResult, ModelInfo, ProviderConfig
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router


class _StubExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    def __init__(self, models=None):
        self._models = models if models is not None else [
            ModelInfo("real-1", "Real 1", "openai-compatible", True, True),
            ModelInfo("real-2", "Real 2", "openai-compatible", False, True),
        ]

    async def list_models(self):
        return list(self._models)

    async def supports_tools(self, model: str):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _default_health() -> dict:
    return {
        "provider": {"status": "ok"},
        "memory": {"status": "ok"},
        "knowledge": {"status": "disabled", "enabled": False},
    }


class _FakeScheduleService:
    def __init__(self):
        self.tasks = []
        self.created_requests = []
        self.updated_requests = []
        self.run_errors = {}

    async def list(self):
        return self.tasks

    async def create(self, request):
        from app.domain.schedule import DeliveryTarget, ScheduledTask, ScheduleExpression, ScheduleTimezone
        from datetime import datetime, timezone

        self.created_requests.append(request)
        task = ScheduledTask(
            id=f"sched-{len(self.tasks) + 1}",
            name=request.name,
            prompt=request.prompt,
            schedule=ScheduleExpression(request.cron_expression),
            timezone=ScheduleTimezone(request.timezone),
            session_id=request.session_id or "session-1",
            delivery_target=DeliveryTarget.silent() if request.delivery_target == "silent" else DeliveryTarget.dashboard(),
            next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
            created_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
            updated_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        )
        self.tasks.append(task)
        return task

    async def get(self, task_id):
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ScheduledTaskNotFoundError(task_id)

    async def update(self, task_id, request):
        from app.domain.schedule import DeliveryTarget, ScheduledTask, ScheduleExpression, ScheduleTimezone

        task = await self.get(task_id)
        self.updated_requests.append(request)
        updated = ScheduledTask(
            **{
                **task.__dict__,
                "name": request.name if request.name is not None else task.name,
                "prompt": request.prompt if request.prompt is not None else task.prompt,
                "schedule": ScheduleExpression(request.cron_expression or task.schedule.value),
                "timezone": ScheduleTimezone(request.timezone or task.timezone.value),
                "session_id": request.session_id if request.session_id is not None else task.session_id,
                "delivery_target": DeliveryTarget.silent() if request.delivery_target == "silent" else task.delivery_target,
            }
        )
        self.tasks = [updated if item.id == task_id else item for item in self.tasks]
        return updated

    async def list_executions(self, task_id, limit=10):
        from app.domain.schedule import ScheduledTaskExecution, ScheduledTaskExecutionStatus
        from datetime import datetime, timezone

        await self.get(task_id)
        if limit < 1 or limit > 50:
            raise ScheduleValidationError("invalid limit")
        return [
            ScheduledTaskExecution(
                id="execution-1",
                task_id=task_id,
                session_id="session-1",
                claim_id="claim-1",
                lease_owner="owner-1",
                status=ScheduledTaskExecutionStatus.SUCCEEDED,
                started_at=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
                completed_at=datetime(2026, 6, 16, 0, 1, tzinfo=timezone.utc),
                output="done",
                delivery_status="success",
                created_at=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
            )
        ][:limit]

    async def pause(self, task_id):
        return await self.get(task_id)

    async def resume(self, task_id):
        return await self.get(task_id)

    async def run_now(self, task_id):
        if task_id in self.run_errors:
            raise self.run_errors[task_id]
        return {"status": "ok"}

    async def delete(self, task_id):
        await self.get(task_id)
        return True



class _ChatProvider:
    """Minimal provider for dashboard chat tests."""
    async def list_models(self):
        return [ModelInfo("test-model", "test-model", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        return LLMResult({"role": "assistant", "content": "hello"}, "stop")


def _build_app(store, health=None, model_service=None, provider_service=None, schedule_service=None, chat_service=None):
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = model_service or ModelService(_StubProvider(), "real-1")
    if chat_service is None:
        runner = AgentGraphRunner(
            _ChatProvider(),
            ToolService(_StubExecutor(), builtin_tool_definitions()),
            store,
            HeuristicSummarizer(),
        )
        chat_service = ChatCompletionService(store, runner, SessionService(store))
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store),
        tool_service,
        model_service,
        health or _default_health,
        provider_service=provider_service,
        schedule_service=schedule_service,
        memory_store=store,
        chat_service=chat_service,
    ))
    return app


class _StubHolder:
    def __init__(self):
        self.swaps = []

    async def swap(self, cfg, api_key):
        self.swaps.append((cfg.id, api_key))


def test_chat_page_and_apis(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="s1", title="S1"))
        await store.lock_session_external_memory("s1", ["builtin", "project_memory_1"])
        await store.append_message("s1", ConversationMessage(role="user", content="hello"))
        await store.append_message(
            "s1",
            ConversationMessage(
                role="assistant",
                content={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": "{}"},
                        }
                    ],
                },
            ),
        )
        await store.save_summary(Summary(session_id="s1", summary="summary"))
        await store.save_task_state(TaskState(session_id="s1", status="completed", iteration_count=1))
        await store.save_tool_call(ToolCall(id="call-1", session_id="s1", tool_name="calculator", arguments={}, status="success"))

    asyncio.run(seed())
    app = _build_app(store)
    client = TestClient(app)

    html = client.get("/chat")
    sessions = client.get("/chat/sessions")
    detail = client.get("/chat/sessions/s1")
    tool_calls = client.get("/chat/sessions/s1/tool-calls")

    assert html.status_code == 200
    assert "<aside" in html.text and "sidebar" in html.text
    assert "/static/app.js" in html.text
    assert sessions.json()[0]["id"] == "s1"
    detail_body = detail.json()
    assert detail_body["session"]["external_memory_enabled"] == ["builtin", "project_memory_1"]
    assert detail_body["summary"]["summary"] == "summary"
    assistant = next(message for message in detail_body["messages"] if message["role"] == "assistant")
    assert assistant["content"] == ""
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert tool_calls.json()[0]["tool_name"] == "calculator"


def test_chat_sessions_includes_feishu_gateway_sessions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="feishu-session", title="飞书会话", source="feishu"))

    asyncio.run(seed())
    client = TestClient(_build_app(store))

    response = client.get("/chat/sessions")

    assert response.status_code == 200
    assert {item["id"]: item["source"] for item in response.json()}["feishu-session"] == "feishu"


def test_scheduled_tasks_routes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    schedule = _FakeScheduleService()
    client = TestClient(_build_app(store, schedule_service=schedule))

    shell = client.get("/scheduled-tasks")
    created = client.post("/chat/scheduled-tasks", json={"name": "Daily", "prompt": "summarize", "cron_expression": "*/5 * * * *", "timezone": "Asia/Shanghai"})
    listed = client.get("/chat/scheduled-tasks")
    run = client.post("/chat/scheduled-tasks/sched-1/run")

    assert shell.status_code == 200
    assert created.json()["id"] == "sched-1"
    assert listed.json()[0]["timezone"] == "Asia/Shanghai"
    assert run.json()["status"] == "ok"



def test_scheduled_task_update_and_execution_routes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    schedule = _FakeScheduleService()
    client = TestClient(_build_app(store, schedule_service=schedule))
    created = client.post("/chat/scheduled-tasks", json={"name": "Daily", "prompt": "summarize", "cron_expression": "*/5 * * * *"}).json()

    patched = client.patch(f"/chat/scheduled-tasks/{created['id']}", json={"name": "Updated", "delivery_target": "silent", "session_id": "session-2"})
    executions = client.get(f"/chat/scheduled-tasks/{created['id']}/executions?limit=10")
    invalid = client.get(f"/chat/scheduled-tasks/{created['id']}/executions?limit=0")

    assert patched.status_code == 200
    assert patched.json()["name"] == "Updated"
    assert patched.json()["delivery_target"] == "silent"
    assert patched.json()["session_id"] == "session-2"
    assert executions.status_code == 200
    assert executions.json()[0]["id"] == "execution-1"
    assert invalid.status_code == 422


def test_scheduled_task_routes_pass_allowed_tools_grant(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    schedule = _FakeScheduleService()
    client = TestClient(_build_app(store, schedule_service=schedule))

    created = client.post(
        "/chat/scheduled-tasks",
        json={
            "name": "photo",
            "prompt": "拍照上传",
            "cron_expression": "0 10,18 * * *",
            "allowed_tools": ["host_terminal"],
        },
    )
    assert created.status_code == 200
    assert schedule.created_requests[0].allowed_tools == ("host_terminal",)

    patched = client.patch(
        f"/chat/scheduled-tasks/{created.json()['id']}",
        json={"allowed_tools": "host_terminal,other"},
    )
    assert patched.status_code == 200
    assert schedule.updated_requests[0].allowed_tools == ("host_terminal", "other")


def test_scheduled_task_origin_payloads_are_protected(tmp_path):
    from app.domain.schedule import DeliveryTarget, ScheduledTask, ScheduleExpression, ScheduleTimezone
    from datetime import datetime, timezone

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    schedule = _FakeScheduleService()
    origin_task = ScheduledTask(
        id="origin-1",
        name="Origin",
        prompt="prompt",
        schedule=ScheduleExpression("0 9 * * *"),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id="session-origin",
        delivery_target=DeliveryTarget.origin({"receive_id": "chat-1", "receive_id_type": "chat_id"}),
        origin={"receive_id": "chat-1", "receive_id_type": "chat_id"},
        next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )
    schedule.tasks.append(origin_task)
    client = TestClient(_build_app(store, schedule_service=schedule))

    create_origin = client.post("/chat/scheduled-tasks", json={"name": "x", "prompt": "p", "cron_expression": "* * * * *", "delivery_target": "origin"})
    create_context = client.post("/chat/scheduled-tasks", json={"name": "x", "prompt": "p", "cron_expression": "* * * * *", "origin": {"receive_id": "x"}})
    patched = client.patch(
        "/chat/scheduled-tasks/origin-1",
        json={"name": "Renamed", "delivery_target": "silent", "session_id": "changed", "origin": {"receive_id": "changed"}, "delivery_context": {"receive_id": "changed"}},
    )

    assert create_origin.status_code == 422
    assert create_origin.json()["error"]["code"] == "scheduled_task_delivery_context_invalid"
    assert create_context.status_code == 422
    assert patched.status_code == 200
    assert schedule.updated_requests[-1].name == "Renamed"
    assert schedule.updated_requests[-1].delivery_target is None
    assert schedule.updated_requests[-1].session_id is None
    assert schedule.updated_requests[-1].origin is None


def test_scheduled_task_errors_are_mapped(tmp_path):
    class ErrorSchedule(_FakeScheduleService):
        async def get(self, task_id):
            raise ScheduledTaskNotFoundError(task_id)

        async def create(self, request):
            raise ScheduleValidationError("bad")

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store, schedule_service=ErrorSchedule()))

    missing = client.get("/chat/scheduled-tasks/missing")
    invalid = client.post("/chat/scheduled-tasks", json={"name": "x"})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "scheduled_task_not_found"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "scheduled_task_invalid"


def test_scheduled_task_run_now_error_contract(tmp_path):
    from app.application.schedule_service import ScheduledTaskNotRunnableError

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    schedule = _FakeScheduleService()
    client = TestClient(_build_app(store, schedule_service=schedule))
    created = client.post("/chat/scheduled-tasks", json={"name": "Daily", "prompt": "summarize", "cron_expression": "*/5 * * * *"}).json()
    schedule.run_errors["missing"] = ScheduledTaskNotFoundError("missing")
    schedule.run_errors["paused"] = ScheduledTaskNotRunnableError("scheduled_task_paused")
    schedule.run_errors["session-missing"] = ScheduledTaskNotRunnableError("scheduled_task_session_missing")
    schedule.run_errors["claimed"] = ScheduledTaskNotRunnableError("scheduled_task_claim_conflict")

    ok = client.post(f"/chat/scheduled-tasks/{created['id']}/run")
    missing = client.post("/chat/scheduled-tasks/missing/run")
    paused = client.post("/chat/scheduled-tasks/paused/run")
    session_missing = client.post("/chat/scheduled-tasks/session-missing/run")
    claimed = client.post("/chat/scheduled-tasks/claimed/run")

    assert ok.status_code == 200
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "scheduled_task_not_found"
    assert paused.status_code == 409
    assert paused.json()["error"]["code"] == "scheduled_task_paused"
    assert session_missing.status_code == 409
    assert session_missing.json()["error"]["code"] == "scheduled_task_session_missing"
    assert claimed.status_code == 409
    assert claimed.json()["error"]["code"] == "scheduled_task_claim_conflict"


def test_chat_can_create_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.post("/chat/sessions?session_id=s2")

    assert response.status_code == 200
    assert response.json()["id"] == "s2"


def test_chat_tools_endpoint_returns_definitions(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.get("/chat/tools")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    names = [item["name"] for item in payload]
    assert "get_current_time" in names
    sample = next(item for item in payload if item["name"] == "calculator")
    assert sample["source_type"] == "builtin"
    assert sample["toolset"] == "math"
    assert "description" in sample and "risk_level" in sample
    assert "enabled" in sample and "input_schema" in sample


def test_chat_health_dependencies_endpoint(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    snapshot = {
        "provider": {"status": "ok", "base_url": "http://localhost:11434/v1", "model": "qwen2.5"},
        "memory": {"status": "ok", "path": str(tmp_path / "sessions.db")},
        "knowledge": {"status": "ok", "base_url": "http://localhost:8202", "enabled": True},
    }
    client = TestClient(_build_app(store, health=lambda: snapshot))

    response = client.get("/chat/health/dependencies")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"]["status"] == "ok"
    assert payload["memory"]["status"] == "ok"
    assert payload["knowledge"]["enabled"] is True


def test_admin_models_endpoint_returns_real_provider_models(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    provider = _StubProvider([
        ModelInfo("real-1", "Real 1", "openai-compatible", True, True),
        ModelInfo("real-2", "Real 2", "openai-compatible", False, True),
    ])
    model_service = ModelService(provider, "real-1")
    client = TestClient(_build_app(store, model_service=model_service))

    response = client.get("/chat/models")

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["default_model"] == "real-1"
    assert isinstance(payload["data"], list) and len(payload["data"]) == 2

    by_id = {item["id"]: item for item in payload["data"]}
    assert set(by_id.keys()) == {"real-1", "real-2"}
    for item in payload["data"]:
        assert set(item.keys()) == {
            "id", "display_name", "provider", "supports_tools", "supports_streaming", "is_default",
        }
    assert by_id["real-1"]["is_default"] is True
    assert by_id["real-1"]["display_name"] == "Real 1"
    assert by_id["real-1"]["provider"] == "openai-compatible"
    assert by_id["real-1"]["supports_tools"] is True
    assert by_id["real-2"]["is_default"] is False
    assert by_id["real-2"]["supports_tools"] is False


def _build_provider_app(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    holder = _StubHolder()
    provider_service = ProviderService(registry, holder)
    app = _build_app(store, provider_service=provider_service)
    return app, holder, registry


def test_provider_routes_full_lifecycle(tmp_path):
    app, holder, _ = _build_provider_app(tmp_path)
    client = TestClient(app)

    create = client.post(
        "/chat/providers",
        json={"name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1"},
    )
    assert create.status_code == 200
    body = create.json()
    assert "api_key" not in body
    assert body["api_key_present"] is True
    pid = body["id"]

    listed = client.get("/chat/providers").json()
    assert len(listed) == 1 and listed[0]["id"] == pid
    assert "api_key" not in listed[0]

    activated = client.post(f"/chat/providers/{pid}/activate")
    assert activated.status_code == 200
    assert activated.json()["is_active"] is True
    assert holder.swaps and holder.swaps[-1][0] == pid

    delete_active = client.delete(f"/chat/providers/{pid}")
    assert delete_active.status_code == 409
    assert delete_active.json()["error"]["code"] == "provider_in_use"

    patched = client.patch(f"/chat/providers/{pid}", json={"model": "m2"})
    assert patched.status_code == 200
    assert patched.json()["model"] == "m2"
    assert "api_key" not in patched.json()
    assert holder.swaps[-1][0] == pid


def test_provider_create_supports_vision_default_true_for_openai_compatible(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    create = client.post(
        "/chat/providers",
        json={"name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1"},
    )
    assert create.status_code == 200
    assert create.json()["supports_vision"] is True


def test_provider_create_supports_vision_explicit_false(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    create = client.post(
        "/chat/providers",
        json={
            "name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1",
            "supports_vision": False,
        },
    )
    assert create.status_code == 200
    assert create.json()["supports_vision"] is False


def test_provider_create_supports_vision_string_rejected(tmp_path):
    """字符串 'false' 不能被误当 False，必须 422。"""
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    create = client.post(
        "/chat/providers",
        json={
            "name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1",
            "supports_vision": "false",
        },
    )
    assert create.status_code == 422


def test_provider_update_supports_vision(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    pid = client.post(
        "/chat/providers",
        json={"name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1"},
    ).json()["id"]
    patched = client.patch(
        f"/chat/providers/{pid}",
        json={"supports_vision": False},
    )
    assert patched.status_code == 200
    assert patched.json()["supports_vision"] is False


def test_provider_list_includes_supports_vision(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    client.post(
        "/chat/providers",
        json={"name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1"},
    )
    listed = client.get("/chat/providers").json()
    assert "supports_vision" in listed[0]


def test_provider_create_validation(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)

    invalid = client.post(
        "/chat/providers",
        json={"name": "", "base_url": "http://x", "model": "m", "api_key": "k"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "provider_invalid"

    invalid_type = client.post(
        "/chat/providers",
        json={"name": "P", "base_url": "http://x", "model": "m", "api_key": "k", "provider_type": "foo"},
    )
    assert invalid_type.status_code == 422
    assert invalid_type.json()["error"]["code"] == "provider_invalid"

    missing = client.get("/chat/providers/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "provider_not_found"


def test_provider_duplicate_name_returns_409(tmp_path):
    app, _, _ = _build_provider_app(tmp_path)
    client = TestClient(app)
    payload = {"name": "P1", "base_url": "http://x", "model": "m1", "api_key": "k1"}
    assert client.post("/chat/providers", json=payload).status_code == 200
    dup = client.post("/chat/providers", json=payload)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "provider_duplicate"



def test_provider_activate_invalid_provider_type_returns_422(tmp_path):
    import asyncio
    from datetime import datetime, timezone

    app, holder, registry = _build_provider_app(tmp_path)
    now = datetime.now(timezone.utc)
    cfg = ProviderConfig(
        id="",
        name="Bad",
        provider_type="foo",
        base_url="http://x",
        model="m",
        api_key_present=False,
        is_active=False,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )
    bad = asyncio.run(registry.create_provider(cfg, "k"))
    client = TestClient(app)

    response = client.post(f"/chat/providers/{bad.id}/activate")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_invalid"
    assert holder.swaps == []


def test_chat_session_can_be_renamed(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="s-rename"))

    asyncio.run(seed())
    client = TestClient(_build_app(store))

    response = client.patch("/chat/sessions/s-rename", json={"title": "  新标题  "})

    assert response.status_code == 200
    assert response.json()["title"] == "新标题"


def test_chat_session_rename_validates_title(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    asyncio.run(store.create_session(ConversationSession(id="s1")))
    client = TestClient(_build_app(store))

    response = client.patch("/chat/sessions/s1", json={"title": "   "})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "session_title_invalid"


def test_chat_session_rename_returns_404_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.patch("/chat/sessions/missing", json={"title": "x"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


def test_chat_session_can_be_deleted_with_cascade(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio

    async def seed():
        await store.create_session(ConversationSession(id="s-del"))
        await store.append_message("s-del", ConversationMessage(role="user", content="hi"))

    asyncio.run(seed())
    client = TestClient(_build_app(store))

    response = client.delete("/chat/sessions/s-del")

    assert response.status_code == 204
    assert client.get("/chat/sessions/s-del").json()["session"] is None


def test_chat_session_delete_returns_404_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.delete("/chat/sessions/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# S6: Dashboard /chat/completions route tests
# ---------------------------------------------------------------------------


def test_dashboard_chat_completions_two_rounds_same_session(tmp_path):
    """Create dashboard session -> /chat/completions two rounds -> detail returns same history."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    # Create a dashboard session first
    create = client.post("/chat/sessions?session_id=dash-chat-1")
    assert create.status_code == 200
    assert create.json()["source"] == "dashboard"

    # First round
    r1 = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "dash-chat-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "first"}]},
    )
    assert r1.status_code == 200

    # Second round
    r2 = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "dash-chat-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "second"}]},
    )
    assert r2.status_code == 200

    # Detail should return the same session history
    detail = client.get("/chat/sessions/dash-chat-1").json()
    messages = detail["messages"]
    assert len(messages) >= 2
    assert detail["session"]["source"] == "dashboard"


def test_dashboard_chat_completions_no_session_header_returns_409(tmp_path):
    """Dashboard /chat/completions without X-Session-ID returns 409."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.post(
        "/chat/completions",
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dashboard_session_scope_mismatch"


def test_dashboard_chat_completions_nonexistent_session_returns_409(tmp_path):
    """Dashboard /chat/completions with a non-existent session returns 409 (no implicit create)."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    response = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "does-not-exist"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dashboard_session_scope_mismatch"


def test_dashboard_chat_completions_api_session_returns_409(tmp_path):
    """Dashboard selector cannot select an api session."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio
    from app.domain.session import ConversationSession

    asyncio.run(store.create_session(ConversationSession(id="api-1", source="api")))
    client = TestClient(_build_app(store))

    response = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "api-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dashboard_session_scope_mismatch"


def test_dashboard_chat_completions_streaming(tmp_path):
    """Dashboard /chat/completions streaming returns SSE."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))

    client.post("/chat/sessions?session_id=dash-stream-1")

    with client.stream(
        "POST",
        "/chat/completions",
        headers={"X-Session-ID": "dash-stream-1"},
        json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        text = "".join(response.iter_text())

    assert response.headers["content-type"].startswith("text/event-stream")
    assert "chat.completion.chunk" in text
    assert "data: [DONE]" in text


def test_api_selector_cannot_select_dashboard_session(tmp_path):
    """API selector (OpenAI route) cannot select a dashboard session."""
    from app.application.agent_graph import AgentGraphRunner
    from app.application.chat_service import ChatCompletionService
    from app.application.model_service import ModelService
    from app.application.session_service import SessionService
    from app.application.tool_service import ToolService, builtin_tool_definitions
    from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
    from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
    from app.interfaces.http.openai_compatible import create_openai_compatible_router
    from fastapi import FastAPI

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio
    from app.domain.session import ConversationSession

    asyncio.run(store.create_session(ConversationSession(id="dash-2", source="dashboard")))

    runner = AgentGraphRunner(
        _ChatProvider(),
        ToolService(_StubExecutor(), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
    )
    chat = ChatCompletionService(store, runner, SessionService(store))
    models = ModelService(_ChatProvider(), "test-model")
    app = FastAPI()
    app.include_router(create_openai_compatible_router(chat, models, store))
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Session-ID": "dash-2"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "api_session_scope_mismatch"
