"""T6: Application task tool definitions (Application Layer).

Returns 6 managed ToolDefinitions for the Task subdomain (Manus-aligned
7-state machine). Each tool:
  - source_type=AGENT (worker agent callable)
  - toolset="task"
  - managed=True (ToolPolicy only exposes when trusted_metadata.task context
    present -- pattern twelve gating; OpenAI HTTP clients cannot forge
    trusted_metadata)
  - risk_level=CONFIRM (managed tools require CONFIRM per ToolPolicy)
  - description English (per spec)
  - input_schema per spec tool contract

工具契约（spec, 6 工具）:
  - task_show:             {task_id} -- 读取 task + 上下文
  - task_complete:         {summary, metadata, artifacts} -- 提交完成意图
  - task_heartbeat:        {note} -- 续租 lease + 记录心跳
  - task_comment:          {task_id, body} -- 给指定 task 加评论
  - task_propose_change:   {proposal} -- 提出需用户审批的变更提案
  - task_fail:             {reason} -- worker 快速失败（不再重试）

移除的工具（对齐 Manus 扁平状态机）:
  - task_block:    阻塞意图已由 task_propose_change 的意图审批替代
  - task_create:   worker 不再自动拆分子任务，创建只通过用户入口
  - task_link:     依赖图已移除
  - task_cancel:   取消语义收回为用户专用（用户 /task cancel 或取消按钮），
                   worker 判定无法继续改用 task_fail（快速失败 -> FAILED 不重试），
                   不再混用用户取消语义
"""
from __future__ import annotations

from app.domain.task import TaskStatus
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType


# 工具名常量，供 TaskManagementToolExecutor 与测试引用
TASK_TOOL_SHOW = "task_show"
TASK_TOOL_COMPLETE = "task_complete"
TASK_TOOL_HEARTBEAT = "task_heartbeat"
TASK_TOOL_COMMENT = "task_comment"
TASK_TOOL_PROPOSE_CHANGE = "task_propose_change"
TASK_TOOL_FAIL = "task_fail"

# managed 工具集（6 工具），供 TaskAgentExecutor 写入 permitted_managed_tools
TASK_TOOL_NAMES: frozenset[str] = frozenset({
    TASK_TOOL_SHOW,
    TASK_TOOL_COMPLETE,
    TASK_TOOL_HEARTBEAT,
    TASK_TOOL_COMMENT,
    TASK_TOOL_PROPOSE_CHANGE,
    TASK_TOOL_FAIL,
})


def task_tool_definitions() -> list[ToolDefinition]:
    """返回 6 个 task managed ToolDefinition。

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
                "Read the full context of the current task: title, body, pending proposals, "
                "approval decisions, progress events, comments, recent events, run history, "
                "and worker_context. After startup, the worker should call this tool first to "
                "understand the task before starting work."
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
                "Submit the task completion intent. summary is a human-readable completion "
                "summary, metadata is structured results, and artifacts is the list of outputs. "
                "The tool only returns the terminal intent; TaskRunService finalizes the run in "
                "one shot via a CAS using the claim token."
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
            name=TASK_TOOL_HEARTBEAT,
            description=(
                "Record a task heartbeat and renew the claim lease. Call periodically during "
                "long tasks to avoid being deemed stale and reclaimed by the dispatcher. note "
                "is a remark for this heartbeat."
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
                "Append a comment to the specified task. The worker can only comment on the "
                "task it has claimed; cross-task comments are rejected by ownership checks."
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
            name=TASK_TOOL_PROPOSE_CHANGE,
            description=(
                "Raise a change proposal that requires user approval. Call this tool when the "
                "worker encounters a change requiring a user decision during execution (e.g., "
                "altering the plan, confirming a destructive operation, a key path divergence), "
                "with a proposal text describing the proposal. After the call, this run ends "
                "immediately, the task enters WAITING_APPROVAL, and the user decides the next "
                "step via approve/reject: if approved, proceed per the proposal; if rejected, "
                "do not execute the proposal. Repeated calls or calling when the task is already "
                "in WAITING_APPROVAL returns 409 task_state_invalid."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "proposal": {"type": "string", "minLength": 1},
                },
                "required": ["proposal"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
        ToolDefinition(
            name=TASK_TOOL_FAIL,
            description=(
                "Call when the worker determines the current task cannot continue and must "
                "fail fast deterministically (no retry). Typical scenarios: a required tool is "
                "unavailable, the task instructions explicitly forbid a fallback, or an "
                "unrecoverable precondition is missing. reason is the failure cause (human-"
                "readable). After the call, this run ends immediately and the task enters the "
                "FAILED terminal state (bypassing the circuit breaker, no auto-retry). Note: "
                "this tool expresses a worker-initiated failure, not a user cancellation; for "
                "user cancellation use /task cancel or the cancel button."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=True,
        ),
    ]


# ---------------------------------------------------------------------------
# 用户侧任务工具（自然语言委派入口）
# ---------------------------------------------------------------------------

USER_TASK_TOOL_CREATE = "create_task"
USER_TASK_TOOL_LIST = "list_tasks"
USER_TASK_TOOL_NAMES: frozenset[str] = frozenset({USER_TASK_TOOL_CREATE, USER_TASK_TOOL_LIST})


def user_task_tool_definitions() -> list[ToolDefinition]:
    """返回用户侧任务工具定义（create_task / list_tasks）。

    与 worker managed task 工具（task_tool_definitions）的关键差异：
      - source_type=AGENT + risk_level=SAFE + managed=false
      - realtime（DEFAULT 暴露）对对话 Agent 可见；unattended（SAFE_ONLY）
        默认隐藏 AGENT 源工具，故 worker/judge 不可见，防递归建子任务。
      - 绝不加入任何 worker/judge 的 granted_tools（见 spec Constraints）。

    create_task 由对话 Agent 在判断用户目标适合委派时调用，把自然语言目标
    委派为后台 Task（绑定当前会话），由既有 TaskRunner/worker 在同会话执行。
    list_tasks 列出当前会话关联任务，供 Agent 回答任务进度类提问。
    """
    return [
        ToolDefinition(
            name=USER_TASK_TOOL_CREATE,
            description=(
                "Delegate the user's natural-language goal as a background Task bound to the "
                "current session; after queuing, the task engine executes it autonomously within "
                "the current session, and lifecycle states are surfaced as system messages. "
                "Suitable for multi-step execution, research and analysis, file/code output, "
                "long-running, or goals that can be completed autonomously in the background; "
                "not suitable for single-step questions, fact lookups, or simple calculations "
                "that can be answered directly. goal is the full natural-language goal (written "
                "to the task body); title is a short title and may be omitted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "minLength": 1},
                    "title": {"type": "string"},
                    "priority": {"type": "integer", "minimum": 0},
                    "goal_mode": {"type": "boolean"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=False,
        ),
        ToolDefinition(
            name=USER_TASK_TOOL_LIST,
            description=(
                "List the current session's associated, non-archived tasks (exact match on "
                "origin_session_id). Used to answer user questions about this session's task "
                "progress/status. Optional status filter."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": [s.value for s in TaskStatus],
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="task",
            managed=False,
        ),
    ]
