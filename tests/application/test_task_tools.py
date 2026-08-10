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
        "task_fail",
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


def test_task_tool_definitions_descriptions_are_english():
    """description 必须为英文（spec 约束：任务相关描述从中文改为英文）。"""
    defs = task_tool_definitions()
    for d in defs:
        # 不含中文字符
        assert not any("一" <= ch <= "鿿" for ch in d.description), (
            f"{d.name} description should not contain Chinese characters"
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


def test_task_complete_description_guides_workspace_ref_via_write_file():
    """task_complete description must explain that a workspace: ref resolves
    to the workspace root (not the sandbox cwd) and the file must be written
    via write_file -- regression for the silent-drop bug where workers wrote
    via open() to scratch and the artifact was never registered."""
    defs = {d.name: d for d in task_tool_definitions()}
    desc = defs["task_complete"].description
    assert "workspace ROOT" in desc or "workspace root" in desc
    assert "write_file" in desc
    assert "open()" in desc


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
    """task_propose_change: {proposal, proposal_type?} -- proposal 非空 text;
    proposal_type 可选 enum [approval, intent_request]，默认 approval。"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_propose_change"].input_schema
    props = schema["properties"]
    assert "proposal" in props
    assert props["proposal"]["type"] == "string"
    assert props["proposal"].get("minLength") == 1
    assert "proposal" in schema.get("required", [])
    # proposal_type 可选（不在 required 中）
    assert "proposal_type" in props
    assert props["proposal_type"]["type"] == "string"
    assert props["proposal_type"]["enum"] == ["approval", "intent_request"]
    assert "proposal_type" not in schema.get("required", [])
    assert schema.get("additionalProperties") is False


def test_task_propose_change_description_mentions_proposal_type():
    """task_propose_change description 应说明 proposal_type 的两种语义。"""
    defs = {d.name: d for d in task_tool_definitions()}
    desc = defs["task_propose_change"].description
    assert "proposal_type" in desc
    assert "approval" in desc
    assert "intent_request" in desc


def test_task_fail_input_schema():
    """task_fail: {reason} 必填"""
    defs = {d.name: d for d in task_tool_definitions()}
    schema = defs["task_fail"].input_schema
    props = schema["properties"]
    assert "reason" in props
    assert props["reason"].get("minLength") == 1
    assert "reason" in schema.get("required", [])
    assert schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# 用户侧任务工具定义（自然语言委派入口 create_task / list_tasks）
# spec: spec-260720-chat-natural-language-task.md
# ---------------------------------------------------------------------------


def test_user_task_tool_names_constants_and_disjoint():
    from app.application.task_tools import (
        USER_TASK_TOOL_CREATE,
        USER_TASK_TOOL_LIST,
        USER_TASK_TOOL_NAMES,
    )

    assert USER_TASK_TOOL_CREATE == "create_task"
    assert USER_TASK_TOOL_LIST == "list_tasks"
    assert USER_TASK_TOOL_NAMES == frozenset({USER_TASK_TOOL_CREATE, USER_TASK_TOOL_LIST})
    # 与 worker managed 工具集不相交（防递归：worker 侧不暴露用户侧工具）
    worker_names = {d.name for d in task_tool_definitions()}
    assert USER_TASK_TOOL_NAMES.isdisjoint(worker_names)


def test_user_task_tool_definitions_returns_two():
    from app.application.task_tools import user_task_tool_definitions

    defs = user_task_tool_definitions()
    assert {d.name for d in defs} == {"create_task", "list_tasks"}
    assert len(defs) == 2


def test_user_task_tool_definitions_common_attributes():
    """两个用户侧工具：AGENT + SAFE + managed=False + toolset=task + enabled。"""
    from app.application.task_tools import user_task_tool_definitions

    for d in user_task_tool_definitions():
        assert d.source_type is ToolSourceType.AGENT, f"{d.name} source_type"
        assert d.toolset == "task", f"{d.name} toolset"
        assert d.managed is False, f"{d.name} managed"
        assert d.risk_level is RiskLevel.SAFE, f"{d.name} risk_level"
        assert d.enabled is True, f"{d.name} enabled"


def test_user_task_tool_definitions_pass_policy_validation():
    """managed=False + SAFE 能通过 ToolPolicy.validate_definition。"""
    from app.application.task_tools import user_task_tool_definitions
    from app.domain.tool_policy import ToolPolicy

    policy = ToolPolicy()
    for d in user_task_tool_definitions():
        policy.validate_definition(d)


def test_create_task_input_schema():
    from app.application.task_tools import user_task_tool_definitions

    schema = {d.name: d for d in user_task_tool_definitions()}["create_task"].input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["goal"]
    props = schema["properties"]
    assert props["goal"]["type"] == "string"
    assert props["goal"]["minLength"] == 1
    assert props["title"]["type"] == "string"
    assert props["priority"]["type"] == "integer"
    assert props["priority"]["minimum"] == 0
    assert props["goal_mode"]["type"] == "boolean"
    assert props["skills"]["type"] == "array"
    assert props["skills"]["items"]["type"] == "string"


def test_list_tasks_input_schema_status_enum_matches_taskstatus():
    """list_tasks 的 status 枚举与 TaskStatus value 集合一致（不硬编码状态机）。"""
    from app.application.task_tools import user_task_tool_definitions
    from app.domain.task import TaskStatus

    schema = {d.name: d for d in user_task_tool_definitions()}["list_tasks"].input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"status"}
    expected = {s.value for s in TaskStatus}
    assert set(schema["properties"]["status"]["enum"]) == expected


# ---------------------------------------------------------------------------
# 用户侧任务审批工具定义（approve_task / reject_task / revise_task）
# spec: Task 4 -- 用户侧审批工具定义与暴露策略
# ---------------------------------------------------------------------------


def test_user_task_approval_tool_names_constants_and_disjoint():
    """三个审批工具名常量与 USER_TASK_APPROVAL_TOOL_NAMES 集合，且与既有工具集不相交。"""
    from app.application.task_tools import (
        USER_TASK_TOOL_APPROVE,
        USER_TASK_TOOL_REJECT,
        USER_TASK_TOOL_REVISE,
        USER_TASK_APPROVAL_TOOL_NAMES,
        USER_TASK_TOOL_NAMES,
    )

    assert USER_TASK_TOOL_APPROVE == "approve_task"
    assert USER_TASK_TOOL_REJECT == "reject_task"
    assert USER_TASK_TOOL_REVISE == "revise_task"
    assert USER_TASK_APPROVAL_TOOL_NAMES == frozenset({
        USER_TASK_TOOL_APPROVE,
        USER_TASK_TOOL_REJECT,
        USER_TASK_TOOL_REVISE,
    })
    # 与 create/list 工具集不相交（保留 USER_TASK_TOOL_NAMES 既有语义）
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(USER_TASK_TOOL_NAMES)
    # 与 worker managed 工具集不相交（防递归）
    worker_names = {d.name for d in task_tool_definitions()}
    assert USER_TASK_APPROVAL_TOOL_NAMES.isdisjoint(worker_names)


def test_user_task_approval_tool_definitions_returns_three():
    from app.application.task_tools import user_task_approval_tool_definitions

    defs = user_task_approval_tool_definitions()
    assert {d.name for d in defs} == {"approve_task", "reject_task", "revise_task"}
    assert len(defs) == 3


def test_user_task_approval_tool_definitions_common_attributes():
    """三个审批工具：AGENT + SAFE + managed=False + toolset=task + enabled。"""
    from app.application.task_tools import user_task_approval_tool_definitions

    for d in user_task_approval_tool_definitions():
        assert d.source_type is ToolSourceType.AGENT, f"{d.name} source_type"
        assert d.toolset == "task", f"{d.name} toolset"
        assert d.managed is False, f"{d.name} managed"
        assert d.risk_level is RiskLevel.SAFE, f"{d.name} risk_level"
        assert d.enabled is True, f"{d.name} enabled"


def test_user_task_approval_tool_definitions_pass_policy_validation():
    """managed=False + SAFE 能通过 ToolPolicy.validate_definition。"""
    from app.application.task_tools import user_task_approval_tool_definitions
    from app.domain.tool_policy import ToolPolicy

    policy = ToolPolicy()
    for d in user_task_approval_tool_definitions():
        policy.validate_definition(d)


def test_user_task_approval_tool_definitions_no_duplicate_names():
    from app.application.task_tools import user_task_approval_tool_definitions

    defs = user_task_approval_tool_definitions()
    names = [d.name for d in defs]
    assert len(names) == len(set(names)), "duplicate tool names"


def test_user_task_approval_tool_definitions_descriptions_english_and_distinct():
    """description 英文，且 reject 与 revise 必须语义可区分。"""
    from app.application.task_tools import user_task_approval_tool_definitions

    defs = {d.name: d for d in user_task_approval_tool_definitions()}
    for d in defs.values():
        # 不含中文字符
        assert not any("一" <= ch <= "鿿" for ch in d.description), (
            f"{d.name} description should not contain Chinese characters"
        )
        assert len(d.description) > 0, f"{d.name} description empty"

    # reject 与 revise 必须有不同 description，且都含可区分的关键词
    reject_desc = defs["reject_task"].description.lower()
    revise_desc = defs["revise_task"].description.lower()
    assert defs["reject_task"].description != defs["revise_task"].description
    # reject 语义：拒绝提案、不再执行
    assert "reject" in reject_desc or "deny" in reject_desc or "decline" in reject_desc
    # revise 语义：给修改指示让 worker 调整后重试（区别于直接拒绝）
    assert "revise" in revise_desc or "revision" in revise_desc or "adjust" in revise_desc


def test_user_task_approval_tool_definitions_root_schema_shape():
    """三个工具根 schema: type=object + additionalProperties=false。"""
    from app.application.task_tools import user_task_approval_tool_definitions

    for d in user_task_approval_tool_definitions():
        schema = d.input_schema
        assert schema["type"] == "object", f"{d.name} schema type"
        assert schema["additionalProperties"] is False, f"{d.name} additionalProperties"
        assert "properties" in schema, f"{d.name} properties"


def test_approve_task_input_schema():
    """approve_task: {task_id(可选 minLength:1), note(可选 maxLength:2000)} required=[]"""
    from app.application.task_tools import user_task_approval_tool_definitions

    schema = {d.name: d for d in user_task_approval_tool_definitions()}["approve_task"].input_schema
    props = schema["properties"]
    assert set(props) == {"task_id", "note"}
    assert props["task_id"]["type"] == "string"
    assert props["task_id"]["minLength"] == 1
    assert props["note"]["type"] == "string"
    assert props["note"]["maxLength"] == 2000
    assert schema["required"] == []
    assert schema["additionalProperties"] is False


def test_reject_task_input_schema():
    """reject_task: {task_id(可选 minLength:1), note(可选 maxLength:2000)} required=[]"""
    from app.application.task_tools import user_task_approval_tool_definitions

    schema = {d.name: d for d in user_task_approval_tool_definitions()}["reject_task"].input_schema
    props = schema["properties"]
    assert set(props) == {"task_id", "note"}
    assert props["task_id"]["type"] == "string"
    assert props["task_id"]["minLength"] == 1
    assert props["note"]["type"] == "string"
    assert props["note"]["maxLength"] == 2000
    assert schema["required"] == []
    assert schema["additionalProperties"] is False


def test_revise_task_input_schema():
    """revise_task: {task_id(可选 minLength:1), note(必填 minLength:1 maxLength:2000)} required=["note"]"""
    from app.application.task_tools import user_task_approval_tool_definitions

    schema = {d.name: d for d in user_task_approval_tool_definitions()}["revise_task"].input_schema
    props = schema["properties"]
    assert set(props) == {"task_id", "note"}
    assert props["task_id"]["type"] == "string"
    assert props["task_id"]["minLength"] == 1
    assert props["note"]["type"] == "string"
    assert props["note"]["minLength"] == 1
    assert props["note"]["maxLength"] == 2000
    assert schema["required"] == ["note"]
    assert schema["additionalProperties"] is False
