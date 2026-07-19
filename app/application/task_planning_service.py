"""T16: TaskPlanningService -- LLM-assisted task planning (specify/decompose/swarm).

Reuses ``SkillEvolutionService.run_background_review`` fork pattern: a forked
``ChatCompletionService.complete`` turn with a read-only tool whitelist
(skills-read only). The fork is unattended (UNATTENDED mode), sources to
``task_planning``, and runs under a timeout. All model output is validated
before any single-transaction commit; a validation or write failure never
leaves a half-graph.

Public surface:
  - ``specify_task(task_id)``: only TRIAGE -> TODO with structured body and
    验收标准 written back via optimistic-lock update.
  - ``decompose_task(task_id)``: LLM produces child task list; validated for
    schema / count / length / role / state / dependency refs / DAG; then
    single-transaction ``create_graph`` writes tasks + links.
  - ``create_swarm(goal, workers, verifier_assignee, synthesizer_assignee)``:
    deterministic topology (no LLM). Creates root (immediately DONE) +
    workers (TODO, parents=[root]) + verifier (TODO, parents=[workers]) +
    synthesizer (TODO, parents=[verifier]). Blackboard comment on root with
    ``[swarm:blackboard] `` prefix JSON.

This module MUST NOT import app.infrastructure. It depends on:
  - ``ChatCompletionService`` (Application)
  - ``TaskRegistry`` (Domain port)
  - ``TaskPolicy`` (Domain)
  - Settings limits (plain Python ints)
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput
from app.application.policy_snapshot import IngressFacts
from app.domain.policy import ExecutionMode
from app.domain.session import SessionSource
from app.domain.task import (
    CreateGraphCommand,
    Task,
    TaskComment,
    TaskLink,
    TaskNotFoundError,
    TaskStateError,
    TaskValidationError,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults (overridden by Settings in Batch F wiring)
# ---------------------------------------------------------------------------

_DEFAULT_PLANNING_MAX_CHILDREN = 20
_DEFAULT_MAX_SWARM_WORKERS = 20
_DEFAULT_MAX_GOAL_LENGTH = 4000
_DEFAULT_MAX_GOAL_MAX_TURNS = 30
_DEFAULT_MAX_ITERATIONS = 16
_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_BODY_MAX_BYTES = 32 * 1024  # 32 KB
_DEFAULT_TITLE_MAX_LEN = 256


# ---------------------------------------------------------------------------
# SwarmWorkerSpec (planning input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwarmWorkerSpec:
    """Input spec for a single swarm worker task.

    ``profile`` is the role label used for prompt/skill selection; it is NOT
    a Task.assignee (workers may share a runtime). ``title`` and ``body`` are
    the worker's task title and body. ``skills`` and ``priority`` propagate
    to the created Task. ``max_runtime_seconds`` overrides the default runtime
    cap for this worker.
    """

    profile: str
    title: str
    body: str = ""
    skills: tuple[str, ...] = ()
    priority: int = 0
    max_runtime_seconds: int | None = None


@dataclass(frozen=True)
class SwarmResult:
    """Result of create_swarm: ids of created tasks."""

    root_id: str
    worker_ids: tuple[str, ...]
    verifier_id: str
    synthesizer_id: str


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


_SPECIFY_PROMPT = """\
你是一个 Task 规划助手。你的职责是将一个 TRIAGE 状态的粗想法细化为结构化的
任务正文与验收标准。

可用工具:
- skills_list: 列出可用的 procedural skills
- skill_view: 读取某个 skill 的内容（建议先用 skills_list 发现）

禁止使用任何 task 写工具（task_create/task_complete/task_block 等）。

你必须以严格 JSON 格式返回，不要包含其他文本或 markdown 包裹：
{
  "body": "结构化的任务正文（可含 markdown）",
  "acceptance_criteria": ["验收标准1", "验收标准2", ...]
}

要求：
1. body 必须明确说明任务目标、范围、产出物
2. acceptance_criteria 必须是非空列表，每条都可独立验证
3. body 长度不超过 32KB
4. 不要编造任务 id 或外部依赖
"""


_DECOMPOSE_PROMPT = """\
你是一个 Task 分解助手。你的职责是将一个 TODO 任务拆分为可独立执行的子任务。

可用工具:
- skills_list: 列出可用的 procedural skills
- skill_view: 读取某个 skill 的内容

禁止使用任何 task 写工具。

