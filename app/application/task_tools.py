"""T10: Application task tool definitions (Application Layer).

Returns 7 managed ToolDefinitions for the Task subdomain. Each tool:
  - source_type=AGENT (worker agent callable)
  - toolset="task"
  - managed=True (ToolPolicy only exposes when trusted_metadata.task context
    present -- pattern twelve gating; OpenAI HTTP clients cannot forge
    trusted_metadata)
  - risk_level=SAFE (all task tools are safe to execute within a trusted task
    context; gating is by managed+trusted_metadata, not by risk level)
  - description Chinese (per spec)
  - input_schema per spec tool contract

工具契约（spec）:
  - task_show:        {task_id} -- 读取 task + 上下文
  - task_complete:    {summary, metadata, artifacts} -- 提交完成意图
  - task_block:       {reason, kind} -- 提交阻塞意图（kind 对应 BlockKind）
  - task_heartbeat:   {note} -- 续租 lease + 记录心跳
  - task_comment:     {task_id, body} -- 给指定 task 加评论
  - task_create:      {title, body, assignee, parents, skills} -- 创建子任务
  - task_link:        {parent_id, child_id} -- 链接依赖
"""
from __future__ import annotations

from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType


# 工具名常量，供 TaskManagementToolExecutor 与测试引用
TASK_TOOL_SHOW = "task_show"
TASK_TOOL_COMPLETE = "task_complete"
TASK_TOOL_BLOCK = "task_block"
TASK_TOOL_HEARTBEAT = "task_heartbeat"
TASK_TOOL_COMMENT = "task_comment"
TASK_TOOL_CREATE = "task_create"
TASK_TOOL_LINK = "task_link"

# managed 工具集，供 TaskAgentExecutor 写入 permitted_managed_tools
TASK_TOOL_NAMES: frozenset[str] = frozenset({
    TASK_TOOL_SHOW,
    TASK_TOOL_COMPLETE,
    TASK_TOOL_BLOCK,
    TASK_TOOL_HEARTBEAT,
    TASK_TOOL_COMMENT,
    TASK_TOOL_CREATE,
    TASK_TOOL_LINK,
})


def task_tool_definitions() -> list[ToolDefinition]:
    """返回 7 个 task managed ToolDefinition。

    工具集与 source_type/toolset/managed/risk_level 在所有工具上一致；
    差异在 description 与 input_schema。managed=True 让 ToolPolicy 走
    trusted_metadata.task 门控（模式十二）：普通 chat 看不到这组工具，
    只有 TaskAgentExecutor 在 worker 执行时注入 permitted_managed_tools
    与 trusted_metadata.task 才会暴露和执行。
    """
    return [
        ToolDefinition(
            name=TASK_TOOL_SHOW,
            description=(
                "读取当前 task 的完整上下文：标题、正文、父任务交接、先前尝试摘要、"
                "评论、最近事件、运行历史与 worker_context。worker 启动后应先调用"
                "此工具了解任务全貌再开始工作。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_COMPLETE,
            description=(
                "提交 task 完成意图。summary 为人类可读的完成摘要，metadata 为"
                "结构化结果，artifacts 为产出物清单。工具只返回终态意图，最终"
                "由 TaskRunService 以 claim token 一次性 CAS 终结 run。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "metadata": {"type": "object"},
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "name": {"type": "string"},
                                "mime": {"type": "string"},
                                "size": {"type": "integer", "minimum": 0},
                                "storage_ref": {"type": "string"},
                                "summary": {"type": "string"},
                                "checksum": {"type": "string"},
                            },
                            "required": ["type", "name", "storage_ref"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_BLOCK,
            description=(
                "提交 task 阻塞意图。reason 为阻塞原因，kind 决定路由："
                "dependency 回到 TODO 等待父任务；needs_input/capability/transient"
                "进入 BLOCKED。工具只返回终态意图，最终由 TaskRunService 终结。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "minLength": 1},
                    "kind": {
                        "type": "string",
                        "enum": ["dependency", "needs_input", "capability", "transient"],
                    },
                },
                "required": ["reason", "kind"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_HEARTBEAT,
            description=(
                "记录 task 心跳并续租 claim lease。长任务执行中应周期调用，"
                "避免被 dispatcher 判定 stale 而 reclaim。note 为本次心跳备注。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "note": {"type": "string"},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_COMMENT,
            description=(
                "给指定 task 追加评论。worker 只能评论自己 claim 的 task；"
                "跨 task 评论会被 ownership 校验拒绝。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "minLength": 1},
                },
                "required": ["task_id", "body"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_CREATE,
            description=(
                "创建当前 task 的直接子任务。title 必填，body/assignee/parents/skills"
                "可选。parents 不填时默认以当前 task 为 parent。创建后子任务默认 TODO，"
                "由依赖重算推进到 READY。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "body": {"type": "string"},
                    "assignee": {"type": "string"},
                    "parents": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_LINK,
            description=(
                "建立 parent -> child 依赖边。parent 或 child 必须是当前 task，"
                "且仍走同 board/DAG 校验（拒绝自环、重复边、跨 board、环）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "parent_id": {"type": "string", "minLength": 1},
                    "child_id": {"type": "string", "minLength": 1},
                },
                "required": ["parent_id", "child_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
    ]
