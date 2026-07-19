"""Domain Task aggregate, value objects, ports, and state transition contract tests.

Covers plan T1 (aggregate + value objects + exceptions), T2 (state transition
contract + claim CAS + retry/archive), and T5 (async Protocol ports).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.task import (
    BlockKind,
    Task,
    TaskAttachment,
    TaskArtifact,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskDispatcher,
    TaskEvent,
    TaskAttachmentError,
    TaskExecutionPolicy,
    TaskLink,
    TaskNotFoundError,
    TaskNotifier,
    TaskRegistry,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)


# ---------------------------------------------------------------------------
# T1 S1: enums + Task basic fields
# ---------------------------------------------------------------------------


def test_task_status_enum():
    assert TaskStatus.TRIAGE.value == "triage"
    assert TaskStatus.DONE.value == "done"
    assert TaskStatus.ARCHIVED.value == "archived"
    # Full enum coverage per spec
    expected = {
        "triage",
        "todo",
        "scheduled",
        "ready",
        "running",
        "blocked",
        "review",
        "done",
        "archived",
    }
    assert {s.value for s in TaskStatus} == expected


def test_block_kind_enum():
    expected = {"dependency", "needs_input", "capability", "transient"}
    assert {b.value for b in BlockKind} == expected


def test_task_workspace_kind_enum():
    expected = {"scratch", "dir"}
    assert {w.value for w in TaskWorkspaceKind} == expected
    # WORKTREE not accepted per spec
    assert not hasattr(TaskWorkspaceKind, "WORKTREE")


def test_task_run_status_enum():
    expected = {
        "running",
        "completed",
        "blocked",
        "failed",
        "crashed",
        "timed_out",
        "terminated",
        "reclaimed",
    }
    assert {s.value for s in TaskRunStatus} == expected


def test_task_run_outcome_enum():
    expected = {
        "completed",
        "blocked",
        "failed",
        "crashed",
        "timed_out",
        "terminated",
        "spawn_failed",
        "gave_up",
        "reclaimed",
    }
    assert {o.value for o in TaskRunOutcome} == expected


def test_task_creation_minimal():
    t = Task(id="t_abc", title="调研 N-Agent 架构")
    assert t.status == TaskStatus.TRIAGE
    assert t.priority == 0
    assert t.board == "default"
    assert t.consecutive_failures == 0
    assert t.origin_session_id is None
    assert t.execution_session_id is None
    assert t.version == 1
    assert t.max_retries == 0
    assert t.goal_mode is False
    assert t.workspace_kind == TaskWorkspaceKind.SCRATCH
    assert t.execution_policy.allowed_tools == ()
    assert t.skills == ()
    assert t.block_recurrences == 0


def test_task_is_frozen():
    t = Task(id="t_1", title="x")
    with pytest.raises(Exception):
        t.title = "y"  # type: ignore[misc]


def test_task_execution_policy_default():
    p = TaskExecutionPolicy()
    assert p.allowed_tools == ()


def test_task_execution_policy_frozen():
    p = TaskExecutionPolicy(allowed_tools=("foo", "bar"))
    assert p.allowed_tools == ("foo", "bar")
    with pytest.raises(Exception):
        p.allowed_tools = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T1 S5: value objects (TaskRun/TaskLink/TaskComment/TaskEvent/TaskAttachment/TaskArtifact)
# ---------------------------------------------------------------------------


@pytest.fixture
def task_run_factory():
    def _factory(task_id="t_abc", **overrides):
        defaults = dict(
            id="run_1",
            task_id=task_id,
            status=TaskRunStatus.RUNNING,
            claim_lock="lock-xyz",
        )
        defaults.update(overrides)
        return TaskRun(**defaults)

    return _factory


def test_task_run_fields(task_run_factory):
    r = task_run_factory(task_id="t_abc")
    assert r.status == TaskRunStatus.RUNNING
    assert r.metadata == {}
    assert r.outcome is None
    assert r.summary is None
    assert r.error is None
    assert r.lease_seconds is None


def test_task_run_lease_seconds_field():
    r = TaskRun(id=1, task_id="t_1", lease_seconds=300)
    assert r.lease_seconds == 300


def test_task_run_is_frozen(task_run_factory):
    r = task_run_factory()
    with pytest.raises(Exception):
        r.status = TaskRunStatus.COMPLETED  # type: ignore[misc]


def test_task_link_fields():
    link = TaskLink(parent_id="t_p", child_id="t_c")
    assert link.parent_id == "t_p"
    assert link.child_id == "t_c"


def test_task_comment_fields():
    c = TaskComment(id="c1", task_id="t_1", author="worker", body="hi")
    assert c.author == "worker"
    assert c.body == "hi"


def test_task_event_fields():
    e = TaskEvent(id=1, task_id="t_1", kind="created", payload={"k": "v"})
    assert e.kind == "created"
    assert e.payload == {"k": "v"}
    assert e.run_id is None


def test_task_attachment_fields():
    a = TaskAttachment(
        id="a1",
        task_id="t_1",
        filename="report.md",
        stored_name="server-generated",
        content_type="text/markdown",
        size=10,
        checksum="sha256:abc",
        uploaded_by="u",
    )
    assert a.stored_name == "server-generated"
    assert a.size == 10


def test_task_artifact_checksum():
    a = TaskArtifact(
        type="file",
        name="report.md",
        mime="text/markdown",
        size=100,
        storage_ref="tasks/t_abc/report.md",
        source_task_id="t_abc",
        summary="调研报告",
        checksum="sha256:abc",
    )
    assert a.source_task_id == "t_abc"
    assert a.checksum == "sha256:abc"


# ---------------------------------------------------------------------------
# FinishRunCommand: target_task_status override (Concern A)
# ---------------------------------------------------------------------------


def test_finish_run_command_target_task_status_default_none():
    from app.domain.task import FinishRunCommand

    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=1,
        claim_lock="L",
        outcome=TaskRunOutcome.COMPLETED,
    )
    assert cmd.target_task_status is None


def test_finish_run_command_target_task_status_override():
    from app.domain.task import FinishRunCommand

    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=1,
        claim_lock="L",
        outcome=TaskRunOutcome.COMPLETED,
        target_task_status=TaskStatus.REVIEW,
    )
    assert cmd.target_task_status == TaskStatus.REVIEW


# ---------------------------------------------------------------------------
# T1 S8: exception classes
# ---------------------------------------------------------------------------


def test_task_exceptions():
    assert issubclass(TaskNotFoundError, Exception)
    assert issubclass(TaskValidationError, Exception)
    assert issubclass(TaskClaimError, Exception)
    assert issubclass(TaskStateError, Exception)
    assert issubclass(TaskConflictError, Exception)
    assert issubclass(TaskAttachmentError, Exception)


# ---------------------------------------------------------------------------
# T2 S1: state transition contract
# ---------------------------------------------------------------------------


def test_state_transition_legal():
    t = Task(id="t_1", title="x", status=TaskStatus.TRIAGE)
    assert t.can_transition_to(TaskStatus.TODO) is True
    assert t.can_transition_to(TaskStatus.RUNNING) is False  # TRIAGE 不能直接 RUNNING


def test_ready_requires_assignee_and_deps():
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.TODO, assignee="default")
    assert t.can_promote_to_ready(parents_done=True, now=now) is True
    t2 = Task(id="t_2", title="y", status=TaskStatus.TODO)  # 无 assignee
    assert t2.can_promote_to_ready(parents_done=True, now=now) is False


def test_block_kind_routing():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
    t = t.block(BlockKind.DEPENDENCY, "wait parent")
    assert t.status == TaskStatus.TODO
    t3 = Task(id="t_3", title="z", status=TaskStatus.RUNNING)
    t3 = t3.block(BlockKind.NEEDS_INPUT, "need user")
    assert t3.status == TaskStatus.BLOCKED


# ---------------------------------------------------------------------------
# T2 S5: claim CAS + stale detection
# ---------------------------------------------------------------------------


def test_claim_sets_lock():
    expires_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.READY, assignee="default")
    t = t.claim(run_id=1, claim_lock="lock-xyz", expires_at=expires_at)
    assert t.status == TaskStatus.RUNNING
    assert t.claim_lock == "lock-xyz"
    assert t.current_run_id == 1
    assert t.claim_expires == expires_at


def test_claim_requires_ready():
    expires_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.TODO, assignee="default")
    with pytest.raises(TaskStateError):
        t.claim(run_id=1, claim_lock="L", expires_at=expires_at)


def test_release_claim_wrong_token_raises():
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="lock-xyz",
        current_run_id=1,
    )
    with pytest.raises(TaskClaimError):
        t.release_claim("wrong-token")


def test_release_claim_correct_token():
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="lock-xyz",
        current_run_id=1,
    )
    t = t.release_claim("lock-xyz")
    assert t.claim_lock is None
    assert t.current_run_id is None
    assert t.status == TaskStatus.RUNNING  # release_claim only clears lock, not status


def test_is_stale_heartbeat_timeout():
    base = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="l",
        last_heartbeat_at=base,
    )
    # now > 300s past heartbeat -> stale
    assert t.is_stale(now=base + timedelta(seconds=500), heartbeat_timeout=300) is True
    # now within 300s of heartbeat -> not stale
    assert t.is_stale(now=base + timedelta(seconds=200), heartbeat_timeout=300) is False


def test_is_stale_no_heartbeat():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING, claim_lock="l")
    assert t.is_stale(now=now, heartbeat_timeout=300) is False


def test_is_stale_not_running():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.READY,
        last_heartbeat_at=now,
    )
    assert t.is_stale(now=now, heartbeat_timeout=300) is False


# ---------------------------------------------------------------------------
# T2 S8: comprehensive transition / retry / archive contract
# ---------------------------------------------------------------------------


# Allowed edges per spec state transition table
ALLOWED_TRANSITIONS = {
    TaskStatus.TRIAGE: {TaskStatus.TODO, TaskStatus.ARCHIVED},
    TaskStatus.TODO: {
        TaskStatus.SCHEDULED,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.ARCHIVED,
    },
    TaskStatus.SCHEDULED: {TaskStatus.READY, TaskStatus.TODO, TaskStatus.ARCHIVED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.TODO, TaskStatus.BLOCKED, TaskStatus.ARCHIVED},
    TaskStatus.RUNNING: {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.TODO, TaskStatus.BLOCKED},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.TODO, TaskStatus.BLOCKED, TaskStatus.ARCHIVED},
    TaskStatus.BLOCKED: {TaskStatus.TODO, TaskStatus.ARCHIVED},
    TaskStatus.DONE: {TaskStatus.REVIEW, TaskStatus.ARCHIVED},
    TaskStatus.ARCHIVED: set(),  # unarchive is explicit domain op, not generic transition
}


@pytest.mark.parametrize(
    "current,target",
    [
        (current, target)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    ],
)
def test_state_transition_allowed_edges(current, target):
    t = Task(id="t_1", title="x", status=current, assignee="default")
    assert t.can_transition_to(target) is True


@pytest.mark.parametrize(
    "current,target",
    [
        (TaskStatus.TRIAGE, TaskStatus.RUNNING),
        (TaskStatus.TRIAGE, TaskStatus.DONE),
        (TaskStatus.TODO, TaskStatus.RUNNING),  # must go through READY via claim
        (TaskStatus.TODO, TaskStatus.REVIEW),
        (TaskStatus.READY, TaskStatus.DONE),
        (TaskStatus.READY, TaskStatus.REVIEW),
        (TaskStatus.RUNNING, TaskStatus.READY),
        (TaskStatus.RUNNING, TaskStatus.SCHEDULED),
        (TaskStatus.RUNNING, TaskStatus.TRIAGE),
        (TaskStatus.BLOCKED, TaskStatus.RUNNING),
        (TaskStatus.BLOCKED, TaskStatus.READY),
        (TaskStatus.BLOCKED, TaskStatus.DONE),
        (TaskStatus.DONE, TaskStatus.TODO),
        (TaskStatus.DONE, TaskStatus.RUNNING),
        (TaskStatus.ARCHIVED, TaskStatus.TODO),  # unarchive is explicit op
        (TaskStatus.ARCHIVED, TaskStatus.RUNNING),
    ],
)
def test_state_transition_disallowed_edges(current, target):
    t = Task(id="t_1", title="x", status=current, assignee="default")
    assert t.can_transition_to(target) is False


def test_can_promote_to_ready_requires_parents_done():
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.TODO, assignee="default")
    assert t.can_promote_to_ready(parents_done=False, now=now) is False
    assert t.can_promote_to_ready(parents_done=True, now=now) is True


def test_can_promote_to_ready_scheduled_at_must_be_due():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    # scheduled_at in future -> not ready
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.TODO,
        assignee="default",
        scheduled_at=now + timedelta(seconds=500),
    )
    assert t.can_promote_to_ready(parents_done=True, now=now) is False
    # scheduled_at in past -> ready
    t_past = Task(
        id="t_1b",
        title="x",
        status=TaskStatus.TODO,
        assignee="default",
        scheduled_at=now - timedelta(seconds=500),
    )
    assert t_past.can_promote_to_ready(parents_done=True, now=now) is True
    # scheduled_at None -> ready
    t2 = Task(id="t_2", title="y", status=TaskStatus.TODO, assignee="d")
    assert t2.can_promote_to_ready(parents_done=True, now=now) is True


def test_can_promote_to_ready_wrong_status():
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.TRIAGE, assignee="default")
    assert t.can_promote_to_ready(parents_done=True, now=now) is False


def test_block_increments_recurrences_for_blocked_kind():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
    t = t.block(BlockKind.NEEDS_INPUT, "need user")
    assert t.status == TaskStatus.BLOCKED
    assert t.block_kind == BlockKind.NEEDS_INPUT
    assert t.block_reason == "need user"
    assert t.block_recurrences == 1
    # block again increments
    t = t.block(BlockKind.TRANSIENT, "again")
    assert t.block_recurrences == 2


def test_block_dependency_does_not_increment_recurrences():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
    t = t.block(BlockKind.DEPENDENCY, "wait parent")
    assert t.status == TaskStatus.TODO
    assert t.block_kind == BlockKind.DEPENDENCY
    # DEPENDENCY routes to TODO, not a blocked-loop recurrence
    assert t.block_recurrences == 0


def test_record_failure_increments_consecutive():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
    t = t.record_failure(error="boom")
    assert t.consecutive_failures == 1
    assert t.last_failure_error == "boom"
    t = t.record_failure(error="boom2")
    assert t.consecutive_failures == 2


def test_should_give_up_when_exceeds_max_retries():
    # max_retries=0: first failure -> GAVE_UP (1 > 0)
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING, max_retries=0)
    t = t.record_failure(error="boom")
    assert t.consecutive_failures == 1
    assert t.should_give_up() is True

    # max_retries=2: allow 2 retries (3 failures total -> 3 > 2 -> GAVE_UP)
    t2 = Task(id="t_2", title="y", status=TaskStatus.RUNNING, max_retries=2)
    t2 = t2.record_failure(error="e1")
    assert t2.should_give_up() is False  # 1 <= 2
    t2 = t2.record_failure(error="e2")
    assert t2.should_give_up() is False  # 2 <= 2
    t2 = t2.record_failure(error="e3")
    assert t2.should_give_up() is True  # 3 > 2


def test_complete_clears_failures_and_sets_done():
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        consecutive_failures=3,
        claim_lock="L",
        current_run_id=1,
    )
    t = t.complete(summary="done")
    assert t.status == TaskStatus.DONE
    assert t.consecutive_failures == 0
    assert t.result == "done"


def test_record_heartbeat_updates_timestamp():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="L",
        current_run_id=1,
    )
    t = t.record_heartbeat(now=now, claim_lock="L")
    assert t.last_heartbeat_at == now


def test_record_heartbeat_wrong_token_raises():
    now = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="L",
        current_run_id=1,
    )
    with pytest.raises(TaskClaimError):
        t.record_heartbeat(now=now, claim_lock="WRONG")


def test_archive_saves_pre_archive_status():
    t = Task(id="t_1", title="x", status=TaskStatus.DONE)
    t = t.archive()
    assert t.status == TaskStatus.ARCHIVED
    assert t.pre_archive_status == TaskStatus.DONE


def test_unarchive_restores_pre_archive_status():
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.ARCHIVED,
        pre_archive_status=TaskStatus.DONE,
    )
    t = t.unarchive()
    assert t.status == TaskStatus.DONE
    assert t.pre_archive_status is None


def test_unarchive_defaults_to_todo_when_no_pre_archive():
    t = Task(id="t_1", title="x", status=TaskStatus.ARCHIVED)
    t = t.unarchive()
    assert t.status == TaskStatus.TODO


def test_archive_not_allowed_from_running():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING, claim_lock="L")
    # RUNNING -> ARCHIVED not in allowed transitions
    assert t.can_transition_to(TaskStatus.ARCHIVED) is False


def test_archive_rejects_running_task():
    """Spec: RUNNING Task 禁止 archive; terminate 后才可归档."""
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING, claim_lock="L")
    with pytest.raises(TaskStateError):
        t.archive()


def test_archive_allows_non_running_statuses():
    """archive() should accept all non-RUNNING statuses (explicit domain op)."""
    for status in (
        TaskStatus.TRIAGE,
        TaskStatus.TODO,
        TaskStatus.SCHEDULED,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.REVIEW,
        TaskStatus.DONE,
    ):
        t = Task(id="t_1", title="x", status=status)
        archived = t.archive()
        assert archived.status == TaskStatus.ARCHIVED
        assert archived.pre_archive_status == status


# ---------------------------------------------------------------------------
# T5 S1: async Protocol ports
# ---------------------------------------------------------------------------


def test_task_registry_protocol():
    assert hasattr(TaskRegistry, "create_task")
    assert hasattr(TaskRegistry, "claim_task")
    assert hasattr(TaskRegistry, "list_ready")


def test_task_dispatcher_protocol():
    assert hasattr(TaskDispatcher, "spawn")
    assert hasattr(TaskDispatcher, "cancel")
    assert hasattr(TaskDispatcher, "inspect")


def test_task_notifier_protocol():
    assert hasattr(TaskNotifier, "deliver")


def test_task_registry_has_full_method_set():
    """Verify TaskRegistry protocol exposes all spec-mandated methods."""
    expected_methods = {
        "create_task",
        "get_task",
        "list_tasks",
        "update_task",
        "bulk_update",
        "delete_task",
        "claim_task",
        "record_heartbeat",
        "finish_run",
        "recover_run",
        "list_ready",
        "list_running",
        "recompute_ready",
        "create_graph",
        "add_link",
        "remove_link",
        "list_links",
        "list_children",
        "list_parents",
        "add_comment",
        "list_comments",
        "append_event",
        "list_events",
        "list_runs",
        "add_attachment",
        "list_attachments",
        "get_attachment",
        "delete_attachment",
        "subscribe_notify",
        "list_notify_subs",
        "unsubscribe_notify",
    }
    missing = expected_methods - set(dir(TaskRegistry))
    assert not missing, f"TaskRegistry missing methods: {missing}"
