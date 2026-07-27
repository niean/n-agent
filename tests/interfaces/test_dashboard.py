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


def test_chat_image_route_serves_stored_image_and_rejects_unsafe_names(tmp_path):
    from app.infrastructure.image_store import LocalImageStore

    image_store = LocalImageStore(tmp_path / "images", "http://localhost:8201")
    image_id = "deadbeef.jpg"
    (tmp_path / "images" / image_id).write_bytes(b"\xff\xd8fakejpeg")

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = ModelService(_StubProvider(), "real-1")
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store), tool_service, model_service, _default_health,
        memory_store=store, image_store=image_store,
    ))
    client = TestClient(app)

    ok = client.get(f"/chat/images/{image_id}")
    assert ok.status_code == 200
    assert ok.content == b"\xff\xd8fakejpeg"
    assert ok.headers["content-type"].startswith("image/jpeg")

    # unsafe / missing names -> 404, never a filesystem read
    assert client.get("/chat/images/nonexistent.png").status_code == 404
    assert client.get("/chat/images/not-valid").status_code == 404


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


def test_dashboard_chat_completions_continues_api_session(tmp_path):
    """Dashboard is a cross-source debug UI: it may continue an existing api session.

    Restores pre-policy-governance behavior; the session's origin source is preserved
    (continuing from the Dashboard must not rewrite an api session into a dashboard one).
    """
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

    assert response.status_code == 200
    assert client.get("/chat/sessions/api-1").json()["session"]["source"] == "api"


def test_dashboard_chat_completions_continues_feishu_session(tmp_path):
    """Regression: Dashboard could not send to a feishu-type session after 8649dc4.

    The Dashboard session list shows every session regardless of source, so the operator
    must be able to continue a feishu conversation from the Dashboard. The feishu session's
    origin source is preserved.
    """
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    import asyncio
    from app.domain.session import ConversationSession

    asyncio.run(store.create_session(ConversationSession(id="feishu-1", source="feishu")))
    client = TestClient(_build_app(store))

    response = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "feishu-1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert client.get("/chat/sessions/feishu-1").json()["session"]["source"] == "feishu"


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


# ---------------------------------------------------------------------------
# T3: Dashboard tool approval route tests
# ---------------------------------------------------------------------------

import asyncio
import json as _json
from contextlib import suppress

import httpx
import pytest
from app.application.events import ChatEvent, ChatEventType
from app.application.gateway_tool_approval_service import GatewayToolApprovalService
from app.domain.tool import ApprovalRequest, RiskLevel
from app.interfaces.http.dashboard_tool_approval import DashboardToolApprovalBridge


class _ApprovalChatService:
    """Fake ChatCompletionService that invokes the approval decider once.

    When stream=True and an approval_decider is set, it spawns a background
    task that calls the decider (which registers a pending on the bridge and
    puts a TOOL_APPROVAL_REQUIRED event on the per-stream queue).  The stream
    yields MESSAGE_START, the approval event, MESSAGE_DONE, DONE.  The
    background task is cancelled in ``finally`` to simulate the real
    AgentGraphRunner cleanup chain.
    """

    def __init__(self):
        self.last_request = None

    async def complete(self, request):
        self.last_request = request
        if not request.stream:
            from app.application.chat_service import ChatCompletionResult
            return ChatCompletionResult(
                session_id=request.session_id or "",
                model=request.model,
                message={"role": "assistant", "content": ""},
            )
        return self._stream(request)

    async def _stream(self, request):
        queue = request.options.get("dashboard_approval_event_queue")
        decider = request.approval_decider
        task = None
        try:
            yield ChatEvent(ChatEventType.MESSAGE_START)
            if decider is not None and queue is not None:
                req = ApprovalRequest(
                    session_id=request.session_id,
                    tool_call_id="call-1",
                    tool_name="browser_click",
                    arguments={"selector": "#btn"},
                    description="Click an element",
                    risk_level=RiskLevel.CONFIRM,
                )
                task = asyncio.ensure_future(decider(req))
                event = await asyncio.wait_for(queue.get(), timeout=2.0)
                yield event
            yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
            yield ChatEvent(ChatEventType.DONE)
        finally:
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _build_approval_app(
    store,
    *,
    bridge=None,
    tool_approval_service=None,
    chat_service=None,
):
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = ModelService(_StubProvider(), "real-1")
    if chat_service is None:
        chat_service = _ApprovalChatService()
    if bridge is None:
        bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    if tool_approval_service is None:
        tool_approval_service = GatewayToolApprovalService()
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(
        create_dashboard_router(
            SessionService(store),
            tool_service,
            model_service,
            _default_health,
            memory_store=store,
            chat_service=chat_service,
            dashboard_tool_approval_bridge=bridge,
            tool_approval_service=tool_approval_service,
        )
    )
    return app


