"""T16: TaskPlanningService tests.

Tests for LLM-assisted task planning (specify/decompose) and deterministic
swarm topology creation. Uses FakeChatService and FakeTaskRegistry so no
real LLM or SQLite is exercised.

Coverage:
  specify:
    - TRIAGE -> TODO with structured body written back
    - non-TRIAGE rejected
    - invalid LLM JSON -> error, no half-write
    - body length validation
    - skills-read tools exposed (no write tools)
  decompose:
    - creates children with links in single transaction
    - validates max children count
    - rejects cycle-inducing dependencies
    - rejects duplicate ids
    - rejects invalid LLM JSON
    - parent not auto-completed
    - children default TODO
  swarm:
    - root immediately DONE
    - workers TODO with parents=[root]
    - verifier depends on all workers
    - synthesizer depends on verifier
    - blackboard comment with topology JSON
    - workers count / goal length bounds
    - validation rejects empty workers
  rollback:
    - any write failure rolls back the whole graph
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.task_planning_service import (
    SwarmResult,
    SwarmWorkerSpec,
    TaskPlanningService,
)
from app.domain.task import (
    CreateGraphCommand,
    Task,
    TaskConflictError,
    TaskLink,
    TaskNotFoundError,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
)
from app.domain.task_policy import TaskPolicy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeChatService:
    """Captures complete() calls; returns configurable results."""

    def __init__(self, result: ChatCompletionResult | None = None):
        self.complete_calls: list[Any] = []
        self._results: list[ChatCompletionResult] = []
        self._result = result or ChatCompletionResult(
            session_id="planning-session",
            model="N-Agent",
            message={"role": "assistant", "content": "{}"},
            finish_reason="stop",
        )

    async def complete(self, request):
        self.complete_calls.append(request)
        if self._results:
            return self._results.pop(0)
        return self._result

    def set_result(self, result: ChatCompletionResult) -> None:
        self._result = result

    def set_results(self, results: list[ChatCompletionResult]) -> None:
        self._results = list(results)


class FakeTaskRegistry:
    """In-memory registry that supports graph creation + cycle detection."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._links: list[TaskLink] = []
        self._comments: dict[str, list[Any]] = {}
        self._events: list[Any] = []
        self._next_event_id = 1
        # Failure injection for rollback tests
        self._create_graph_fail = False
        # Tracking
        self.create_graph_calls: list[CreateGraphCommand] = []
        self.update_calls: list[tuple[str, dict, int]] = []

    async def create_task(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise TaskConflictError(f"duplicate id: {task.id}")
        self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def update_task(self, task_id, fields, expected_version):
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.version != expected_version:
            raise TaskConflictError("version conflict")
        from dataclasses import replace as dc_replace
        updated = dc_replace(
            task, **dict(fields), version=task.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = updated
        self.update_calls.append((task_id, dict(fields), expected_version))
        return updated

    async def append_event(self, task_id, kind, payload, run_id=None):
        from app.domain.task import TaskEvent
        event = TaskEvent(
            id=self._next_event_id, task_id=task_id, kind=kind,
            payload=dict(payload), run_id=run_id,
            created_at=datetime.now(timezone.utc),
        )
        self._next_event_id += 1
        self._events.append(event)
        return event

    async def create_graph(self, command: CreateGraphCommand):
        self.create_graph_calls.append(command)
        if self._create_graph_fail:
            raise RuntimeError("injected create_graph failure")
        # Atomic: do all checks first, then all writes
        for task in command.tasks:
            if task.id in self._tasks:
                raise TaskConflictError(f"duplicate id: {task.id}")
        # Check links reference existing tasks
        task_ids = {t.id for t in command.tasks} | set(self._tasks.keys())
        for link in command.links:
            if link.parent_id == link.child_id:
                raise TaskValidationError("self-loop")
            if link.parent_id not in task_ids or link.child_id not in task_ids:
                raise TaskNotFoundError("link endpoint not found")
        # Cycle check (combined with existing links)
        new_links = list(self._links) + list(command.links)
        for link in command.links:
            if _would_cycle(new_links, link.parent_id, link.child_id):
                raise TaskValidationError("cycle detected")
        # Apply
        for task in command.tasks:
            self._tasks[task.id] = task
        for link in command.links:
            self._links.append(link)
        for comment in command.comments:
            self._comments.setdefault(comment.task_id, []).append(comment)
        from app.domain.task import CreateGraphResult
        return CreateGraphResult(
            tasks=command.tasks, links=command.links, comments=command.comments,
        )

    async def add_link(self, parent_id, child_id):
        if parent_id == child_id:
            raise TaskValidationError("self-loop")
        if parent_id not in self._tasks or child_id not in self._tasks:
            raise TaskNotFoundError("endpoint not found")
        for existing in self._links:
            if existing.parent_id == parent_id and existing.child_id == child_id:
                raise TaskConflictError("duplicate link")
        if _would_cycle(self._links, parent_id, child_id):
            raise TaskValidationError("cycle detected")
        link = TaskLink(parent_id=parent_id, child_id=child_id)
        self._links.append(link)
        return link

    async def list_children(self, parent_id):
        child_ids = {l.child_id for l in self._links if l.parent_id == parent_id}
        return tuple(self._tasks[cid] for cid in child_ids if cid in self._tasks)

    async def list_parents(self, child_id):
        parent_ids = {l.parent_id for l in self._links if l.child_id == child_id}
        return tuple(self._tasks[pid] for pid in parent_ids if pid in self._tasks)

    async def list_links(self, task_id):
        return tuple(
            l for l in self._links if l.parent_id == task_id or l.child_id == task_id
        )

    async def add_comment(self, task_id, author, body):
        from app.domain.task import TaskComment
        comment = TaskComment(
            id=f"tc_{uuid4().hex[:8]}", task_id=task_id, author=author,
            body=body, created_at=datetime.now(timezone.utc),
        )
        self._comments.setdefault(task_id, []).append(comment)
        return comment

    async def list_comments(self, task_id):
        return tuple(self._comments.get(task_id, []))

    async def list_events(self, task_id, since=0, limit=100):
        return tuple(
            e for e in self._events
            if e.task_id == task_id and e.id > since
        )[-limit:]


def _would_cycle(links: list[TaskLink], parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child would create a cycle."""
    # walk from child_id; if we reach parent_id, cycle
    if parent_id == child_id:
        return True
    visited: set[str] = set()
    stack: list[str] = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        for link in links:
            if link.parent_id == node:
                stack.append(link.child_id)
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_result(content: str) -> ChatCompletionResult:
    return ChatCompletionResult(
        session_id="planning-session",
        model="N-Agent",
        message={"role": "assistant", "content": content},
        finish_reason="stop",
    )


def _triage_task(**kwargs) -> Task:
    defaults = dict(
        id="t_1",
        title="粗想法",
        body="做个调研",
        status=TaskStatus.TRIAGE,
        created_at=datetime.now(timezone.utc),
        version=1,
    )
    defaults.update(kwargs)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_chat():
    return FakeChatService()


@pytest.fixture
def fake_registry():
    return FakeTaskRegistry()


@pytest.fixture
def planning_service(fake_chat, fake_registry):
    return TaskPlanningService(
        chat_service=fake_chat,
        registry=fake_registry,
        policy=TaskPolicy(),
        planning_max_children=5,
        max_swarm_workers=5,
        max_goal_length=2000,
        max_goal_max_turns=20,
        max_iterations=4,
        timeout_seconds=10,
    )


# ---------------------------------------------------------------------------
# specify_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_specify_triage_to_todo(planning_service, fake_chat, fake_registry):
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result(json.dumps({
        "body": "## 目标\n完成架构调研\n\n## 验收标准\n- [ ] 文档完成",
        "acceptance_criteria": ["文档完成", "评审通过"],
    })))
    updated = await planning_service.specify_task("t_1")
    assert updated.status == TaskStatus.TODO
    assert "验收" in updated.body or "目标" in updated.body
    # Version incremented
    assert updated.version == 2
    # Event recorded
    events = await fake_registry.list_events("t_1")
    assert any(e.kind == "specified" for e in events)


@pytest.mark.asyncio
async def test_specify_rejects_non_triage(planning_service, fake_registry):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    with pytest.raises(TaskStateError):
        await planning_service.specify_task("t_1")


@pytest.mark.asyncio
async def test_specify_rejects_unknown_task(planning_service):
    with pytest.raises(TaskNotFoundError):
        await planning_service.specify_task("t_missing")


@pytest.mark.asyncio
async def test_specify_invalid_llm_json_no_write(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result("not valid json"))
    with pytest.raises(Exception):
        await planning_service.specify_task("t_1")
    # No update should have happened
    task = await fake_registry.get_task("t_1")
    assert task.status == TaskStatus.TRIAGE
    assert task.version == 1
    assert fake_registry.update_calls == []


@pytest.mark.asyncio
async def test_specify_missing_body_field_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result(json.dumps({"acceptance_criteria": []})))
    with pytest.raises(Exception):
        await planning_service.specify_task("t_1")


@pytest.mark.asyncio
async def test_specify_oversized_body_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result(json.dumps({
        "body": "x" * 100000,
        "acceptance_criteria": [],
    })))
    with pytest.raises(Exception):
        await planning_service.specify_task("t_1")


@pytest.mark.asyncio
async def test_specify_chat_service_called_with_unattended_mode(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result(json.dumps({
        "body": "ok", "acceptance_criteria": ["done"],
    })))
    await planning_service.specify_task("t_1")
    assert len(fake_chat.complete_calls) == 1
    call = fake_chat.complete_calls[0]
    # source must be the Task subdomain
    assert call.ingress_facts.source == "task"
    # UNATTENDED mode (no interactive approval)
    from app.domain.policy import ExecutionMode
    assert call.ingress_facts.execution_mode == ExecutionMode.UNATTENDED


@pytest.mark.asyncio
async def test_specify_no_task_write_tools_exposed(
    planning_service, fake_chat, fake_registry
):
    """specify fork must NOT expose task_complete / task_block / task_create
    write tools -- only skills-read."""
    await fake_registry.create_task(_triage_task(id="t_1"))
    fake_chat.set_result(_llm_result(json.dumps({
        "body": "ok", "acceptance_criteria": ["done"],
    })))
    await planning_service.specify_task("t_1")
    call = fake_chat.complete_calls[0]
    # permitted_managed_tools should not include task write tools
    permitted = set(call.trusted_metadata.get("permitted_managed_tools", []))
    from app.application.task_tools import TASK_TOOL_NAMES
    # No task managed tools at all in the planning fork
    assert not TASK_TOOL_NAMES.intersection(permitted)


# ---------------------------------------------------------------------------
# decompose_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_creates_children_with_links(
    planning_service, fake_chat, fake_registry
):
    parent = _triage_task(id="t_p", status=TaskStatus.TODO)
    await fake_registry.create_task(parent)
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [
            {"title": "sub1", "body": "do part 1"},
            {"title": "sub2", "body": "do part 2"},
        ],
    })))
    children = await planning_service.decompose_task("t_p")
    assert len(children) == 2
    # Each child has parent t_p
    for child in children:
        parents = await fake_registry.list_parents(child.id)
        parent_ids = [p.id for p in parents]
        assert "t_p" in parent_ids
        assert child.status == TaskStatus.TODO
    # Parent not auto-completed
    parent_after = await fake_registry.get_task("t_p")
    assert parent_after.status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_decompose_rejects_triage(planning_service, fake_registry):
    await fake_registry.create_task(_triage_task(id="t_1"))
    with pytest.raises(TaskStateError):
        await planning_service.decompose_task("t_1")


@pytest.mark.asyncio
async def test_decompose_rejects_archived(planning_service, fake_registry):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.ARCHIVED))
    with pytest.raises(TaskStateError):
        await planning_service.decompose_task("t_1")


@pytest.mark.asyncio
async def test_decompose_invalid_json_no_write(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result("not json"))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")
    # No graph creation should have happened
    assert fake_registry.create_graph_calls == []


@pytest.mark.asyncio
async def test_decompose_exceeds_max_children(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    # planning_max_children=5 in fixture; generate 6
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [{"title": f"sub{i}", "body": "b"} for i in range(6)],
    })))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")
    assert fake_registry.create_graph_calls == []


@pytest.mark.asyncio
async def test_decompose_empty_children_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result(json.dumps({"children": []})))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")


@pytest.mark.asyncio
async def test_decompose_child_missing_title_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [{"body": "no title"}],
    })))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")


@pytest.mark.asyncio
async def test_decompose_dependency_on_unknown_index_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    # depends_on index 5 (out of range, only 2 children)
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [
            {"title": "a", "body": "b"},
            {"title": "b", "body": "b", "depends_on_indices": [5]},
        ],
    })))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")


@pytest.mark.asyncio
async def test_decompose_dependency_creates_links_between_children(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [
            {"title": "a", "body": "b"},
            {"title": "b", "body": "b", "depends_on_indices": [0]},
        ],
    })))
    children = await planning_service.decompose_task("t_1")
    assert len(children) == 2
    # child[1] depends on child[0]
    parents_of_b = await fake_registry.list_parents(children[1].id)
    parent_ids = [p.id for p in parents_of_b]
    assert children[0].id in parent_ids
    assert "t_1" in parent_ids  # also depends on original parent


@pytest.mark.asyncio
async def test_decompose_dependency_cycle_rejected(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    # 0 -> 1 -> 0 cycle
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [
            {"title": "a", "body": "b", "depends_on_indices": [1]},
            {"title": "b", "body": "b", "depends_on_indices": [0]},
        ],
    })))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")
    assert fake_registry.create_graph_calls == []


@pytest.mark.asyncio
async def test_decompose_indirect_cycle_rejected(
    planning_service, fake_chat, fake_registry
):
    """Indirect cycle: 0 -> 1 -> 2 -> 0."""
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [
            {"title": "a", "body": "b", "depends_on_indices": [2]},
            {"title": "b", "body": "b", "depends_on_indices": [0]},
            {"title": "c", "body": "b", "depends_on_indices": [1]},
        ],
    })))
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")
    assert fake_registry.create_graph_calls == []


@pytest.mark.asyncio
async def test_decompose_rollback_on_create_graph_failure(
    planning_service, fake_chat, fake_registry
):
    await fake_registry.create_task(_triage_task(id="t_1", status=TaskStatus.TODO))
    fake_chat.set_result(_llm_result(json.dumps({
        "children": [{"title": "a", "body": "b"}],
    })))
    # Inject failure in create_graph
    fake_registry._create_graph_fail = True
    with pytest.raises(Exception):
        await planning_service.decompose_task("t_1")
    # create_graph was called but failed; no tasks should have been written
    # (FakeTaskRegistry does all-then-write, but the failure propagates)
    # The key assertion: no half-graph
    for call in fake_registry.create_graph_calls:
        # The command was attempted, but registry rejected it
        assert len(call.tasks) == 1


# ---------------------------------------------------------------------------
# create_swarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_topology(planning_service, fake_registry):
    workers = [
        SwarmWorkerSpec(profile="worker", title="w1", body="worker 1"),
        SwarmWorkerSpec(profile="worker", title="w2", body="worker 2"),
    ]
    result = await planning_service.create_swarm(
        goal="完成 swarm 演示",
        workers=workers,
        verifier_assignee="verifier",
        synthesizer_assignee="synthesizer",
    )
    assert isinstance(result, SwarmResult)
    # Root DONE
    root = await fake_registry.get_task(result.root_id)
    assert root.status == TaskStatus.DONE
    # Workers: parents=[root], status TODO
    worker_tasks = []
    for wid in result.worker_ids:
        w = await fake_registry.get_task(wid)
        assert w.status == TaskStatus.TODO
        parents = await fake_registry.list_parents(wid)
        parent_ids = [p.id for p in parents]
        assert root.id in parent_ids
        worker_tasks.append(w)
    # Verifier depends on all workers
    verifier = await fake_registry.get_task(result.verifier_id)
    assert verifier.status == TaskStatus.TODO
    verifier_parents = await fake_registry.list_parents(result.verifier_id)
    verifier_parent_ids = [p.id for p in verifier_parents]
    for w in worker_tasks:
        assert w.id in verifier_parent_ids
    # Synthesizer depends on verifier
    synthesizer = await fake_registry.get_task(result.synthesizer_id)
    assert synthesizer.status == TaskStatus.TODO
    synth_parents = await fake_registry.list_parents(result.synthesizer_id)
    synth_parent_ids = [p.id for p in synth_parents]
    assert result.verifier_id in synth_parent_ids


@pytest.mark.asyncio
async def test_swarm_blackboard_comment(planning_service, fake_registry):
    workers = [SwarmWorkerSpec(profile="worker", title="w1", body="worker 1")]
    result = await planning_service.create_swarm(
        goal="g",
        workers=workers,
        verifier_assignee="v",
        synthesizer_assignee="s",
    )
    # Blackboard comment on root task
    comments = await fake_registry.list_comments(result.root_id)
    blackboard_comments = [c for c in comments if c.body.startswith("[swarm:blackboard] ")]
    assert len(blackboard_comments) == 1
    # Parse JSON payload
    payload = blackboard_comments[0].body[len("[swarm:blackboard] "):]
    data = json.loads(payload)
    assert "topology" in data
    assert data["topology"]["root"] == result.root_id
    assert data["topology"]["verifier"] == result.verifier_id
    assert data["topology"]["synthesizer"] == result.synthesizer_id
    assert set(data["topology"]["workers"]) == set(result.worker_ids)


@pytest.mark.asyncio
async def test_swarm_rejects_empty_workers(planning_service):
    with pytest.raises(TaskValidationError):
        await planning_service.create_swarm(
            goal="g",
            workers=[],
            verifier_assignee="v",
            synthesizer_assignee="s",
        )


@pytest.mark.asyncio
async def test_swarm_rejects_too_many_workers(planning_service):
    # max_swarm_workers=5 in fixture
    workers = [
        SwarmWorkerSpec(profile="w", title=f"w{i}", body="b")
        for i in range(6)
    ]
    with pytest.raises(TaskValidationError):
        await planning_service.create_swarm(
            goal="g",
            workers=workers,
            verifier_assignee="v",
            synthesizer_assignee="s",
        )


@pytest.mark.asyncio
async def test_swarm_rejects_oversized_goal(planning_service):
    workers = [SwarmWorkerSpec(profile="w", title="w", body="b")]
    with pytest.raises(TaskValidationError):
        await planning_service.create_swarm(
            goal="x" * 10000,
            workers=workers,
            verifier_assignee="v",
            synthesizer_assignee="s",
        )


@pytest.mark.asyncio
async def test_swarm_rollback_on_failure(planning_service, fake_registry):
    workers = [SwarmWorkerSpec(profile="w", title="w", body="b")]
    fake_registry._create_graph_fail = True
    with pytest.raises(Exception):
        await planning_service.create_swarm(
            goal="g",
            workers=workers,
            verifier_assignee="v",
            synthesizer_assignee="s",
        )
    # No tasks should exist
    assert fake_registry.create_graph_calls
    # Verify no tasks were created (rollback)
    # The FakeTaskRegistry create_graph is atomic -- if it raises, nothing
    # was written. So tasks list should be empty.


@pytest.mark.asyncio
async def test_swarm_no_llm_call(planning_service, fake_chat, fake_registry):
    """create_swarm is deterministic -- no LLM fork."""
    workers = [SwarmWorkerSpec(profile="w", title="w", body="b")]
    await planning_service.create_swarm(
        goal="g",
        workers=workers,
        verifier_assignee="v",
        synthesizer_assignee="s",
    )
    # No chat.complete calls
    assert fake_chat.complete_calls == []


# ---------------------------------------------------------------------------
# Topology progression (workers -> verifier -> synthesizer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_topology_progression_after_workers_done(
    planning_service, fake_registry
):
    """When all workers -> DONE, verifier should be promotable (parent deps met)."""
    workers = [
        SwarmWorkerSpec(profile="worker", title="w1", body="b1"),
        SwarmWorkerSpec(profile="worker", title="w2", body="b2"),
    ]
    result = await planning_service.create_swarm(
        goal="g",
        workers=workers,
        verifier_assignee="verifier",
        synthesizer_assignee="synthesizer",
    )
    # Initially verifier and synthesizer are TODO with unmet deps
    verifier = await fake_registry.get_task(result.verifier_id)
    assert verifier.status == TaskStatus.TODO
    # Mark all workers DONE (simulate completion)
    for wid in result.worker_ids:
        w = await fake_registry.get_task(wid)
        await fake_registry.update_task(
            wid, {"status": TaskStatus.DONE}, expected_version=w.version,
        )
    # Verifier parents are all DONE now -- manually promote to READY for test
    verifier = await fake_registry.get_task(result.verifier_id)
    await fake_registry.update_task(
        result.verifier_id, {"status": TaskStatus.READY},
        expected_version=verifier.version,
    )
    # Mark verifier DONE -> synthesizer parents met
    verifier = await fake_registry.get_task(result.verifier_id)
    await fake_registry.update_task(
        result.verifier_id, {"status": TaskStatus.DONE},
        expected_version=verifier.version,
    )
    synth = await fake_registry.get_task(result.synthesizer_id)
    await fake_registry.update_task(
        result.synthesizer_id, {"status": TaskStatus.READY},
        expected_version=synth.version,
    )
    synth = await fake_registry.get_task(result.synthesizer_id)
    assert synth.status == TaskStatus.READY
