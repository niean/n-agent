"""T13: TaskAgentExecutor -- in-process worker that reuses AgentRunner.

Calls ``ChatCompletionService.complete(stream=False)`` with trusted task
context, aligned with ``ScheduledAgentExecutor``'s calling boundary:

  - ``IngressFacts`` with ``source=task``, ``ExecutionMode.UNATTENDED``
  - Same ``run_id`` in both ``IngressFacts`` and ``trusted_metadata``
  - task 7 tools in ``permitted_managed_tools`` (NOT ``granted_tools``)
  - ``trusted_metadata.task`` carries the server-side immutable claim context
  - ``trusted_claims`` mirrors the same claim identifiers

The executor returns ``TaskAgentResult`` (terminal INTENT only). It does NOT
call ``finish_run`` -- ``TaskRunService`` (T14) owns the single CAS-based
finalization path.

Terminal intent detection:
  After ``chat_service.complete()`` returns, the executor reads the latest
  ``complete_requested`` or ``block_requested`` event for the task. These
  events are written by ``TaskService.complete/block`` (called by the tool
  executor inside the agent loop). If no intent event is found, the executor
  defaults to COMPLETED with the final output as summary.

goal_mode:
  ``run_goal_loop`` runs multiple turns, each calling ``run()`` once. A
  judge fork (read-only, no write tools) evaluates ``{achieved, reason}``.
  ``goal_max_turns`` budget caps the loop. Not achieved at max turns ->
  BLOCKED(NEEDS_INPUT). Judge parse failure -> retryable FAILED.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.policy_snapshot import IngressFacts
from app.application.task_tools import TASK_TOOL_NAMES
from app.domain.policy import ExecutionMode
from app.domain.task import Task, TaskRunOutcome

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TASK_GUIDANCE (aligns Hermes KANBAN_GUIDANCE)
# ---------------------------------------------------------------------------

TASK_GUIDANCE = """\
## Task Worker Guidance

你正在执行一个 Task（异步后台任务）。请严格遵循以下步骤：

1. 先调用 task_show 读取任务完整上下文（标题、正文、父任务交接、先前尝试、评论）
2. 在 workspace 中使用通用工具干活，不要凭空假设
3. 长任务执行中周期调用 task_heartbeat 续租 lease，避免被 reclaim
4. 遇到歧义或缺少信息时调用 task_block 提交阻塞意图（附 reason + kind）
5. 完成后调用 task_complete 提交完成意图，附带 summary + metadata + artifacts
6. 后续工作应通过 task_create 派发子任务，不要自己做完所有事

重要：task_complete 和 task_block 只提交终态意图，系统会以 claim token 一次性
终结 run。不要尝试直接修改 task 状态。
"""


# ---------------------------------------------------------------------------
# TaskAgentResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskAgentResult:
    """Terminal intent returned by the executor.

    ``status`` is the terminal outcome intent (COMPLETED / BLOCKED / FAILED /
    TIMED_OUT). ``output`` is the agent's final text. ``metadata`` and
    ``artifacts`` come from the task_complete tool call. ``error`` is set
    for FAILED/TIMED_OUT.

    The executor does NOT finalize the run; TaskRunService reads this result
    and performs the CAS finalize.
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


