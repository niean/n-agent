from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.gateway_service import GatewayService
from app.application.schedule_service import (
    ScheduleService,
    ScheduledTaskCreateInput,
)
from app.application.session_service import SessionService
from app.application.skill_service import SkillService, SkillToolExecutor, skill_tool_definitions
from app.application.tool_service import (
    ToolService,
    builtin_tool_definitions,
    schedule_tool_definitions,
)
from app.domain.gateway import GatewaySessionKey, InteractionMessage
from app.domain.platform import Platform
from app.domain.provider import LLMResult, ModelInfo
from app.domain.schedule import PromptSafetyResult
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.infrastructure.tools.schedule_management import ScheduleManagementToolExecutor


class _ScriptedProvider:
    def __init__(self):
        self.calls = 0
        self.captured_tools: list[list[dict]] = []
        self.captured_messages: list[list[dict]] = []

    async def list_models(self):
        return [ModelInfo("N-Agent", "N-Agent", "scripted")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.captured_tools.append(list(tools))
        self.captured_messages.append([dict(m) for m in messages])
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-skill",
                            "type": "function",
                            "function": {
                                "name": "skill_view",
                                "arguments": '{"name":"n-agent"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            args = {
                "action": "create",
                "name": "日报提醒",
                "prompt": "提醒我看日报",
                "cron_expression": "0 9 * * *",
            }
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-manage",
                            "type": "function",
                            "function": {
                                "name": "manage_schedule",
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(
            message={"role": "assistant", "content": "已为你创建每日 9 点的日报提醒"},
            finish_reason="stop",
        )


class _RecordingScheduleRegistry:
    def __init__(self):
        self.tasks: dict = {}
        self.create_calls: list = []

    async def create(self, task):
        self.create_calls.append(task)
        self.tasks[task.id] = task
        return task

    async def list(self):
        return list(self.tasks.values())

    async def get(self, task_id):
        return self.tasks.get(task_id)

    async def update(self, task):
        self.tasks[task.id] = task
        return task

    async def update_status(self, task_id, status, enabled):
        task = self.tasks[task_id]
        updated = type(task)(**{**task.__dict__, "status": status, "enabled": enabled})
        self.tasks[task_id] = updated
        return updated

    async def delete(self, task_id):
        return self.tasks.pop(task_id, None) is not None

    async def list_executions(self, task_id, limit):
        return []

    async def mark_session_missing(self, session_id):
        return 0


class _PassthroughCalculator:
    def validate(self, expression, timezone):
        return None

    def next_after(self, expression, base_time, timezone_value):
        return datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc)


class _AllowScanner:
    def scan(self, prompt):
        return PromptSafetyResult(True, "")


class _RecordingScheduleService(ScheduleService):
    def __init__(self, registry, session_service):
        super().__init__(
            registry,
            _PassthroughCalculator(),
            _AllowScanner(),
            session_service,
            run_now_handler=None,
        )
        self.create_inputs: list[ScheduledTaskCreateInput] = []

    async def create(self, request: ScheduledTaskCreateInput):
        self.create_inputs.append(request)
        return await super().create(request)


class _FakeGatewayRegistry:
    def __init__(self):
        self.active: dict = {}
        self.processed: set = set()

    async def get_active_session(self, key):
        sid = self.active.get(key.conversation_parts)
        if sid is None:
            return None
        from app.domain.gateway import GatewaySessionLink

        return GatewaySessionLink("c", sid, key.display_name)

    async def create_session_link(self, key, session_id):
        from app.domain.gateway import GatewaySessionLink

        self.active[key.conversation_parts] = session_id
        return GatewaySessionLink("c", session_id, key.display_name)

    async def set_active_session(self, key, session_id):
        from app.domain.gateway import GatewaySessionLink

        self.active[key.conversation_parts] = session_id
        return GatewaySessionLink("c", session_id, key.display_name)

    async def list_session_links(self, key):
        return []

    async def delete_session_link(self, session_id):
        return None

    async def mark_event_processed(self, source, event_id, message_id=""):
        marker = (source, event_id)
        if marker in self.processed:
            return False
        self.processed.add(marker)
        return True


class _FakeModelService:
    @property
    def default_model(self):
        return "N-Agent"

    async def list_models(self):
        return [ModelInfo("N-Agent", "N-Agent", "scripted")]


class _StubSkillService:
    async def render_view(self, name: str, session_id: str = ""):
        return {
            "success": True,
            "name": name,
            "content": "Cron Jobs chapter omitted in stub.",
            "description": "stub",
            "readiness": "available",
            "linked_files": {},
        }

    async def list_for_llm(self):
        return []

    async def render_linked_file(self, name, file_path):
        return {"success": False, "error": "not supported in stub"}


@pytest.mark.asyncio
async def test_feishu_natural_language_creates_scheduled_task(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    session_service = SessionService(store)

    schedule_registry = _RecordingScheduleRegistry()
    schedule_service = _RecordingScheduleService(schedule_registry, session_service)

    skill_executor = SkillToolExecutor(_StubSkillService())
    schedule_executor = ScheduleManagementToolExecutor(schedule_service)
    builtin_executor = build_builtin_tool_executor(tmp_path)

    routes = {
        "skills_list": skill_executor,
        "skill_view": skill_executor,
        "manage_schedule": schedule_executor,
        "schedule_query": schedule_executor,
        "calculator": builtin_executor,
        "get_current_time": builtin_executor,
        "list_directory": builtin_executor,
        "read_text_file": builtin_executor,
    }
    composite = CompositeToolExecutor(routes)

    tool_definitions = (
        builtin_tool_definitions()
        + skill_tool_definitions()
        + schedule_tool_definitions()
    )
    tool_service = ToolService(composite, tool_definitions)

    provider = _ScriptedProvider()
    runner = AgentGraphRunner(
        provider,
        tool_service,
        store,
        HeuristicSummarizer(),
        iteration_limit=5,
    )
    chat_service = ChatCompletionService(store, runner, session_service)

    gateway_registry = _FakeGatewayRegistry()
    gateway = GatewayService(
        gateway_registry,
        chat_service,
        session_service,
        tool_service,
        _FakeModelService(),
        lambda: {"provider": {"status": "ok"}},
        schedule_service=schedule_service,
    )

    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", thread_id="")
    event = InteractionMessage(
        id="evt-natural-1",
        session_key=feishu_key,
        text="每天早上 9 点提醒我看日报",
        metadata={
            "actor_id": "ou_x",
            "receive_id": "oc_a",
            "receive_id_type": "chat_id",
            "thread_id": "",
        },
    )

    response = await gateway.handle_message(event)

    assert provider.calls == 3
    assert "日报提醒" in response.messages[0].content or "9" in response.messages[0].content

    system_prompt = provider.captured_messages[0][0]
    assert system_prompt["role"] == "system"
    assert "skill_view" in system_prompt["content"]
    assert "manage_schedule" in system_prompt["content"]

    first_tool_names = {t["function"]["name"] for t in provider.captured_tools[0]}
    assert "manage_schedule" in first_tool_names
    assert "skill_view" in first_tool_names

    assert len(schedule_service.create_inputs) == 1
    captured = schedule_service.create_inputs[0]
    assert captured.prompt == "提醒我看日报"
    assert captured.cron_expression == "0 9 * * *"
    assert captured.delivery_target == "origin"
    assert captured.origin == {
        "platform": "feishu",
        "receive_id": "oc_a",
        "receive_id_type": "chat_id",
        "thread_id": "",
    }
    assert captured.session_id == response.session_id

    tool_calls = await store.list_tool_calls(response.session_id)
    names = [tc.tool_name for tc in tool_calls]
    assert "skill_view" in names
    assert "manage_schedule" in names