async def _create_pending(bridge, session_id, tool_name="browser_click"):
    """Register a pending approval on the bridge and return its confirmation_id.

    The decider task is left running (pending) so that ``claim`` can resolve it.
    Returns ``(confirmation_id, decider_task)``.
    """
    sender_event = asyncio.Event()
    captured = {}

    async def sender(metadata):
        captured["metadata"] = metadata
        sender_event.set()

    decider = bridge.create_decider(
        session_id=session_id,
        actor_id="dashboard",
        sender=sender,
    )
    req = ApprovalRequest(
        session_id=session_id,
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments={"selector": "#btn"},
        description="Click an element",
        risk_level=RiskLevel.CONFIRM,
    )
    task = asyncio.ensure_future(decider(req))
    await asyncio.wait_for(sender_event.wait(), timeout=2.0)
    return captured["metadata"]["confirmation_id"], task


# -- Stream validation (sync, TestClient) ------------------------------------

def test_dashboard_stream_false_returns_422_when_approval_enabled(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    client.post("/chat/sessions?session_id=s1")
    r = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "dashboard_stream_required"


def test_dashboard_stream_string_true_returns_422(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    client.post("/chat/sessions?session_id=s1")
    r = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "stream": "true", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "dashboard_stream_required"


def test_dashboard_stream_int_one_returns_422(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    client.post("/chat/sessions?session_id=s1")
    r = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "stream": 1, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "dashboard_stream_required"


def test_dashboard_stream_omitted_defaults_to_streaming_when_approval_enabled(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    client.post("/chat/sessions?session_id=s1")
    with client.stream(
        "POST",
        "/chat/completions",
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())
    assert "data: [DONE]" in text


# -- Claim endpoint (async, needs pending on same loop) -----------------------

async def test_dashboard_claim_returns_204_on_ok(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    grant_service = GatewayToolApprovalService()
    app = _build_approval_app(store, bridge=bridge, tool_approval_service=grant_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        confirmation_id, task = await _create_pending(bridge, "s1")
        r = await client.post(
            f"/chat/tool-approvals/{confirmation_id}",
            headers={"X-Session-ID": "s1"},
            json={"choice": "once"},
        )
        assert r.status_code == 204
        assert r.content == b""
        # The decider future is resolved by the claim
        decision = await asyncio.wait_for(task, timeout=2.0)
        assert decision.allowed


async def test_dashboard_claim_returns_404_on_unknown(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    app = _build_approval_app(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        r = await client.post(
            "/chat/tool-approvals/tool-confirm-nonexistent",
            headers={"X-Session-ID": "s1"},
            json={"choice": "once"},
        )
        assert r.status_code == 404
        body = r.json()
        assert body["error"]["code"] == "tool_approval_not_found"
        # No sensitive context leaks
        blob = _json.dumps(body).lower()
        for s in ("#btn", "browser_click", "dashboard", "call-1", "selector"):
            assert s.lower() not in blob, f"404 leaked '{s}'"


async def test_dashboard_claim_returns_404_on_cross_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    app = _build_approval_app(store, bridge=bridge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        await client.post("/chat/sessions?session_id=s2")
        confirmation_id, task = await _create_pending(bridge, "s1")
        # Claim from a different session -> 404 (must not leak ownership)
        r = await client.post(
            f"/chat/tool-approvals/{confirmation_id}",
            headers={"X-Session-ID": "s2"},
            json={"choice": "once"},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "tool_approval_not_found"
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_dashboard_claim_returns_409_on_duplicate(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    app = _build_approval_app(store, bridge=bridge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        confirmation_id, task = await _create_pending(bridge, "s1")
        # First claim -> 204
        r1 = await client.post(
            f"/chat/tool-approvals/{confirmation_id}",
            headers={"X-Session-ID": "s1"},
            json={"choice": "once"},
        )
        assert r1.status_code == 204
        # Decider resolves
        await asyncio.wait_for(task, timeout=2.0)
        # Second claim (same session, duplicate) -> 409
        r2 = await client.post(
            f"/chat/tool-approvals/{confirmation_id}",
            headers={"X-Session-ID": "s1"},
            json={"choice": "once"},
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "tool_approval_conflict"


# -- Claim endpoint 422 validation (sync, no pending needed) -----------------

def test_dashboard_claim_returns_422_on_bad_choice(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    r = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1"},
        json={"choice": "bad_value"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tool_approval_invalid"


def test_dashboard_claim_returns_422_on_extra_fields(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    r = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1"},
        json={"choice": "once", "session": "s1", "tool": "browser_click"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tool_approval_invalid"


def test_dashboard_claim_returns_422_on_missing_header(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    r = client.post(
        "/chat/tool-approvals/some-id",
        json={"choice": "once"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tool_approval_invalid"


def test_dashboard_claim_returns_422_on_non_json(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    r = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1", "Content-Type": "application/json"},
        content=b"not json",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tool_approval_invalid"


def test_dashboard_claim_returns_422_on_wrong_content_type(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_approval_app(store))
    r = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1", "Content-Type": "text/plain"},
        content="choice=once",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "tool_approval_invalid"


def test_dashboard_claim_errors_contain_no_sensitive_context(tmp_path):
    """No 422/404/409 response may leak tool args, session ID, actor, or confirmation ID."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    client = TestClient(_build_approval_app(store, bridge=bridge))
    sensitive = ("#btn", "browser_click", "dashboard", "call-1", "selector")
    # 404
    r404 = client.post(
        "/chat/tool-approvals/tool-confirm-leak-test",
        headers={"X-Session-ID": "s1"},
        json={"choice": "once"},
    )
    assert r404.status_code == 404
    blob404 = _json.dumps(r404.json()).lower()
    for s in sensitive:
        assert s.lower() not in blob404, f"404 leaked '{s}'"
    # 422
    r422 = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1"},
        json={"choice": "bad"},
    )
    assert r422.status_code == 422
    blob422 = _json.dumps(r422.json()).lower()
    for s in sensitive:
        assert s.lower() not in blob422, f"422 leaked '{s}'"


# -- Approval SSE envelope ---------------------------------------------------

async def test_dashboard_approval_sse_envelope(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    grant_service = GatewayToolApprovalService()
    app = _build_approval_app(store, bridge=bridge, tool_approval_service=grant_service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        async with client.stream(
            "POST",
            "/chat/completions",
            headers={"X-Session-ID": "s1"},
            json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = []
            async for line in response.aiter_lines():
                lines.append(line)

    text = "\n".join(lines)
    # Approval envelope present with the right object type
    assert "n-agent.tool_approval" in text
    # Find the approval data line
    approval_data = None
    chunk_count = 0
    done_count = 0
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            done_count += 1
            continue
        obj = _json.loads(payload)
        if obj.get("object") == "n-agent.tool_approval":
            approval_data = obj["approval"]
        else:
            assert obj["object"] == "chat.completion.chunk"
            chunk_count += 1

    assert approval_data is not None
    # Exactly the 5 fixed metadata fields
    assert set(approval_data.keys()) == {
        "confirmation_id",
        "tool_name",
        "description",
        "arguments_summary",
        "expires_at",
    }
    # All fields are JSON scalar strings
    for v in approval_data.values():
        assert isinstance(v, str)
    # Normal chat chunks still present (at least MESSAGE_START)
    assert chunk_count >= 1
    # [DONE] at most once
    assert done_count == 1


# -- Disconnect cleanup -----------------------------------------------------

async def test_dashboard_disconnect_cleans_up_bridge(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    bridge = DashboardToolApprovalBridge(timeout_seconds=30.0)
    grant_service = GatewayToolApprovalService()
    app = _build_approval_app(store, bridge=bridge, tool_approval_service=grant_service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/chat/sessions?session_id=s1")
        confirmation_id = None
        async with client.stream(
            "POST",
            "/chat/completions",
            headers={"X-Session-ID": "s1"},
            json={"model": "test-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: ") and "n-agent.tool_approval" in line:
                    payload = _json.loads(line[len("data: "):])
                    confirmation_id = payload["approval"]["confirmation_id"]
                    break  # stop reading -> triggers disconnect

        # Give cleanup a moment to propagate
        await asyncio.sleep(0.15)
        assert bridge.pending_count == 0
        # Subsequent claim cannot execute the tool
        if confirmation_id is not None:
            result = bridge.claim(confirmation_id, "s1", "once")
            assert result.status in ("not_found", "conflict")
            assert result.status != "ok"


# -- Fail-closed / no-bridge regression -------------------------------------

def test_dashboard_router_without_bridge_has_no_claim_endpoint(tmp_path):
    """When bridge/tool_approval_service are absent, the claim endpoint is not
    registered.  A request to its path returns 404 (not 422/204)."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    app = _build_app(store)  # default: no bridge, no tool_approval_service
    client = TestClient(app)
    r = client.post(
        "/chat/tool-approvals/some-id",
        headers={"X-Session-ID": "s1"},
        json={"choice": "once"},
    )
    assert r.status_code == 404


def test_dashboard_router_without_bridge_stream_false_works(tmp_path):
    """Without bridge, stream: false works normally (no 422 dashboard_stream_required)."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    client = TestClient(_build_app(store))
    client.post("/chat/sessions?session_id=s1")
    r = client.post(
        "/chat/completions",
        headers={"X-Session-ID": "s1"},
        json={"model": "test-model", "stream": False, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200


async def test_v1_chat_completions_still_fail_closed_no_approval(tmp_path):
    """/v1/chat/completions never receives approval events (no decider injected)."""
    from app.application.agent_graph import AgentGraphRunner
    from app.application.chat_service import ChatCompletionService
    from app.application.model_service import ModelService
    from app.application.session_service import SessionService
    from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
    from app.interfaces.http.openai_compatible import create_openai_compatible_router

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Create an api session
        await client.post("/v1/chat/completions", json={
            "model": "test-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        # The /v1 route has no approval decider -- if a CONFIRM tool were
        # called it would return approval_required (fail-closed).  We just
        # verify the route does not inject approval events into the stream.
        r = await client.post("/v1/chat/completions", json={
            "model": "test-model", "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200
        assert "n-agent.tool_approval" not in r.text
