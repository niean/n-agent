"""T6: Application task tool definitions (Application Layer).

Returns 6 managed ToolDefinitions for the Task subdomain (Manus-aligned
7-state machine). Each tool:
  - source_type=AGENT (worker agent callable)
  - toolset="task"
  - managed=True (ToolPolicy only exposes when trusted_metadata.task context
    present -- pattern twelve gating; OpenAI HTTP clients cannot forge
    trusted_metadata)
  - risk_level=CONFIRM (managed tools require CONFIRM per ToolPolicy)
  - description Chinese (per spec)
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
                "读取当前 task 的完整上下文：标题、正文、待审批提案、审批决策、"
                "进度事件、评论、最近事件、运行历史与 worker_context。worker 启动后"
                "应先调用此工具了解任务全貌再开始工作。"
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
            name=TASK_TOOL_PROPOSE_CHANGE,
            description=(
                "提出需要用户审批的变更提案。当 worker 在执行中遇到需要用户决策"
                "的修改（如改变方案、确认破坏性操作、关键路径分歧）时调用此工具，"
                "附带 proposal 文本说明提案内容。调用后本 run 立即结束，task 进入"
                "WAITING_APPROVAL，等待用户通过 approve/reject 决定后续行：批准后"
                "按提案继续，拒绝后不得执行该提案。重复调用或 task 已在"
                "WAITING_APPROVAL 时返回 409 task_state_invalid。"
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
                "worker 判定当前 task 无法继续、确定性地快速失败（不再重试）时调用。"
                "典型场景：必需工具不可用、任务指令明确禁止兜底方案、遇到不可恢复的"
                "前置条件缺失。reason 为失败原因（人类可读）。调用后本 run 立即结束，"
                "task 进入 FAILED 终态（绕过断路器，不自动重试）。注意：本工具表达的是"
                "worker 主动失败，不是用户取消；用户取消请走 /task cancel 或取消按钮。"
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
                "把用户的自然语言目标委派为一个后台 Task，绑定当前会话；"
                "Task 进入队列后由任务引擎在当前会话内自主执行，生命周期状态以系统消息呈现。"
                "适用于多步执行、研究分析、文件/代码产出、长耗时或可后台自主完成的目标；"
                "不适用于单步问答、查事实、简单计算等可直接回答的请求。"
                "goal 为完整自然语言目标（写入 task body）；title 为短标题，可省略。"
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
                "列出当前会话关联、未归档的任务（origin_session_id 精确匹配）。"
                "用于回答用户关于本会话任务进度/状态的提问。可选 status 过滤。"
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
