"""T6: Application task tool definitions (ToolDefinition).

Covers plan T6 (task_tool_definitions 返回 6 个 managed ToolDefinition).

Spec reference (Manus-aligned 7-state machine):
  - 6 个工具: task_show / task_complete / task_heartbeat / task_comment /
    task_propose_change / task_cancel
  - source_type=AGENT, toolset="task", managed=True, risk_level=CONFIRM
  - managed=True 表示 ToolPolicy 仅在 trusted_metadata.task 上下文存在时
    才暴露（模式十二 trusted_metadata 门控）
  - description 中文
  - input_schema 按 spec 工具契约

移除的工具（对齐 Manus 扁平状态机）:
  - task_block / task_create / task_link
"""
from __future__ import annotations

import pytest

from app.application.task_tools import task_tool_definitions
from app.domain.tool import RiskLevel, ToolSourceType


# ---------------------------------------------------------------------------
# T6 S1-S4: 6 工具 ToolDefinition
# ---------------------------------------------------------------------------


def test_task_tool_definitions_returns_six_tools():
    defs = task_tool_definitions()
    names = {d.name for d in defs}
    assert names == {
        "task_show",
        "task_complete",
        "task_heartbeat",
        "task_comment",
        "task_propose_change",
        "task_cancel",
    }
    assert len(defs) == 6


def test_task_tool_definitions_no_removed_tools():
    """Removed tools (task_block/task_create/task_link) must not appear."""
    defs = task_tool_definitions()
    names = {d.name for d in defs}
    assert "task_block" not in names
    assert "task_create" not in names
    assert "task_link" not in names


def test_task_tool_definitions_common_attributes():
    """所有 6 个工具必须有统一的 source_type/toolset/managed/risk_level。"""
    defs = task_tool_definitions()
    for d in defs:
        assert d.source_type is ToolSourceType.AGENT, f"{d.name} source_type"
        assert d.toolset == "task", f"{d.name} toolset"
        assert d.managed is True, f"{d.name} managed"
        # managed 工具必须用 CONFIRM 风险级别（ToolPolicy.validate_definition
        # 要求 managed -> CONFIRM；与 schedule manage_schedule 一致）
        assert d.risk_level is RiskLevel.CONFIRM, f"{d.name} risk_level"
        assert d.enabled is True, f"{d.name} enabled"


def test_task_tool_definitions_pass_policy_validation():
    """6 个工具必须能通过 ToolPolicy.validate_definition（wiring 必需）。"""
    from app.domain.tool_policy import ToolPolicy
    policy = ToolPolicy()
    for d in task_tool_definitions():
        policy.validate_definition(d)


def test_task_tool_definitions_no_duplicate_names():
    defs = task_tool_definitions()
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "duplicate tool names"


def test_task_tool_definitions_descriptions_are_chinese():
    """description 必须为中文（spec 约束）。"""
    defs = task_tool_definitions()
    for d in defs:
        # 简单启发式：description 至少包含一个中文字符
        assert any("一" <= ch <= "鿿" for ch in d.description), (
            f"{d.name} description should contain Chinese characters"
        )
        assert len(d.description) > 0, f"{d.name} description empty"


def test_task_tool_definitions_input_schemas_are_valid_json_schema():
    """每个工具的 input_schema 必须是 type=object 的合法 JSON Schema。"""
    defs = task_tool_definitions()
    for d in defs:
        assert isinstance(d.input_schema, dict), f"{d.name} input_schema type"
        assert d.input_schema.get("type") == "object", f"{d.name} schema type"
        assert "properties" in d.input_schema, f"{d.name} properties"
        assert "additionalProperties" in d.input_schema, f"{d.name} additionalProperties"


def test_task_show_input_schema():
    """task_show: {task_id}"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_show"].input_schema
    assert "task_id" in schema["properties"]
    assert schema["properties"]["task_id"]["type"] == "string"
    assert "task_id" in schema.get("required", [])


def test_task_complete_input_schema():
    """task_complete: {summary, metadata, artifacts}"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_complete"].input_schema
    props = schema["properties"]
    assert "summary" in props
    assert "metadata" in props
    assert "artifacts" in props
    assert "summary" in schema.get("required", [])


def test_task_heartbeat_input_schema():
    """task_heartbeat: {note}"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_heartbeat"].input_schema
    props = schema["properties"]
    assert "note" in props
    assert props["note"]["type"] == "string"


def test_task_comment_input_schema():
    """task_comment: {task_id, body}"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_comment"].input_schema
    props = schema["properties"]
    assert "task_id" in props
    assert "body" in props
    assert "task_id" in schema.get("required", [])
    assert "body" in schema.get("required", [])


def test_task_propose_change_input_schema():
    """task_propose_change: {proposal} -- proposal 非空 text"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_propose_change"].input_schema
    props = schema["properties"]
    assert "proposal" in props
    assert props["proposal"]["type"] == "string"
    assert props["proposal"].get("minLength") == 1
    assert "proposal" in schema.get("required", [])
    assert schema.get("additionalProperties") is False


def test_task_cancel_input_schema():
    """task_cancel: 无参"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_cancel"].input_schema
    assert schema["properties"] == {}
    assert schema.get("required", []) == []
    assert schema.get("additionalProperties") is False