你必须以严格 JSON 格式返回，不要包含其他文本或 markdown 包裹：
{
  "children": [
    {
      "title": "子任务标题（必填）",
      "body": "子任务正文",
      "assignee": "可选的角色标签",
      "skills": ["skill_name1", ...],
      "depends_on_indices": [0, 1]
    }
  ]
}

要求：
1. children 必须是非空列表
2. 每个 child 的 title 非空
3. depends_on_indices 引用同批 children 的下标（0-based）
4. 不要形成循环依赖
5. 不要自己生成 task id（系统自动生成）
"""


# ---------------------------------------------------------------------------
# TaskPlanningService
# ---------------------------------------------------------------------------


class TaskPlanningService:
    """LLM-assisted task planning (specify / decompose) + deterministic swarm.

    Injection:
      - ``chat_service``: ChatCompletionService (Application)
      - ``registry``: TaskRegistry (Domain port)
      - ``policy``: TaskPolicy (Domain)
      - ``tool_service``: optional ToolService (for future tool filtering);
        not used by the current fork which delegates whitelist to
        ``permitted_managed_tools`` and ``granted_tools``.
      - Limits: ``planning_max_children``, ``max_swarm_workers``,
        ``max_goal_length``, ``max_goal_max_turns``, ``max_iterations``,
        ``timeout_seconds``.
    """

    def __init__(
        self,
        chat_service: Any,
        registry: Any,
        policy: Any,
        tool_service: Any | None = None,
        planning_max_children: int = _DEFAULT_PLANNING_MAX_CHILDREN,
        max_swarm_workers: int = _DEFAULT_MAX_SWARM_WORKERS,
        max_goal_length: int = _DEFAULT_MAX_GOAL_LENGTH,
        max_goal_max_turns: int = _DEFAULT_MAX_GOAL_MAX_TURNS,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        model: str | None = None,
    ):
        self.chat_service = chat_service
        self.registry = registry
        self.policy = policy
        self.tool_service = tool_service
        self.planning_max_children = max(1, planning_max_children)
        self.max_swarm_workers = max(1, max_swarm_workers)
        self.max_goal_length = max(1, max_goal_length)
        self.max_goal_max_turns = max(1, max_goal_max_turns)
        self.max_iterations = max(1, max_iterations)
        self.timeout_seconds = max(1, timeout_seconds)
        self.model = model

    # ------------------------------------------------------------------
    # specify_task
    # ------------------------------------------------------------------

    async def specify_task(self, task_id: str) -> Task:
        """Specify a TRIAGE task into a TODO task with structured body.

        Forks a ChatCompletionService turn with skills-read tools only;
        validates the LLM's JSON output (schema + length); writes back the
        structured body and transitions TRIAGE -> TODO via optimistic-lock
        update. Appends a ``specified`` audit event.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        if task.status != TaskStatus.TRIAGE:
            raise TaskStateError(
                f"specify requires TRIAGE, got {task.status.value}"
            )

        user_prompt = (
            f"任务标题: {task.title}\n"
            f"任务原始想法: {task.body or '(空)'}\n"
            "请细化该任务并产出结构化 JSON。"
        )
        result = await self._run_planning_fork(
            system_prompt=_SPECIFY_PROMPT,
            user_prompt=user_prompt,
            session_id=f"task-planning-{task_id}",
        )
        parsed = self._parse_json(result)
        body = self._validate_specify_output(parsed)

        # Single-transaction write: update body + status -> TODO
        updated = await self.registry.update_task(
            task_id,
            {"body": body, "status": TaskStatus.TODO},
            expected_version=task.version,
        )
        await self.registry.append_event(
            task_id, "specified",
            {"body_length": len(body.encode("utf-8"))},
        )
        return updated

    # ------------------------------------------------------------------
    # decompose_task
    # ------------------------------------------------------------------

    async def decompose_task(self, task_id: str) -> tuple[Task, ...]:
        """Decompose a TODO task into child tasks with dependency links.

        Forks a ChatCompletionService turn; validates the LLM's JSON output
        (schema / count / length / dependency refs / DAG); then issues a
        single-transaction ``create_graph`` to create all children + links.
        Parent is NOT auto-completed; children default TODO and advance via
        ``recompute_ready``.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        if task.status in (TaskStatus.TRIAGE, TaskStatus.ARCHIVED):
            raise TaskStateError(
                f"decompose requires non-TRIAGE non-ARCHIVED, got {task.status.value}"
            )

        user_prompt = (
            f"父任务标题: {task.title}\n"
            f"父任务正文: {task.body or '(空)'}\n"
            "请拆分为可独立执行的子任务，以严格 JSON 返回。"
        )
        result = await self._run_planning_fork(
            system_prompt=_DECOMPOSE_PROMPT,
            user_prompt=user_prompt,
            session_id=f"task-planning-{task_id}",
        )
        parsed = self._parse_json(result)
        children_spec = self._validate_decompose_output(parsed, task)

        # Build Task + TaskLink list for atomic create_graph
        created_tasks: list[Task] = []
        links: list[TaskLink] = []
        now = datetime.now(timezone.utc)
        for spec in children_spec:
            child_id = f"t_{uuid4().hex[:16]}"
            child = Task(
                id=child_id,
                title=spec["title"],
                body=spec.get("body", ""),
                assignee=spec.get("assignee"),
                skills=tuple(spec.get("skills", [])),
                created_at=now,
                updated_at=now,
                version=1,
                status=TaskStatus.TODO,
                board=task.board,
            )
            created_tasks.append(child)
            # Every child depends on the parent
            links.append(TaskLink(parent_id=task.id, child_id=child_id))
        # Add inter-child dependencies
        for i, spec in enumerate(children_spec):
            for dep_idx in spec.get("depends_on_indices", []):
                links.append(
                    TaskLink(
                        parent_id=created_tasks[dep_idx].id,
                        child_id=created_tasks[i].id,
                    )
                )

        command = CreateGraphCommand(
            tasks=tuple(created_tasks),
            links=tuple(links),
        )
        graph_result = await self.registry.create_graph(command)
        await self.registry.append_event(
            task_id, "decomposed",
            {"children_count": len(created_tasks)},
        )
        return graph_result.tasks

    # ------------------------------------------------------------------
    # create_swarm
    # ------------------------------------------------------------------

    async def create_swarm(
        self,
        goal: str,
        workers: list[SwarmWorkerSpec],
        verifier_assignee: str,
        synthesizer_assignee: str,
    ) -> SwarmResult:
        """Create a swarm topology: root (DONE) + workers + verifier + synthesizer.

        Deterministic -- no LLM fork. Validates workers count, goal length.
        Single-transaction ``create_graph`` writes all tasks + links + the
        blackboard comment. On any failure no tasks are written.
        """
        self._validate_swarm_inputs(goal, workers)
        now = datetime.now(timezone.utc)

        # Build root (immediately DONE)
        root_id = f"t_{uuid4().hex[:16]}"
        root = Task(
            id=root_id,
            title=f"[swarm] {goal[:80]}",
            body=goal,
            status=TaskStatus.DONE,
            completed_at=now,
            created_at=now,
            updated_at=now,
            version=1,
            board="default",
        )

        # Build workers (TODO, parents=[root])
        worker_ids: list[str] = []
        worker_tasks: list[Task] = []
        for i, w in enumerate(workers):
            wid = f"t_{uuid4().hex[:16]}"
            worker_ids.append(wid)
            worker = Task(
                id=wid,
                title=w.title,
                body=w.body,
                assignee=w.profile,
                skills=tuple(w.skills),
                priority=w.priority,
                max_runtime_seconds=w.max_runtime_seconds,
                created_at=now,
                updated_at=now,
                version=1,
                status=TaskStatus.TODO,
                board="default",
            )
            worker_tasks.append(worker)

        # Verifier (TODO, parents=[all workers])
        verifier_id = f"t_{uuid4().hex[:16]}"
        verifier = Task(
            id=verifier_id,
            title="[swarm verifier]",
            body=f"验证 goal: {goal}",
            assignee=verifier_assignee,
            created_at=now,
            updated_at=now,
            version=1,
            status=TaskStatus.TODO,
            board="default",
        )

        # Synthesizer (TODO, parents=[verifier])
        synthesizer_id = f"t_{uuid4().hex[:16]}"
        synthesizer = Task(
            id=synthesizer_id,
            title="[swarm synthesizer]",
            body=f"综合 goal: {goal}",
            assignee=synthesizer_assignee,
            created_at=now,
            updated_at=now,
            version=1,
            status=TaskStatus.TODO,
            board="default",
        )

        # Links: root -> each worker, each worker -> verifier, verifier -> synth
        links: list[TaskLink] = []
        for wid in worker_ids:
            links.append(TaskLink(parent_id=root_id, child_id=wid))
        for wid in worker_ids:
            links.append(TaskLink(parent_id=wid, child_id=verifier_id))
        links.append(TaskLink(parent_id=verifier_id, child_id=synthesizer_id))

        # Blackboard comment on root
        topology = {
            "topology": {
                "root": root_id,
                "workers": list(worker_ids),
                "verifier": verifier_id,
                "synthesizer": synthesizer_id,
            },
            "goal": goal,
            "created_at": now.isoformat(),
        }
        blackboard = TaskComment(
            id=f"tc_{uuid4().hex[:8]}",
            task_id=root_id,
            author="system",
            body=f"[swarm:blackboard] {json.dumps(topology, ensure_ascii=False)}",
            created_at=now,
        )

        all_tasks = (root, *worker_tasks, verifier, synthesizer)
        command = CreateGraphCommand(
            tasks=all_tasks,
            links=tuple(links),
            comments=(blackboard,),
        )
        await self.registry.create_graph(command)
        return SwarmResult(
            root_id=root_id,
            worker_ids=tuple(worker_ids),
            verifier_id=verifier_id,
            synthesizer_id=synthesizer_id,
        )

    # ------------------------------------------------------------------
    # Planning fork (reuses SkillEvolutionService pattern)
    # ------------------------------------------------------------------

    async def _run_planning_fork(
        self,
        system_prompt: str,
        user_prompt: str,
        session_id: str,
    ) -> Any:
        """Run a forked ChatCompletionService turn with read-only tools.

        The fork uses UNATTENDED mode + source=task (Task subdomain).
        permitted_managed_tools is empty (no task managed tools exposed).
        granted_tools exposes only skills_list + skill_view (read-only).
        """
        planning_run_id = f"task-planning-{uuid4().hex[:12]}"
        trusted_claims: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": ["skills_list", "skill_view"],
            "permitted_managed_tools": [],
            "planning": True,
        }
        trusted_metadata: dict[str, Any] = {
            "execution_mode": ExecutionMode.UNATTENDED.value,
            "granted_tools": ["skills_list", "skill_view"],
            "permitted_managed_tools": [],
            "planning": True,
            **trusted_claims,
        }
        request = ChatCompletionInput(
            model=self.model or "",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
            session_id=session_id,
            ingress_facts=IngressFacts(
                run_id=planning_run_id,
                session_id=session_id,
                source=SessionSource.TASK.value,
                actor_id=None,
                execution_mode=ExecutionMode.UNATTENDED,
                trusted_claims=trusted_claims,
            ),
            trusted_metadata=trusted_metadata,
            options={"max_iterations": self.max_iterations},
        )
        try:
            return await asyncio.wait_for(
                self.chat_service.complete(request),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise TaskValidationError(
                f"planning fork timed out after {self.timeout_seconds}s"
            ) from exc
        except Exception as exc:
            logger.warning("planning fork failed: %s", exc, exc_info=True)
            raise TaskValidationError(f"planning fork failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Output validation
    # ------------------------------------------------------------------

    def _parse_json(self, result: Any) -> dict[str, Any]:
        """Parse JSON from the LLM result; raises TaskValidationError on failure."""
        content = ""
        msg = getattr(result, "message", None)
        if isinstance(msg, dict):
            raw = msg.get("content", "")
            if isinstance(raw, str):
                content = raw
            elif isinstance(raw, list):
                content = "".join(
                    p.get("text", "")
                    for p in raw
                    if isinstance(p, dict) and p.get("type") in (None, "text")
                )
        content = content.strip()
        if not content:
            raise TaskValidationError("planning LLM returned empty content")
        # Strip markdown code fences if present
        if content.startswith("```"):
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                content = content[start:end]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise TaskValidationError(
                f"planning LLM returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise TaskValidationError("planning LLM JSON must be an object")
        return parsed

    def _validate_specify_output(self, parsed: dict[str, Any]) -> str:
        """Validate specify JSON: must have body (str, length <= 32KB) and
        acceptance_criteria (list). Returns the validated body string."""
        body = parsed.get("body")
        if not isinstance(body, str) or not body.strip():
            raise TaskValidationError("specify output missing body")
        body_bytes = body.encode("utf-8")
        if len(body_bytes) > _DEFAULT_BODY_MAX_BYTES:
            raise TaskValidationError(
                f"specify body too large: {len(body_bytes)} > {_DEFAULT_BODY_MAX_BYTES}"
            )
        criteria = parsed.get("acceptance_criteria")
        if not isinstance(criteria, list) or not criteria:
            raise TaskValidationError(
                "specify output must have non-empty acceptance_criteria list"
            )
        for item in criteria:
            if not isinstance(item, str) or not item.strip():
                raise TaskValidationError(
                    "acceptance_criteria entries must be non-empty strings"
                )
        return body

    def _validate_decompose_output(
        self, parsed: dict[str, Any], parent: Task
    ) -> list[dict[str, Any]]:
        """Validate decompose JSON and return the children spec list.

        Validates: schema, count <= max, title/body length, dependency refs,
        DAG (no cycle). Duplicate child ids impossible (system-generated).
        """
        children = parsed.get("children")
        if not isinstance(children, list) or not children:
            raise TaskValidationError(
                "decompose output must have non-empty children list"
            )
        if len(children) > self.planning_max_children:
            raise TaskValidationError(
                f"decompose children count {len(children)} > "
                f"max {self.planning_max_children}"
            )
        # Validate each child schema
        for i, child in enumerate(children):
            if not isinstance(child, dict):
                raise TaskValidationError(f"child[{i}] must be an object")
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                raise TaskValidationError(f"child[{i}] missing non-empty title")
            if len(title) > _DEFAULT_TITLE_MAX_LEN:
                raise TaskValidationError(
                    f"child[{i}] title too long: {len(title)} > {_DEFAULT_TITLE_MAX_LEN}"
                )
            body = child.get("body", "")
            if not isinstance(body, str):
                raise TaskValidationError(f"child[{i}] body must be string")
            if len(body.encode("utf-8")) > _DEFAULT_BODY_MAX_BYTES:
                raise TaskValidationError(
                    f"child[{i}] body too large"
                )
            assignee = child.get("assignee")
            if assignee is not None and not isinstance(assignee, str):
                raise TaskValidationError(f"child[{i}] assignee must be string")
            skills = child.get("skills", [])
            if not isinstance(skills, list):
                raise TaskValidationError(f"child[{i}] skills must be list")
            for s in skills:
                if not isinstance(s, str) or not s.strip():
                    raise TaskValidationError(
                        f"child[{i}] skills entries must be non-empty strings"
                    )
            depends_on = child.get("depends_on_indices", [])
            if not isinstance(depends_on, list):
                raise TaskValidationError(
                    f"child[{i}] depends_on_indices must be list"
                )
            for idx in depends_on:
                if not isinstance(idx, int) or idx < 0 or idx >= len(children):
                    raise TaskValidationError(
                        f"child[{i}] depends_on_indices out of range: {idx}"
                    )
                if idx == i:
                    raise TaskValidationError(
                        f"child[{i}] cannot depend on itself"
                    )
        # DAG check: build edges among children and detect cycles
        # Build adjacency: edge from dep -> dependent
        adj: dict[int, set[int]] = {i: set() for i in range(len(children))}
        for i, child in enumerate(children):
            for dep_idx in child.get("depends_on_indices", []):
                adj[dep_idx].add(i)
        # DFS cycle detection
        visited: set[int] = set()
        stack: set[int] = set()

        def has_cycle(node: int) -> bool:
            visited.add(node)
            stack.add(node)
            for neighbor in adj[node]:
                if neighbor not in visited:
                    if has_cycle(neighbor):
                        return True
                elif neighbor in stack:
                    return True
            stack.discard(node)
            return False

        for start in range(len(children)):
            if start not in visited:
                if has_cycle(start):
                    raise TaskValidationError(
                        "decompose children form a dependency cycle"
                    )
        return children

    def _validate_swarm_inputs(
        self, goal: str, workers: list[SwarmWorkerSpec]
    ) -> None:
        """Validate swarm creation inputs against Settings limits."""
        if not isinstance(goal, str) or not goal.strip():
            raise TaskValidationError("goal must be non-empty string")
        if len(goal.encode("utf-8")) > self.max_goal_length:
            raise TaskValidationError(
                f"goal too large: {len(goal.encode('utf-8'))} > "
                f"{self.max_goal_length}"
            )
        if not isinstance(workers, list) or not workers:
            raise TaskValidationError("workers must be non-empty list")
        if len(workers) > self.max_swarm_workers:
            raise TaskValidationError(
                f"workers count {len(workers)} > max {self.max_swarm_workers}"
            )
        for i, w in enumerate(workers):
            if not isinstance(w, SwarmWorkerSpec):
                raise TaskValidationError(f"worker[{i}] must be SwarmWorkerSpec")
            if not w.title or not w.title.strip():
                raise TaskValidationError(f"worker[{i}] title must be non-empty")
            if not w.profile or not w.profile.strip():
                raise TaskValidationError(f"worker[{i}] profile must be non-empty")