_JUDGE_PROMPT = """\
你是一个 Task goal_mode judge。你的职责是判断 task 的 goal 是否已达成。

你只能使用只读工具（task_show, search_knowledge, skills_list, skill_view），
禁止使用任何写工具。

判断完成后，你必须以严格 JSON 格式返回结果，不要包含其他内容：
{"achieved": true/false, "reason": "简要说明判断依据"}

判断依据：
- task 的 goal 是否已在 workspace 中产出可验证的成果
- task_complete 是否已被调用并附带合理 summary
- 如果 task 仍在进行中或成果不完整，achieved=false
"""


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
    ):
        self.chat_service = chat_service
        self.task_registry = task_registry
        self.prompt_builder = prompt_builder
        self.max_runtime_seconds = max_runtime_seconds
        self.goal_max_turns = goal_max_turns

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

        Constructs the system prompt (build_system_prompt + TASK_GUIDANCE),
        user prompt ``work task {task.id}``, and calls chat_service.complete
        with trusted task context. After completion, reads the latest intent
        event to determine the terminal outcome.
        """
        execution_session_id = task.execution_session_id or f"task-{task.id}"
        execution_run_id = f"task-run-{uuid4().hex[:12]}"

        # Build system prompt
        system_prompt = self._build_system_prompt(task)
        # Inject TASK_GUIDANCE only when task tools are visible (always true
        # for task workers -- they have permitted_managed_tools)
        system_prompt = system_prompt + "\n\n" + TASK_GUIDANCE

        # Build granted tools and permitted managed tools
        granted_tools = list(task.execution_policy.allowed_tools)
        permitted_managed = set(TASK_TOOL_NAMES)

        # Build trusted task context (server-side immutable)
        task_context: dict[str, Any] = {
            "task_id": task.id,
            "run_id": task_run_id,
            "claim_lock": claim_lock,
            "write_origin": "worker",
            "board": task.board,
            "execution_session_id": execution_session_id,
        }

        # Build trusted_claims (mirrors trusted_metadata)
        trusted_claims: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": granted_tools,
            "permitted_managed_tools": list(permitted_managed),
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

        messages = [
            {"role": "system", "content": system_prompt},
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
                error=f"task execution timed out after {task.max_runtime_seconds or self.max_runtime_seconds}s",
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
          - goal_max_turns reached -> BLOCKED(NEEDS_INPUT)
          - Judge parse failure -> FAILED (retryable)
          - Total runtime exceeded -> TIMED_OUT
        """
        max_turns = task.goal_max_turns or self.goal_max_turns
        max_runtime = task.max_runtime_seconds or self.max_runtime_seconds
        start = asyncio.get_event_loop().time()
        last_result: TaskAgentResult | None = None

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

            # If the turn itself already terminal (BLOCKED/FAILED/TIMED_OUT),
            # return immediately
            if turn_result.status in (
                TaskRunOutcome.BLOCKED,
                TaskRunOutcome.FAILED,
                TaskRunOutcome.TIMED_OUT,
                TaskRunOutcome.CRASHED,
            ):
                return turn_result

            # If the turn completed, run the judge
            if turn_result.status == TaskRunOutcome.COMPLETED:
                judge = await self._run_judge(task)
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
                # Not achieved -- continue to next turn (unless this was the last)
                if turn >= max_turns:
                    return TaskAgentResult(
                        status=TaskRunOutcome.BLOCKED,
                        output=turn_result.output,
                        error=f"goal not achieved after {max_turns} turns: {judge.reason}",
                        metadata={"block_kind": "needs_input", "judge_reason": judge.reason},
                    )

        # Should not reach here, but defensive
        return last_result or TaskAgentResult(
            status=TaskRunOutcome.FAILED,
            error="goal_loop exited without result",
        )

    # ------------------------------------------------------------------
    # Judge fork
    # ------------------------------------------------------------------

    async def _run_judge(self, task: Task) -> JudgeResult | None:
        """Run a judge fork to evaluate goal achievement.

        Returns None on parse failure (retryable FAILED). The judge has
        read-only tools only -- no write tools, no task tools.
        """
        execution_session_id = task.execution_session_id or f"task-{task.id}"
        judge_run_id = f"task-judge-{uuid4().hex[:12]}"

        trusted_claims: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": [],  # No write tools for judge
            "permitted_managed_tools": [],  # No task tools for judge
            "task_id": task.id,
            "judge": True,
        }

        request = ChatCompletionInput(
            model=task.model_override or "N-Agent",
            messages=[
                {"role": "system", "content": _JUDGE_PROMPT},
                {"role": "user", "content": f"Judge task {task.id}: has the goal been achieved?"},
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
                "permitted_managed_tools": [],
                **trusted_claims,
            },
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

        Reads the latest ``complete_requested`` or ``block_requested`` event
        for the task. If found, uses the event payload as the terminal intent.
        If not found, defaults to COMPLETED with the final output as summary.
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
                output=output,
                metadata=metadata,
                artifacts=artifacts,
                error=intent.get("reason") if outcome == TaskRunOutcome.BLOCKED else None,
            )

        # No explicit intent event -- default to COMPLETED
        return TaskAgentResult(
            status=TaskRunOutcome.COMPLETED,
            output=output,
            metadata={"summary": output[:500]} if output else {},
        )

    async def _read_latest_intent(
        self, task_id: str, run_id: int
    ) -> dict[str, Any] | None:
        """Read the latest complete_requested or block_requested event.

        Events are appended by TaskService.complete/block inside the agent
        loop. We read the most recent one matching this run_id.
        """
        try:
            events = await self.task_registry.list_events(task_id, limit=50)
        except Exception:
            return None
        # Search backwards for the latest intent event
        for event in reversed(events):
            if event.kind not in ("complete_requested", "block_requested"):
                continue
            if event.run_id is not None and event.run_id != run_id:
                continue
            return dict(event.payload)
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
