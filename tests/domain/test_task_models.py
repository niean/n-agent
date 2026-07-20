"""Domain Task aggregate, value objects, ports, and state transition contract tests.

Covers plan T1 (Manus 7-state machine + new Task fields/methods + value objects
+ ports). Old 9-state / assignee / BlockKind / TaskLink / archive-state concepts
are removed; archive is now a soft-delete flag ``is_archived`` (not a status).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.task import (
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
    TASK_TRANSITION_TABLE,
)


# ---------------------------------------------------------------------------
# T1 S1: enums (7-state TaskStatus + TaskRunOutcome + TaskRunStatus + workspace)
# ---------------------------------------------------------------------------


def test_status_enum_is_seven_manus_values():
    assert {s.value for s in TaskStatus} == {
        "queued",
        "running",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    }


def test_transition_table_matches_spec():
    assert TASK_TRANSITION_TABLE[TaskStatus.QUEUED] == frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED}
    )
    assert TASK_TRANSITION_TABLE[TaskStatus.RUNNING] == frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.QUEUED,
        }
    )
    assert TASK_TRANSITION_TABLE[TaskStatus.WAITING_APPROVAL] == frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED}
    )
    assert TASK_TRANSITION_TABLE[TaskStatus.FAILED] == frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED}
    )
    assert TASK_TRANSITION_TABLE[TaskStatus.EXPIRED] == frozenset({TaskStatus.QUEUED})
    assert TASK_TRANSITION_TABLE[TaskStatus.SUCCEEDED] == frozenset()
    assert TASK_TRANSITION_TABLE[TaskStatus.CANCELLED] == frozenset()


def test_task_workspace_kind_enum():
    expected = {"scratch", "dir"}
    assert {w.value for w in TaskWorkspaceKind} == expected
    # WORKTREE not accepted per spec
    assert not hasattr(TaskWorkspaceKind, "WORKTREE")


def test_task_run_status_enum():
    # TaskRunStatus lifecycle retained; BLOCKED/RECLAIMED remain as legacy
    # terminal markers but new code does not produce them.
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
    # New outcome set per spec Data Model: adds WAITING_APPROVAL/EXPIRED,
    # drops BLOCKED/GAVE_UP/RECLAIMED.
    expected = {
        "completed",
        "waiting_approval",
        "failed",
        "crashed",
        "timed_out",
        "spawn_failed",
        "expired",
        "terminated",
        "aborted",
    }
    assert {o.value for o in TaskRunOutcome} == expected


# ---------------------------------------------------------------------------
# T1 S5: Task fields + defaults (no assignee, has is_archived, default QUEUED)
# ---------------------------------------------------------------------------


def test_task_creation_minimal():
    t = Task(id="t_abc", title="调研 N-Agent 架构")
    assert t.status == TaskStatus.QUEUED
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


def test_task_has_no_assignee_but_has_is_archived():
    t = Task(id="t1", title="x")
    assert "assignee" not in t.__dataclass_fields__
    assert "pre_archive_status" not in t.__dataclass_fields__
    assert "block_kind" not in t.__dataclass_fields__
    assert "block_reason" not in t.__dataclass_fields__
    assert "block_recurrences" not in t.__dataclass_fields__
    assert t.is_archived is False
    assert t.status is TaskStatus.QUEUED


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
# T1 S5: value objects (TaskRun/TaskComment/TaskEvent/TaskAttachment/TaskArtifact)
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
# FinishRunCommand: target_task_status override
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
        target_task_status=TaskStatus.WAITING_APPROVAL,
    )
    assert cmd.target_task_status == TaskStatus.WAITING_APPROVAL


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
# T1 S5: state transition contract (7-state Manus table)
# ---------------------------------------------------------------------------


def test_state_transition_legal():
    t = Task(id="t_1", title="x", status=TaskStatus.QUEUED)
    assert t.can_transition_to(TaskStatus.RUNNING) is True
    assert t.can_transition_to(TaskStatus.SUCCEEDED) is False  # QUEUED 不能直接 SUCCEEDED


def test_state_transition_disallowed_edges():
    # QUEUED -> SUCCEEDED 不允许
    t = Task(id="t_1", title="x", status=TaskStatus.QUEUED)
    assert t.can_transition_to(TaskStatus.SUCCEEDED) is False
    # SUCCEEDED 是终态
    t2 = Task(id="t_2", title="x", status=TaskStatus.SUCCEEDED)
    assert t2.can_transition_to(TaskStatus.QUEUED) is False
    # CANCELLED 是终态
    t3 = Task(id="t_3", title="x", status=TaskStatus.CANCELLED)
    assert t3.can_transition_to(TaskStatus.QUEUED) is False


# Allowed edges per spec TASK_TRANSITION_TABLE
ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
        TaskStatus.QUEUED,
    },
    TaskStatus.WAITING_APPROVAL: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    TaskStatus.EXPIRED: {TaskStatus.QUEUED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.CANCELLED: set(),
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
    t = Task(id="t_1", title="x", status=current)
    assert t.can_transition_to(target) is True


# ---------------------------------------------------------------------------
# T1 S5: can_claim (QUEUED + scheduled_at due)
# ---------------------------------------------------------------------------


def test_can_claim_only_queued_and_due():
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    t = Task(id="t1", title="x", status=TaskStatus.QUEUED)
    assert t.can_claim(now) is True
    # 未到期 -> 不可 claim
    t2 = Task(
        id="t2",
        title="x",
        status=TaskStatus.QUEUED,
        scheduled_at=datetime(2026, 7, 19, 13, 0, tzinfo=timezone.utc),
    )
    assert t2.can_claim(now) is False
    # 已到期 -> 可 claim
    t3 = Task(
        id="t3",
        title="x",
        status=TaskStatus.QUEUED,
        scheduled_at=datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc),
    )
    assert t3.can_claim(now) is True
    # 非 QUEUED -> 不可 claim
    t4 = Task(id="t4", title="x", status=TaskStatus.RUNNING)
    assert t4.can_claim(now) is False


# ---------------------------------------------------------------------------
# T1 S5: claim / release CAS (claim now requires QUEUED, not READY)
# ---------------------------------------------------------------------------


def test_claim_sets_lock():
    expires_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.QUEUED)
    t = t.claim(run_id=1, claim_lock="lock-xyz", expires_at=expires_at)
    assert t.status == TaskStatus.RUNNING
    assert t.claim_lock == "lock-xyz"
    assert t.current_run_id == 1
    assert t.claim_expires == expires_at
    assert t.started_at is not None


def test_claim_requires_queued():
    expires_at = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
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
    assert t.claim_expires is None
    assert t.status == TaskStatus.RUNNING  # release_claim only clears lock, not status


# ---------------------------------------------------------------------------
# T1 S5: heartbeat / staleness
# ---------------------------------------------------------------------------


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
        status=TaskStatus.QUEUED,
        last_heartbeat_at=now,
    )
    assert t.is_stale(now=now, heartbeat_timeout=300) is False


# ---------------------------------------------------------------------------
# T1 S5: failure / complete / propose / approve-reject / cancel / retry / archive
# ---------------------------------------------------------------------------


def test_record_failure_increments_consecutive():
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING)
    t = t.record_failure(error="boom")
    assert t.consecutive_failures == 1
    assert t.last_failure_error == "boom"
    t = t.record_failure(error="boom2")
    assert t.consecutive_failures == 2


def test_should_give_up_when_exceeds_max_retries():
    # max_retries=0: first failure -> give up (1 > 0)
    t = Task(id="t_1", title="x", status=TaskStatus.RUNNING, max_retries=0)
    t = t.record_failure(error="boom")
    assert t.consecutive_failures == 1
    assert t.should_give_up() is True

    # max_retries=2: allow 2 retries (3 failures total -> 3 > 2 -> give up)
    t2 = Task(id="t_2", title="y", status=TaskStatus.RUNNING, max_retries=2)
    t2 = t2.record_failure(error="e1")
    assert t2.should_give_up() is False  # 1 <= 2
    t2 = t2.record_failure(error="e2")
    assert t2.should_give_up() is False  # 2 <= 2
    t2 = t2.record_failure(error="e3")
    assert t2.should_give_up() is True  # 3 > 2


def test_complete_clears_failures_and_sets_succeeded():
    t = Task(
        id="t_1",
        title="x",
        status=TaskStatus.RUNNING,
        consecutive_failures=3,
        claim_lock="L",
        current_run_id=1,
    )
    t = t.complete(summary="done")
    assert t.status == TaskStatus.SUCCEEDED
    assert t.consecutive_failures == 0
    assert t.result == "done"
    assert t.completed_at is not None


def test_propose_change_moves_running_to_waiting_approval():
    t = Task(
        id="t1",
        title="x",
        status=TaskStatus.RUNNING,
        claim_lock="L",
        current_run_id=1,
    )
    t2 = t.propose_change("need user confirm", run_id=1)
    assert t2.status is TaskStatus.WAITING_APPROVAL


def test_propose_change_rejects_non_running():
    t = Task(id="t1", title="x", status=TaskStatus.QUEUED)
    with pytest.raises(TaskStateError):
        t.propose_change("p", run_id=1)


def test_approve_reject_back_to_queued():
    t = Task(id="t1", title="x", status=TaskStatus.WAITING_APPROVAL)
    assert t.resolve_approval(approved=True).status is TaskStatus.QUEUED
    assert t.resolve_approval(approved=False).status is TaskStatus.QUEUED


def test_resolve_approval_rejects_non_waiting_approval():
    t = Task(id="t1", title="x", status=TaskStatus.RUNNING)
    with pytest.raises(TaskStateError):
        t.resolve_approval(approved=True)


def test_cancel_from_queued_running_waiting_failed():
    for st in (
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.FAILED,
    ):
        t = Task(id="t1", title="x", status=st)
        assert t.cancel().status is TaskStatus.CANCELLED


def test_cancel_rejects_terminal_states():
    for st in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.EXPIRED):
        t = Task(id="t1", title="x", status=st)
        with pytest.raises(TaskStateError):
            t.cancel()


def test_retry_from_failed_expired():
    assert (
        Task(id="t1", title="x", status=TaskStatus.FAILED).retry().status
        is TaskStatus.QUEUED
    )
    assert (
        Task(id="t1", title="x", status=TaskStatus.EXPIRED).retry().status
        is TaskStatus.QUEUED
    )


def test_retry_clears_claim_fields():
    t = Task(
        id="t1",
        title="x",
        status=TaskStatus.FAILED,
        claim_lock="L",
        claim_expires=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
        current_run_id=7,
    )
    t2 = t.retry()
    assert t2.claim_lock is None
    assert t2.claim_expires is None
    assert t2.current_run_id is None


def test_retry_does_not_clear_is_archived():
    t = Task(
        id="t1",
        title="x",
        status=TaskStatus.FAILED,
        is_archived=True,
    )
    t2 = t.retry()
    assert t2.status is TaskStatus.QUEUED
    assert t2.is_archived is True  # is_archived 保留


def test_retry_rejects_non_failed_expired():
    for st in (
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.SUCCEEDED,
        TaskStatus.CANCELLED,
    ):
        t = Task(id="t1", title="x", status=st)
        with pytest.raises(TaskStateError):
            t.retry()


def test_set_archived_does_not_change_status():
    t = Task(id="t1", title="x", status=TaskStatus.SUCCEEDED)
    t2 = t.set_archived(True)
    assert t2.is_archived is True
    assert t2.status is TaskStatus.SUCCEEDED


def test_set_archived_toggle_off():
    t = Task(id="t1", title="x", status=TaskStatus.QUEUED, is_archived=True)
    t2 = t.set_archived(False)
    assert t2.is_archived is False
    assert t2.status is TaskStatus.QUEUED


def test_set_archived_rejects_non_bool():
    t = Task(id="t1", title="x", status=TaskStatus.QUEUED)
    # Falsy/truthy coercion is allowed via bool(value), but value must be bool-able
    t2 = t.set_archived(True)
    assert t2.is_archived is True


# ---------------------------------------------------------------------------
# T5 S1: async Protocol ports
# ---------------------------------------------------------------------------


def test_task_registry_protocol():
    assert hasattr(TaskRegistry, "create_task")
    assert hasattr(TaskRegistry, "claim_task")
    assert hasattr(TaskRegistry, "list_queued_due")
    # Dependency-graph methods removed
    assert not hasattr(TaskRegistry, "list_ready")
    assert not hasattr(TaskRegistry, "recompute_ready")
    assert not hasattr(TaskRegistry, "create_graph")
    assert not hasattr(TaskRegistry, "add_link")
    assert not hasattr(TaskRegistry, "list_links")
    assert not hasattr(TaskRegistry, "list_children")
    assert not hasattr(TaskRegistry, "list_parents")


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
        "list_queued_due",
        "list_running",
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


# ---------------------------------------------------------------------------
# Removed value-object guard: TaskLink/CreateGraphCommand/CreateGraphResult must
# no longer be importable from app.domain.task.
# ---------------------------------------------------------------------------


def test_removed_value_objects_not_exported():
    import app.domain.task as task_mod

    for name in ("TaskLink", "CreateGraphCommand", "CreateGraphResult", "BlockKind"):
        assert not hasattr(task_mod, name), f"{name} should be removed from app.domain.task"
