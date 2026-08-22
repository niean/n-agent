"""T6: TaskAgentExecutor -- in-process worker that reuses AgentRunner.

Calls ``ChatCompletionService.complete(stream=False)`` with trusted task
context, aligned with ``ScheduledAgentExecutor``'s calling boundary:

  - ``IngressFacts`` with ``source=task``, ``ExecutionMode.UNATTENDED``
  - Same ``run_id`` in both ``IngressFacts`` and ``trusted_metadata``
  - task 6 tools in ``permitted_managed_tools`` (NOT ``granted_tools``):
    task_show / task_complete / task_heartbeat / task_comment /
    task_propose_change / task_fail
  - ``trusted_metadata.task`` carries the server-side immutable claim context
  - ``trusted_claims`` mirrors the same claim identifiers

The executor returns ``TaskAgentResult`` (terminal INTENT only). It does NOT
call ``finish_run`` -- ``TaskRunService`` owns the single CAS-based
finalization path.

Terminal intent detection (Manus-aligned):
  After ``chat_service.complete()`` returns, the executor reads the latest
  ``complete_requested`` / ``change_proposed`` / ``fail_requested`` event for the task.
  - ``complete_requested`` (written by ``TaskService.complete``) maps to
    ``TaskRunOutcome.COMPLETED``.
  - ``change_proposed`` (written by ``TaskService.propose_change`` when the
    worker calls ``task_propose_change``) maps to
    ``TaskRunOutcome.WAITING_APPROVAL``. The actual run finalization
    (claim release + worker reclaim) is performed by
    ``TaskRunService.finalize_propose``, called by ``TaskService.propose_change``
    inside the tool execution; the executor's returned intent lets
    ``TaskRunService._finalize_run`` handle the CAS conflict gracefully
    (late-worker path).
  - ``fail_requested`` (written by ``TaskService.fail`` when the worker calls
    ``task_fail``) maps to ``TaskRunOutcome.ABORTED`` -> task FAILED（绕过断路器，
    不重试）。worker 判定无法继续、确定性快速失败时使用；取消（CANCELLED）只认用户
    指令，worker 不得触发取消语义。
  If no intent event is found, the executor defaults to COMPLETED with the
  final output as summary.

goal_mode:
  ``run_goal_loop`` runs multiple turns, each calling ``run()`` once. A
  judge fork (read-only, no write tools) evaluates ``{achieved, reason}``.
  ``goal_max_turns`` budget caps the loop. Not achieved at max turns ->
  FAILED (the worker should use ``task_propose_change`` for needs-input
  scenarios; BLOCKED outcome is removed). Judge parse failure -> retryable
  FAILED.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.delegation_parent_adapter import TaskDelegationAdapter
from app.application.policy_snapshot import IngressFacts
from app.application.task_session import task_execution_session_id
from app.application.task_tools import (
    TASK_TOOL_NAMES,
    TASK_TOOL_SHOW,
    USER_TASK_APPROVAL_TOOL_NAMES,
)
from app.domain.policy import ExecutionMode
from app.domain.task import Task, TaskRunOutcome
from app.domain.task_config import TaskConfig, TaskConfigProvider

logger = logging.getLogger(__name__)


# goal_mode 连续被判"目标未达成"的早退阈值。对齐 Hermes Ralph loop 的 turn-budget
# 兜底思想，但在 N-Agent 后台任务场景下额外加早退：worker 连续 N 次完成但 judge 均判
# 未达成（且已通过 goal_judge_feedback 事件把 reason 喂回 worker 仍无改进）即视为
# "明确失败"，直接 FAILED，避免耗尽 goal_max_turns（修复 t_97d317e953b64edc 耗尽 10 轮）。
GOAL_MAX_CONSECUTIVE_REJECTIONS = 2

# Task worker 默认工具授予。execute_code 让 worker 能跑沙箱 Python、经 write_file
# 回调写 workspace 文件（回调写 workspace_root 经父进程 RPC，绕过 /workspace:ro），
# 对齐 TASK_GUIDANCE“用通用工具在 workspace 做事”，镜像 Hermes cron-default-core-tools。
# write_file 仍为沙箱内部回调（不提升为直接 LLM 工具）。sandbox 未启用时 execute_code
# 定义未注册，此 grant 为 no-op。judge 上下文（下方 run_judge）保持无工具，不受影响。
TASK_WORKER_DEFAULT_TOOLS: tuple[str, ...] = ("execute_code", "delegate_agents")


# ---------------------------------------------------------------------------
# TaskAgentResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskAgentResult:
    """Terminal intent returned by the executor.

    ``status`` is the terminal outcome intent (COMPLETED / WAITING_APPROVAL /
    FAILED / TIMED_OUT / CRASHED / ABORTED). ``output`` is the agent's final text.
    ``metadata`` and ``artifacts`` come from the task_complete tool call.
    ``error`` is set for FAILED/TIMED_OUT.

    The executor does NOT finalize the run; TaskRunService reads this result
    and performs the CAS finalize. For WAITING_APPROVAL, TaskService.propose_change
    has already called TaskRunService.finalize_propose inside the tool
    execution; the returned intent lets _finalize_run handle the CAS conflict
    gracefully (late-worker path).
    """

    status: TaskRunOutcome
    output: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Judge result (goal_mode)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeResult:
    """Parsed judge fork result."""

    achieved: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# TaskAgentExecutor
# ---------------------------------------------------------------------------


class TaskAgentExecutor:
    """In-process task worker that reuses ChatCompletionService.

    Injection:
      - ``chat_service``: ChatCompletionService (Application)
      - ``task_registry``: TaskRegistry (Domain port) -- for reading intent events
      - ``prompt_builder``: callable or object with build_system_prompt
      - ``max_runtime_seconds``: default hard timeout if task has none
      - ``goal_max_turns``: default turn budget for goal_mode
    """

    def __init__(
        self,
        chat_service: Any,
        task_registry: Any,
        prompt_builder: Any | None = None,
        max_runtime_seconds: int = 3600,
        goal_max_turns: int = 10,
        task_config_provider: TaskConfigProvider | None = None,
        task_delegation_adapter: Any | None = None,
        delegation_config: Any | None = None,
    ):
        self.chat_service = chat_service
        self.task_registry = task_registry
        self.prompt_builder = prompt_builder
        self.max_runtime_seconds = max_runtime_seconds
        self.goal_max_turns = goal_max_turns
        self._task_config_provider = task_config_provider
        self._task_delegation_adapter = task_delegation_adapter
        self._delegation_config = delegation_config

    async def _snapshot(self) -> TaskConfig:
        if self._task_config_provider is not None:
            return await self._task_config_provider.current()
        return TaskConfig(
            task_max_runtime_seconds=self.max_runtime_seconds,
            task_goal_max_turns=self.goal_max_turns,
        )

    # ------------------------------------------------------------------
    # Single-turn run
    # ------------------------------------------------------------------

    async def run(
        self,
        task: Task,
        task_run_id: int,
        claim_lock: str,
    ) -> TaskAgentResult:
        """Execute a single task turn via ChatCompletionService.

        Constructs the system prompt (build_system_prompt，含固定 TASK_GUIDANCE block),
        user prompt ``work task {task.id}``, and calls chat_service.complete
        with trusted task context. After completion, reads the latest intent
        event to determine the terminal outcome.
        """
        cfg = await self._snapshot()
        execution_session_id = task_execution_session_id(task)
        execution_run_id = f"task-run-{uuid4().hex[:12]}"

        # system prompt 由 ContextService 运行时构建（build_system_prompt，含固定 Task Guidance），
        # 不在 request 中传 system 消息，避免 working_messages 出现重复 system + 重复 user
        # （request 的 user 经 ChatCompletionService 持久化进 history，又经 input 重复；
        # 且 request 的 system 打头会破坏 _dedupe_trailing 对 user 的去重）。

        # Build granted tools and permitted managed tools
        # 防递归（Task 7）：即便 task.execution_policy.allowed_tools 被误配置为
        # approve_task / reject_task / revise_task，worker boundary 也必须显式剥离
        # 这三个用户侧审批工具名 -- worker 不能审批自己的 task_propose_change 提案。
        # 不改 ToolPolicy 通用 grant 可暴露 SAFE AGENT 工具的设计（模式六保留），
        # 只在 worker boundary 收紧。
        # 默认前置 TASK_WORKER_DEFAULT_TOOLS（execute_code），让 worker 有通用工具可用；
        # 显式 allowed_tools 叠加其上；审批工具仍剥离防递归。
        granted_tools = [
            name
            for name in (*TASK_WORKER_DEFAULT_TOOLS, *task.execution_policy.allowed_tools)
            if name not in USER_TASK_APPROVAL_TOOL_NAMES
        ]
        permitted_managed = set(TASK_TOOL_NAMES)

        # Delegation grant (T12): delegate_agents is in the server-owned
        # default grant for ordinary Task creation, then remains visible only
        # when both feature gates allow it. Task creation surfaces do not
        # accept arbitrary allowed_tools, so using the empty per-Task policy
        # as the Task-level gate made the enabled feature unreachable.
        # Children/aggregators always have it stripped (ChildAgentExecutor).
        if self._task_delegation_adapter is not None:
            delegate_allowed = TaskDelegationAdapter.should_grant(
                global_enabled=getattr(self._delegation_config, "enabled", False),
                task_policy_allows=getattr(
                    self._delegation_config, "task_enabled", False
                ),
                delegate_in_grants=("delegate_agents" in granted_tools),
            )
            granted_tools = TaskDelegationAdapter.grant_delegate_tool(
                granted_tools, allow=delegate_allowed
            )

        # Build trusted task context (server-side immutable)
        task_context: dict[str, Any] = {
            "task_id": task.id,
            "run_id": task_run_id,
            "claim_lock": claim_lock,
            "write_origin": "worker",
            "board": task.board,
            "execution_session_id": execution_session_id,
        }

        # Build trusted_claims (mirrors trusted_metadata). The ``task`` sub-dict
        # must travel here too: ChatCompletionService rebuilds trusted_metadata
        # from the policy snapshot's trusted_claims (sourced from
        # IngressFacts.trusted_claims), so a task context only in
        # trusted_metadata would be dropped and the task tools would fail with
        # trusted_task_context_missing.
        trusted_claims: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": granted_tools,
            "permitted_managed_tools": list(permitted_managed),
            "task": task_context,
            "task_id": task.id,
            "task_run_id": task_run_id,
            "execution_run_id": execution_run_id,
            "claim_lock": claim_lock,
            "write_origin": "worker",
        }

        # Build trusted_metadata (same claim identifiers as trusted_claims)
        trusted_metadata: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": granted_tools,
            "permitted_managed_tools": list(permitted_managed),
            "task": task_context,
            **trusted_claims,
        }

        # Sign a task delegation capability when delegate_agents was granted.
        # The capability is bound to the trusted task scope (task id), never
        # to a client-submitted identifier.
        if (
            self._task_delegation_adapter is not None
            and "delegate_agents" in granted_tools
        ):
            cap = self._task_delegation_adapter.sign_task_capability(
                run_id=execution_run_id,
                session_id=execution_session_id,
                scope_id=task.id,
                actor_id=task.created_by or None,
                parent_allowed_tools=frozenset(granted_tools),
                system_child_allowlist=frozenset(granted_tools),
            )
            trusted_metadata["delegation_capability"] = cap.to_dict()

        messages = [
            {"role": "user", "content": f"work task {task.id}"},
        ]

        request = ChatCompletionInput(
            model=task.model_override or "N-Agent",
            messages=messages,
            stream=False,
            session_id=execution_session_id,
            ingress_facts=IngressFacts(
                run_id=execution_run_id,
                session_id=execution_session_id,
                source="task",
                actor_id=task.created_by or None,
                execution_mode=ExecutionMode.UNATTENDED,
                trusted_claims=trusted_claims,
            ),
            trusted_metadata=trusted_metadata,
            options={"max_iterations": 20},
        )

        try:
            result = await self.chat_service.complete(request)
        except asyncio.TimeoutError:
            return TaskAgentResult(
                status=TaskRunOutcome.TIMED_OUT,
                error=f"task execution timed out after {task.max_runtime_seconds or cfg.task_max_runtime_seconds}s",
            )
        except Exception as exc:
            logger.exception("task agent executor failed: task=%s", task.id)
            return TaskAgentResult(
                status=TaskRunOutcome.FAILED,
                error=f"executor error: {exc}",
            )

        return await self._build_result_from_chat(task, task_run_id, result)

    # ------------------------------------------------------------------
    # goal_mode loop
    # ------------------------------------------------------------------

    async def run_goal_loop(
        self,
        task: Task,
        task_run_id: int,
        claim_lock: str,
    ) -> TaskAgentResult:
        """Multi-turn goal_mode loop.

        Each turn calls ``run()`` once, then a judge fork evaluates whether
        the goal is achieved. The loop continues until:
          - Judge says achieved -> COMPLETED
          - goal_max_turns reached -> FAILED (the worker should use
            task_propose_change for needs-input scenarios; BLOCKED outcome
            is removed from the Manus-aligned state machine)
          - Judge parse failure -> FAILED (retryable)
          - Total runtime exceeded -> TIMED_OUT
          - Worker proposed a change -> WAITING_APPROVAL (returned immediately)
        """
        cfg = await self._snapshot()
        max_turns = task.goal_max_turns or cfg.task_goal_max_turns
        max_runtime = task.max_runtime_seconds or cfg.task_max_runtime_seconds
        start = asyncio.get_event_loop().time()
        last_result: TaskAgentResult | None = None
        consecutive_rejections = 0

        for turn in range(1, max_turns + 1):
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > max_runtime:
                return TaskAgentResult(
                    status=TaskRunOutcome.TIMED_OUT,
                    error=f"goal_mode total runtime exceeded {max_runtime}s",
                )

            # Run one turn
            turn_result = await self.run(task, task_run_id, claim_lock)
            last_result = turn_result

            # If the turn itself already terminal (WAITING_APPROVAL/ABORTED/
            # FAILED/TIMED_OUT/CRASHED), return immediately. WAITING_APPROVAL means
            # the worker called task_propose_change; the run is already being
            # finalized by TaskRunService.finalize_propose.
            if turn_result.status in (
                TaskRunOutcome.WAITING_APPROVAL,
                TaskRunOutcome.ABORTED,
                TaskRunOutcome.FAILED,
                TaskRunOutcome.TIMED_OUT,
                TaskRunOutcome.CRASHED,
            ):
                return turn_result

            # If the turn completed, run the judge
            if turn_result.status == TaskRunOutcome.COMPLETED:
                judge = await self._run_judge(task, task_run_id, claim_lock)
                if judge is None:
                    # Judge parse failure -> retryable FAILED
                    return TaskAgentResult(
                        status=TaskRunOutcome.FAILED,
                        output=turn_result.output,
                        error="judge returned unparseable result",
                    )
                if judge.achieved:
                    return TaskAgentResult(
                        status=TaskRunOutcome.COMPLETED,
                        output=turn_result.output,
                        metadata={**turn_result.metadata, "judge_reason": judge.reason},
                        artifacts=turn_result.artifacts,
                    )
                # Not achieved: 把 judge reason 喂回下一轮 worker（goal_judge_feedback
                # 事件，经 build_worker_context 进度段透传，对齐 Hermes Ralph loop 的
                # continuation prompt），并累计连续否决次数。超过阈值即"明确失败"早退
                # FAILED，避免耗尽 max_turns（修复 t_97d317e953b64edc）。
                consecutive_rejections += 1
                await self._record_judge_feedback(task, task_run_id, turn, judge.reason)
                if consecutive_rejections >= GOAL_MAX_CONSECUTIVE_REJECTIONS:
                    return TaskAgentResult(
                        status=TaskRunOutcome.FAILED,
                        output=turn_result.output,
                        error=(
                            f"goal not achieved after {consecutive_rejections} consecutive "
                            f"judge rejections: {judge.reason}"
                        ),
                        metadata={"judge_reason": judge.reason},
                    )
                # 未达阈值但已是最后一轮 -> max_turns 兜底 FAILED
                if turn >= max_turns:
                    return TaskAgentResult(
                        status=TaskRunOutcome.FAILED,
                        output=turn_result.output,
                        error=f"goal not achieved after {max_turns} turns: {judge.reason}",
                        metadata={"judge_reason": judge.reason},
                    )

        # Should not reach here, but defensive
        return last_result or TaskAgentResult(
            status=TaskRunOutcome.FAILED,
            error="goal_loop exited without result",
        )

    async def _record_judge_feedback(
        self, task: Task, task_run_id: int, turn: int, reason: str,
    ) -> None:
        """把 judge 的 not-achieved reason 持久化为 goal_judge_feedback 事件。

        build_worker_context 进度段透传给下一轮 worker（task_show 读取），使 worker
        能基于反馈调整方法或主动 task_propose_change/task_cancel，而非重复同一失败做法
        （对齐 Hermes Ralph loop 的 continuation prompt）。best-effort，失败仅 log。
        """
        try:
            await self.task_registry.append_event(
                task.id, "goal_judge_feedback",
                {"turn": turn, "achieved": False, "reason": reason},
                run_id=task_run_id,
            )
        except Exception:
            logger.warning(
                "record judge feedback failed for task %s", task.id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Judge fork
    # ------------------------------------------------------------------

    async def _run_judge(
        self, task: Task, task_run_id: int, claim_lock: str,
    ) -> JudgeResult | None:
        """Run a judge fork to evaluate goal achievement.

        Returns None on parse failure (retryable FAILED). The judge is read-only:
        only task_show is permitted（读任务上下文），无写工具、无 search_knowledge。
        system 消息复用 build_system_prompt（含固定 ## Goal Mode Judge 章节），与 worker
        共享 system prompt 前缀（cache stable），不再单独注入 _JUDGE_PROMPT。
        judge 复用 worker 的 run_id/claim_lock 注入 trusted_metadata.task，使 task_show
        通过 _origin_from_trusted（Gate 2）；write_origin="judge" 标识只读 fork。
        """
        execution_session_id = task_execution_session_id(task)
        judge_run_id = f"task-judge-{uuid4().hex[:12]}"

        task_context: dict[str, Any] = {
            "task_id": task.id,
            "run_id": task_run_id,
            "claim_lock": claim_lock,
            "write_origin": "judge",
            "board": task.board,
            "execution_session_id": execution_session_id,
        }

        trusted_claims: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": [],  # 无写工具
            "permitted_managed_tools": [TASK_TOOL_SHOW],  # 只读 task_show
            "task": task_context,
            "task_id": task.id,
            "judge": True,
        }

        request = ChatCompletionInput(
            model=task.model_override or "N-Agent",
            messages=[
                {"role": "user", "content": f"judge task {task.id}: has the goal been achieved?"},
            ],
            stream=False,
            session_id=execution_session_id,
            ingress_facts=IngressFacts(
                run_id=judge_run_id,
                session_id=execution_session_id,
                source="task",
                actor_id=None,
                execution_mode=ExecutionMode.UNATTENDED,
                trusted_claims=trusted_claims,
            ),
            trusted_metadata={
                "execution_mode": ExecutionMode.UNATTENDED.value,
                "granted_tools": [],
                "permitted_managed_tools": [TASK_TOOL_SHOW],
                **trusted_claims,
            },
            # judge reuses execution_session_id but must NOT pollute the user-visible
            # Chat history. Its user prompt ("judge task {id}: ..."), assistant JSON
            # ({"achieved":..., "reason":...}) and any task_show tool_call/result are
            # control-flow signals, not user-facing results. Suppress all message +
            # tool_call persistence for this fork. Tools still execute (task_show can
            # still read the task) and LLM context is unaffected.
            persist_messages=False,
        )

        try:
            result = await self.chat_service.complete(request)
        except Exception as exc:
            logger.warning("judge fork failed: task=%s err=%s", task.id, exc)
            return None

        return self._parse_judge_result(result)

    def _parse_judge_result(self, result: Any) -> JudgeResult | None:
        """Parse the judge's JSON response. Returns None on failure."""
        if not isinstance(result, ChatCompletionResult):
            return None
        content = result.message.get("content", "")
        if not isinstance(content, str):
            return None
        # Try to extract JSON from the response
        try:
            # The judge might wrap JSON in markdown or add extra text
            json_str = content.strip()
            if "```" in json_str:
                # Extract from code block
                start = json_str.find("{")
                end = json_str.rfind("}") + 1
                if start >= 0 and end > start:
                    json_str = json_str[start:end]
            parsed = json.loads(json_str)
            achieved = bool(parsed.get("achieved", False))
            reason = str(parsed.get("reason", ""))
            return JudgeResult(achieved=achieved, reason=reason)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("judge parse failed: %s content=%r", exc, content[:200])
            return None

    # ------------------------------------------------------------------
    # Build result from chat completion
    # ------------------------------------------------------------------

    async def _build_result_from_chat(
        self,
        task: Task,
        task_run_id: int,
        result: ChatCompletionResult,
    ) -> TaskAgentResult:
        """Construct TaskAgentResult from chat completion + intent events.

        Reads the latest ``complete_requested`` or ``change_proposed`` event
        for the task. If ``complete_requested`` is found, maps to COMPLETED.
        If ``change_proposed`` is found (written by TaskService.propose_change
        when the worker called task_propose_change), maps to WAITING_APPROVAL;
        the actual run finalization is performed by
        TaskRunService.finalize_propose (called inside the tool execution).
        If no intent event is found, defaults to COMPLETED with the final
        output as summary.
        """
        output = ""
        if isinstance(result.message, dict):
            content = result.message.get("content", "")
            if isinstance(content, str):
                output = content
            elif isinstance(content, list):
                output = "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") in (None, "text")
                )

        if result.finish_reason == "error":
            return TaskAgentResult(
                status=TaskRunOutcome.FAILED,
                output=output,
                error=output or "chat completion error",
            )

        # Read latest intent event
        intent = await self._read_latest_intent(task.id, task_run_id)
        if intent is not None:
            kind = intent.get("kind")
            if kind == "change_proposed":
                # Worker called task_propose_change -> WAITING_APPROVAL.
                # TaskRunService.finalize_propose has already been called
                # inside the tool execution; the returned intent lets
                # _finalize_run handle the CAS conflict gracefully.
                proposal = intent.get("proposal", "")
                return TaskAgentResult(
                    status=TaskRunOutcome.WAITING_APPROVAL,
                    output=output,
                    metadata={"proposal": proposal},
                    error=None,
                )
            if kind == "fail_requested":
                # Worker called task_fail -> ABORTED -> task FAILED（绕过断路器，不重试）。
                reason = intent.get("error") or "worker aborted"
                return TaskAgentResult(
                    status=TaskRunOutcome.ABORTED,
                    output=reason,
                    error=reason,
                )
            # complete_requested
            outcome_str = intent.get("outcome", "")
            try:
                outcome = TaskRunOutcome(outcome_str)
            except ValueError:
                outcome = TaskRunOutcome.COMPLETED
            metadata = intent.get("metadata", {}) if isinstance(intent.get("metadata"), dict) else {}
            artifacts_raw = intent.get("artifacts", [])
            artifacts = tuple(artifacts_raw) if isinstance(artifacts_raw, list) else ()
            return TaskAgentResult(
                status=outcome,
                output=str(intent.get("summary") or output),
                metadata=metadata,
                artifacts=artifacts,
            )

        # No explicit intent event: worker did not call task_complete/propose.
        # finish_reason "length" = BUDGET_EXHAUSTED or ITERATION_LIMIT (worker hit a
        # limit without completing) -> FAILED, not COMPLETED (otherwise a budget-exhausted
        # run is misclassified as SUCCEEDED, causing 看板/Chat 最终结果不一致).
        # finish_reason "stop" = worker gave a final answer -> COMPLETED with output as
        # summary (existing behavior).
        if result.finish_reason == "length":
            return TaskAgentResult(
                status=TaskRunOutcome.FAILED,
                output=output,
                error=output or "run ended without task_complete (limit reached)",
            )
        return TaskAgentResult(
            status=TaskRunOutcome.COMPLETED,
            output=output,
            metadata={"summary": output[:500]} if output else {},
        )

    async def _read_latest_intent(
        self, task_id: str, run_id: int
    ) -> dict[str, Any] | None:
        """Read the latest complete_requested / change_proposed / fail_requested event.

        Events are appended by TaskService.complete / TaskService.propose_change /
        TaskService.fail inside the agent loop. We read the most recent one matching
        this run_id.

        Returns a dict augmented with the event ``kind`` so the caller can
        distinguish complete_requested (-> COMPLETED) from change_proposed
        (-> WAITING_APPROVAL) from fail_requested (-> ABORTED).
        """
        try:
            events = await self.task_registry.list_events(task_id, limit=50)
        except Exception:
            return None
        # Search backwards for the latest intent event
        for event in reversed(events):
            if event.kind not in ("complete_requested", "change_proposed", "fail_requested"):
                continue
            if event.run_id is not None and event.run_id != run_id:
                continue
            payload = dict(event.payload)
            payload["kind"] = event.kind
            return payload
        return None

    # ------------------------------------------------------------------
    # System prompt builder
    # ------------------------------------------------------------------

    def _build_system_prompt(self, task: Task) -> str:
        """Build the base system prompt using the injected prompt_builder.

        If prompt_builder is a callable, call it. If it has
        ``build_system_prompt``, call that method. Otherwise use a default.
        """
        if self.prompt_builder is None:
            return _DEFAULT_SYSTEM_PROMPT
        if hasattr(self.prompt_builder, "build_system_prompt"):
            return self.prompt_builder.build_system_prompt()
        if callable(self.prompt_builder):
            return self.prompt_builder()
        return _DEFAULT_SYSTEM_PROMPT


_DEFAULT_SYSTEM_PROMPT = (
    "You are N-Agent, an intelligent task execution agent. "
    "You help complete tasks by understanding the goal, using available tools, "
    "and reporting results clearly."
)
