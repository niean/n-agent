import json

import pytest

from app.application.context_service import ContextService
from app.application.external_memory_manager import ExternalMemoryManager
from app.application.external_memory_tool_executor import ExternalMemoryToolExecutor
from app.application.tool_service import ToolService
from app.domain.agent import AgentState
from app.domain.tool import RiskLevel, ToolCallRequest, ToolDefinition, ToolExecutionContext, ToolResultStatus
from app.infrastructure.memory.builtin_project import BuiltinProjectMemory
from app.infrastructure.memory.multi_project import MultiProjectMemory


class QueryMemory:
    name = "mem0"

    def is_available(self):
        return True

    def get_tool_schemas(self):
        return [{"name": "mem0_profile", "parameters": {"type": "object"}}]

    def handle_tool_call(self, *args, **kwargs):
        raise AssertionError("unselected provider must not execute")


@pytest.fixture
def memory_tools(tmp_path):
    base = tmp_path / "memory"
    for name in ("selected", "other"):
        (base / name).mkdir(parents=True)
        (base / name / "memory.md").write_text("original", encoding="utf-8")
    manager = ExternalMemoryManager()
    manager.add_provider(BuiltinProjectMemory(tmp_path, memory_path="memory"))
    files = MultiProjectMemory(tmp_path, memory_base_path="memory")
    files.initialize("")
    manager.add_provider(files)
    manager.add_provider(QueryMemory())
    tools = ToolService(object(), [ToolDefinition(
        name="ordinary", description="ordinary tool", input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
    )])
    tools.set_dynamic_definitions("external_memory", manager.get_tool_definitions())
    service = ContextService(object(), tool_service=tools, external_memory_manager=manager)
    return manager, service, base


@pytest.mark.parametrize(("selected", "expected"), [
    ([], set()),
    (["selected"], {"multi_external_memory"}),
    (["builtin"], {"external_memory"}),
    (["mem0"], {"mem0_profile"}),
    (["selected", "mem0"], {"multi_external_memory", "mem0_profile"}),
    (["builtin", "selected", "mem0"], {"external_memory", "multi_external_memory", "mem0_profile"}),
    (["missing"], set()),
    (None, {"external_memory", "mem0_profile"}),
])
def test_model_tools_follow_session_selection_without_mutating_global_surface(memory_tools, selected, expected):
    manager, service, _ = memory_tools
    manager.set_global_enabled(["builtin", "mem0"])
    state = AgentState(session_id="file-chat", run_options={"external_memory_enabled": selected})
    # The locked state, not unrelated model options, owns the memory profile.
    result = service.build_provider_context(state, {"external_memory_enabled": ["mem0"]})
    assert {t["function"]["name"] for t in result.tools} == expected | {"ordinary"}
    assert len(service.tool_service.list_definitions()) == 4


def test_sequential_sessions_do_not_leak_memory_tools(memory_tools):
    _, service, _ = memory_tools
    def names(selection):
        state = AgentState(session_id="chat", run_options={"external_memory_enabled": selection})
        return {t["function"]["name"] for t in service.build_provider_context(state, {}).tools}
    assert "mem0_profile" in names(["mem0"])
    assert names(["selected"]) == {"ordinary", "multi_external_memory"}
    assert names([]) == {"ordinary"}


def test_memory_selection_does_not_bypass_safe_only_policy(memory_tools):
    _, service, _ = memory_tools
    state = AgentState(session_id="background", run_options={"external_memory_enabled": ["builtin", "selected", "mem0"]})
    result = service.build_provider_context(state, {"tool_exposure_policy": "safe_only"})
    assert {t["function"]["name"] for t in result.tools} == {"ordinary"}


def test_unselected_query_tool_still_denied_at_execution(memory_tools):
    manager, _, _ = memory_tools
    result = manager.handle_tool_call("mem0_profile", {}, agent_context="primary",
                                      session_id="file-chat", enabled_override=["selected"])
    assert json.loads(result)["error"] == "provider not enabled"


def test_unreadable_file_catalog_hides_file_tools_without_breaking_model(memory_tools, monkeypatch):
    manager, service, _ = memory_tools
    def unreadable(_self):
        raise PermissionError("private filesystem path")
    monkeypatch.setattr(MultiProjectMemory, "list_projects", unreadable)
    state = AgentState(session_id="chat", run_options={"external_memory_enabled": ["selected", "mem0"]})
    result = service.build_provider_context(state, {})
    assert {t["function"]["name"] for t in result.tools} == {"ordinary", "mem0_profile"}
    denied = manager.handle_tool_call("multi_external_memory", {"project": "selected"},
                                      agent_context="primary", session_id="chat", enabled_override=["selected"])
    assert json.loads(denied)["error"] == "provider not enabled"


def test_selected_file_tool_executes_only_for_selected_project(memory_tools):
    manager, _, base = memory_tools
    def write(project):
        return json.loads(manager.handle_tool_call(
            "multi_external_memory", {"project": project, "action": "add", "content": "new fact"},
            agent_context="primary", session_id="file-chat", enabled_override=["selected"],
        ))
    assert write("selected")["success"] is True
    assert "new fact" in (base / "selected" / "memory.md").read_text()
    assert write("other")["success"] is False
    assert (base / "other" / "memory.md").read_text() == "original"


@pytest.mark.asyncio
async def test_executor_preserves_selected_file_profile(memory_tools):
    manager, _, _ = memory_tools
    executor = ExternalMemoryToolExecutor(manager)
    context = ToolExecutionContext(session_id="file-chat", enabled_override=["selected"],
                                   trusted_metadata={"agent_context": "primary"})
    result = await executor.execute(ToolCallRequest(
        id="file-write", name="multi_external_memory",
        arguments={"project": "selected", "action": "add", "content": "executor fact"},
    ), context)
    assert result.status is ToolResultStatus.SUCCESS
    denied = await executor.execute(ToolCallRequest(id="query", name="mem0_profile", arguments={}), context)
    assert denied.status is ToolResultStatus.ERROR
    assert denied.content["error"] == "provider not enabled"
