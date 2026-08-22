"""Task aggregate, value objects, ports, and exceptions (Domain Layer).

Implements the Manus-aligned Task subdomain: a flat 7-state state machine
with agent automatic scheduling (``scheduled_at``) and intent approval
(``propose_change`` / ``resolve_approval``). Pure domain -- no FastAPI,
LangGraph, SQLite, OpenAI SDK, or ``app.infrastructure`` imports. Matches the
frozen-dataclass + enum + async Protocol pattern of ``schedule.py`` /
``skill.py``.

State transition contract (spec Data Model):
  QUEUED            -> RUNNING / CANCELLED
  RUNNING           -> SUCCEEDED / FAILED / WAITING_APPROVAL /
                      CANCELLED / EXPIRED / QUEUED
                      (QUEUED is reserved for retryable-failure auto-retry;
                       users cannot manually drag RUNNING back to QUEUED.)
  WAITING_APPROVAL  -> QUEUED / CANCELLED
  FAILED            -> QUEUED / CANCELLED
  EXPIRED           -> QUEUED
  SUCCEEDED         -> (terminal)
  CANCELLED         -> (terminal)

``is_archived`` is a soft-delete flag, NOT a lifecycle state. Archive /
unarchive never changes ``status`` and never triggers run finalization; it
only toggles ``is_archived`` for list/board visibility. Removed concepts
from the prior 9-state machine: ``assignee``, ``pre_archive_status``,
``block_kind``/``block_reason``/``block_recurrences``, ``BlockKind``, the
dependency graph (``TaskLink`` / ``CreateGraphCommand`` /
``CreateGraphResult``), and swarm planning commands.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Task lifecycle states (Manus-aligned 7-state machine)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TaskWorkspaceKind(str, Enum):
    """Workspace strategies accepted by the Task domain.

    WORKTREE is intentionally not declared; the spec scopes this iteration to
    SCRATCH/DIR only.
    """

    SCRATCH = "scratch"
    DIR = "dir"


class TaskRunStatus(str, Enum):
    """Lifecycle of an individual TaskRun (execution attempt).

    Legacy BLOCKED/RECLAIMED values are retained for historical run rows;
    new code must not produce them. Terminal outcome is captured by
    ``TaskRunOutcome``.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    TERMINATED = "terminated"
    RECLAIMED = "reclaimed"


class TaskRunOutcome(str, Enum):
    """Terminal outcome for a TaskRun (Manus-aligned).

    Retryable outcomes (``FAILED`` / ``CRASHED`` / ``TIMED_OUT`` /
    ``SPAWN_FAILED``) map to either ``QUEUED`` (under ``max_retries``) or
    ``FAILED`` (over ``max_retries``) on the Task; the TaskPolicy decides.
    ``WAITING_APPROVAL`` releases the claim like any terminal outcome and
    moves the task to ``WAITING_APPROVAL``. ``TERMINATED`` represents
    user-initiated cancel of a RUNNING worker -> task ``CANCELLED``.
    ``EXPIRED`` represents stale/lease expiration -> task ``EXPIRED``.
    ``ABORTED`` represents worker-initiated fast-fail (worker 判定无法继续、
    不再重试) -> task ``FAILED`` 终态，绕过断路器（区别于 FAILED/SPAWN_FAILED
    的可重试系统失败）。取消（CANCELLED）只认用户指令，worker 不得触发取消。

    Legacy ``BLOCKED`` / ``GAVE_UP`` / ``RECLAIMED`` outcomes are removed;
    historical run rows are migrated by the registry.
    """

    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    FAILED = "failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    EXPIRED = "expired"
    TERMINATED = "terminated"
    ABORTED = "aborted"


# ---------------------------------------------------------------------------
# Execution policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """Per-task execution configuration.

    ``allowed_tools`` only governs additional general-purpose tools; the
    task tools are always authorized separately via the trusted task context
    (``permitted_managed_tools``), never through this field.
    """

    allowed_tools: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# State transition table (authoritative source of truth)
# ---------------------------------------------------------------------------

# Allowed transitions per spec Data Model. ``SUCCEEDED`` / ``CANCELLED`` are
# terminal. ``RUNNING -> QUEUED`` is allowed by the table but reserved for
# retryable-failure auto-retry (TaskPolicy + TaskRunService); users cannot
# manually trigger it. Public constant so that TaskPolicy (same subdomain)
# can evaluate transition legality without reconstructing a Task instance.
TASK_TRANSITION_TABLE: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
            TaskStatus.QUEUED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.FAILED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.EXPIRED: frozenset({TaskStatus.QUEUED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


def available_lifecycle_actions(
    status: TaskStatus, interaction_type: str | None = None,
) -> tuple[str, ...]:
    """Return the lifecycle card actions exposed for a task status.

    Empty tuple means the status renders a plain-text lifecycle message with
    no interactive card. The Task aggregate and action APIs remain the final
    authority on legality; this only drives which buttons the Dashboard card
    offers. EXPIRED returns only ``retry`` because ``Task.cancel()`` rejects
    EXPIRED (expired tasks must be retried, not cancelled).

    ``interaction_type`` distinguishes WAITING_APPROVAL card flavors:
      - ``"approval"`` (default, also when ``None`` or any value other than
        ``"intent_request"``): show approve/reject buttons for the user to
        approve or reject the worker's proposal.
      - ``"intent_request"``: show revise/cancel buttons for the user to
        supply additional intent/information (revise carries the note) or
        cancel the task. The same domain transitions apply; only the offered
        action set differs.

    Validation of ``interaction_type`` membership is the Application layer's
    responsibility (``TaskService.propose_change`` rejects unknown values).
    This Domain pure function conservatively falls back to the approval
    action set for any non-``"intent_request"`` value so a malformed payload
    never produces the revise/cancel pair unintentionally.

    FAILED/EXPIRED/other statuses ignore ``interaction_type``.
    """
    if status == TaskStatus.WAITING_APPROVAL:
        if interaction_type == "intent_request":
            return ("revise", "cancel")
        return ("approve", "reject")
    if status == TaskStatus.FAILED:
        return ("retry", "cancel")
    if status == TaskStatus.EXPIRED:
        return ("retry",)
    return ()


# ---------------------------------------------------------------------------
# Task aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """Task aggregate root (frozen dataclass with domain behavior).

    All domain behavior returns a new instance via ``dataclasses.replace``;
    callers must capture the return value. The aggregate never mutates in
    place.

    Field grouping:
      - 标识 (identity): id, title, body, priority, created_by,
        created_at, updated_at, version
      - 状态 (status): status, started_at, completed_at
      - 调度 (scheduling): scheduled_at, claim_lock, claim_expires,
        current_run_id
      - 执行配置 (execution): workspace_kind, workspace_path, skills,
        execution_policy, model_override, max_runtime_seconds, max_retries,
        goal_mode, goal_max_turns
      - 编排 (orchestration, 预留): workflow_template_id, current_step_key,
        project_id, tenant, board (固定 default)
      - 会话 (sessions): origin_session_id, execution_session_id
      - 韧性 (resilience): consecutive_failures, worker_token,
        last_failure_error, last_heartbeat_at, result, idempotency_key
      - 归档 (archive flag): is_archived (软删标志，非状态)

    Removed fields (vs prior 9-state machine): ``assignee``,
    ``pre_archive_status``, ``block_kind``, ``block_reason``,
    ``block_recurrences``. Archive is now a boolean flag, not a state.
    """

    # 标识
    id: str
    title: str
    body: str = ""
    priority: int = 0
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1

    # 状态
    status: TaskStatus = TaskStatus.QUEUED
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # 调度
    scheduled_at: datetime | None = None
    claim_lock: str | None = None
    claim_expires: datetime | None = None
    current_run_id: int | None = None

    # 执行配置
    workspace_kind: TaskWorkspaceKind = TaskWorkspaceKind.SCRATCH
    workspace_path: str | None = None
    skills: tuple[str, ...] = ()
    execution_policy: TaskExecutionPolicy = field(default_factory=TaskExecutionPolicy)
    model_override: str | None = None
    max_runtime_seconds: int | None = None
    max_retries: int = 0
    goal_mode: bool = False
    goal_max_turns: int | None = None

    # 编排 (预留)
    workflow_template_id: str | None = None
    current_step_key: str | None = None
    project_id: str | None = None
    tenant: str | None = None
    board: str = "default"

    # 会话
    origin_session_id: str | None = None
    execution_session_id: str | None = None

    # 韧性
    consecutive_failures: int = 0
    worker_token: str | None = None
    last_failure_error: str | None = None
    last_heartbeat_at: datetime | None = None
    result: str | None = None
    idempotency_key: str | None = None

    # 归档 (软删标志，非状态)
    is_archived: bool = False

    # -----------------------------------------------------------------
    # State transition contract
    # -----------------------------------------------------------------

    def can_transition_to(self, target: TaskStatus) -> bool:
        """Return True if the transition ``self.status -> target`` is
        allowed by the spec contract table.

        ``SUCCEEDED`` / ``CANCELLED`` are terminal. ``RUNNING -> QUEUED`` is
        allowed by the table but reserved for retryable-failure auto-retry
        (TaskPolicy + TaskRunService); users cannot manually trigger it.
        """
        return target in TASK_TRANSITION_TABLE.get(self.status, frozenset())

    def can_claim(self, now: datetime) -> bool:
        """Return True if this task can be claimed by a dispatcher.

        Conditions (per spec):
          - current status is ``QUEUED``
          - ``scheduled_at`` is None or already due (``scheduled_at <= now``)

        Assignee and parent-dependency checks were removed; the task is an
        autonomous execution unit.
        """
        if self.status is not TaskStatus.QUEUED:
            return False
        if self.scheduled_at is not None and self.scheduled_at > now:
            return False
        return True

    # -----------------------------------------------------------------
    # Claim / release (CAS)
    # -----------------------------------------------------------------

    def claim(self, run_id: int, claim_lock: str, expires_at: datetime) -> Task:
        """Atomically claim a QUEUED task, producing a RUNNING instance.

        Raises ``TaskStateError`` if the task is not ``QUEUED``. The Registry
        performs the actual atomic CAS; this method is the domain-level
        specification for in-transaction validation.
        """
        if self.status is not TaskStatus.QUEUED:
            raise TaskStateError(
                f"claim requires QUEUED status, got {self.status.value}"
            )
        return replace(
            self,
            status=TaskStatus.RUNNING,
            current_run_id=run_id,
            claim_lock=claim_lock,
            claim_expires=expires_at,
            started_at=datetime.now(timezone.utc),
        )

    def release_claim(self, claim_lock: str) -> Task:
        """Release the current claim, clearing the lock and run id.

        Raises ``TaskClaimError`` if ``claim_lock`` does not match. Status
        is NOT changed by release; the caller (TaskRunService) sets the
        terminal status in the same transaction.
        """
        if not self.claim_lock or self.claim_lock != claim_lock:
            raise TaskClaimError("claim_lock mismatch on release")
        return replace(
            self,
            claim_lock=None,
            claim_expires=None,
            current_run_id=None,
        )

    # -----------------------------------------------------------------
    # Heartbeat / staleness
    # -----------------------------------------------------------------

    def record_heartbeat(self, now: datetime, claim_lock: str) -> Task:
        """Update ``last_heartbeat_at`` after verifying ``claim_lock``.

        Raises ``TaskClaimError`` if the token does not match.
        """
        if not self.claim_lock or self.claim_lock != claim_lock:
            raise TaskClaimError("claim_lock mismatch on heartbeat")
        return replace(self, last_heartbeat_at=now)

    def is_stale(self, now: datetime, heartbeat_timeout: int) -> bool:
        """Return True if the RUNNING task has not heartbeated within
        ``heartbeat_timeout`` seconds.

        Only RUNNING tasks with a claim and a heartbeat can be considered
        stale; tasks in other states or without a heartbeat are not stale.
        """
        if self.status is not TaskStatus.RUNNING:
            return False
        if not self.claim_lock:
            return False
        if self.last_heartbeat_at is None:
            return False
        elapsed = (now - self.last_heartbeat_at).total_seconds()
        return elapsed > heartbeat_timeout

    # -----------------------------------------------------------------
    # Failure / completion
    # -----------------------------------------------------------------

    def record_failure(self, error: str) -> Task:
        """Record a failure attempt, incrementing ``consecutive_failures``.

        Does not change status; the caller (TaskPolicy + TaskRunService)
        decides whether to retry (status -> QUEUED) or give up
        (status -> FAILED) based on ``should_give_up()``.
        """
        return replace(
            self,
            consecutive_failures=self.consecutive_failures + 1,
            last_failure_error=error,
        )

    def should_give_up(self) -> bool:
        """Return True when ``consecutive_failures > max_retries``.

        ``max_retries`` is the number of retries allowed after the first
        failure, so give-up triggers when total failures exceed max_retries
        by one (i.e., ``consecutive_failures > max_retries``).
        """
        return self.consecutive_failures > self.max_retries

    def complete(self, summary: str) -> Task:
        """Mark the task SUCCEEDED and clear failure counters."""
        return replace(
            self,
            status=TaskStatus.SUCCEEDED,
            result=summary,
            consecutive_failures=0,
            completed_at=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # Intent approval (propose / resolve)
    # -----------------------------------------------------------------

    def propose_change(self, proposal: str, run_id: int) -> Task:
        """Move RUNNING -> WAITING_APPROVAL.

        Workers call this when encountering a change that requires user
        decision. The proposal text is persisted as a ``change_proposed``
        event by TaskService; the domain method only advances state.
        Claim release is handled by TaskRunService's unified run-finalization
        path, not here.

        Raises ``TaskStateError`` if the task is not RUNNING.
        """
        if self.status is not TaskStatus.RUNNING:
            raise TaskStateError(
                f"propose_change requires RUNNING, got {self.status.value}"
            )
        return replace(self, status=TaskStatus.WAITING_APPROVAL)

    def resolve_approval(self, approved: bool) -> Task:
        """Move WAITING_APPROVAL -> QUEUED.

        Both approve and reject return the task to QUEUED so the next run
        can pick it up. The decision itself is captured in a
        ``change_approved`` / ``change_rejected`` event by TaskService; the
        domain method only advances state.

        Raises ``TaskStateError`` if the task is not WAITING_APPROVAL.
        """
        if self.status is not TaskStatus.WAITING_APPROVAL:
            raise TaskStateError(
                f"resolve_approval requires WAITING_APPROVAL, got {self.status.value}"
            )
        return replace(self, status=TaskStatus.QUEUED)

    def revise(self) -> Task:
        """Move WAITING_APPROVAL -> QUEUED as the third approval decision.

        ``revise`` is the third approval decision alongside approve/reject:
        the user gives revision instructions for the worker to re-execute.
        The state transition WAITING_APPROVAL -> QUEUED is already legal in
        ``TASK_TRANSITION_TABLE``, so this method does not extend the state
        machine. The revision note is persisted as a ``change_revised``
        event by TaskService; the domain method only advances state.

        Raises ``TaskStateError`` if the task is not WAITING_APPROVAL.
        """
        if self.status is not TaskStatus.WAITING_APPROVAL:
            raise TaskStateError(
                f"revise requires WAITING_APPROVAL, got {self.status.value}"
            )
        return replace(self, status=TaskStatus.QUEUED)

    # -----------------------------------------------------------------
    # Cancel / retry (user actions)
    # -----------------------------------------------------------------

    def cancel(self) -> Task:
        """Move {QUEUED, RUNNING, WAITING_APPROVAL, FAILED} -> CANCELLED.

        RUNNING cancel must be coordinated through TaskRunService to
        terminate the worker and release the claim in the same transaction;
        the domain method only advances state.

        Raises ``TaskStateError`` for terminal states (SUCCEEDED / CANCELLED)
        and for EXPIRED (expired tasks must be retried, not cancelled).
        """
        if self.status in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.EXPIRED):
            raise TaskStateError(
                f"cannot cancel from {self.status.value}"
            )
        return replace(
            self,
            status=TaskStatus.CANCELLED,
            completed_at=datetime.now(timezone.utc),
        )

    def retry(self) -> Task:
        """Move {FAILED, EXPIRED} -> QUEUED and clear claim fields.

        Does not clear ``is_archived``; archive is a separate soft-delete
        flag independent of lifecycle.

        Raises ``TaskStateError`` if the task is not FAILED or EXPIRED.
        """
        if self.status not in (TaskStatus.FAILED, TaskStatus.EXPIRED):
            raise TaskStateError(
                f"retry requires FAILED/EXPIRED, got {self.status.value}"
            )
        return replace(
            self,
            status=TaskStatus.QUEUED,
            claim_lock=None,
            claim_expires=None,
            current_run_id=None,
        )

    # -----------------------------------------------------------------
    # Archive flag (soft delete, does not change status)
    # -----------------------------------------------------------------

    def set_archived(self, value: bool) -> Task:
        """Toggle ``is_archived`` without changing status or claim fields.

        Archive is a soft-delete flag for list/board visibility; it is NOT
        a lifecycle state and never triggers run finalization.
        """
        return replace(self, is_archived=bool(value))


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRun:
    """A single execution attempt of a Task."""

    id: int
    task_id: str
    profile: str = "default"
    status: TaskRunStatus = TaskRunStatus.RUNNING
    claim_lock: str | None = None
    claim_expires: datetime | None = None
    worker_token: str | None = None
    max_runtime_seconds: int | None = None
    lease_seconds: int | None = None
    last_heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    outcome: TaskRunOutcome | None = None
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class TaskComment:
    """Human/worker comment attached to a Task."""

    id: str
    task_id: str
    author: str
    body: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskEvent:
    """Append-only audit event for a Task.

    ``id`` is monotonically increasing per registry (used as the WebSocket
    ``since`` cursor and the notify idempotency boundary).
    """

    id: int
    task_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    run_id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskAttachment:
    """Metadata for a file attached to a Task.

    Only ``stored_name`` (server-generated) is persisted; the client-supplied
    ``filename`` is retained for display only. The domain never stores host
    absolute paths.
    """

    id: str
    task_id: str
    filename: str
    stored_name: str
    content_type: str
    size: int
    checksum: str
    uploaded_by: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class TaskArtifact:
    """Logical artifact produced by a Task (cross-task handoff).

    Distinct from TaskAttachment: Artifact is the domain-level descriptor
    that may point at any storage_ref (not necessarily a local file).

    ``content`` carries inline text for text-kind artifacts, used when the
    worker submits the full content directly instead of a ``workspace:``
    file ref. Server-side registration prefers ``content`` (then ``summary``
    fallback) to create an inline artifact when no readable workspace ref
    is provided.
    """

    type: str
    name: str
    mime: str
    size: int
    storage_ref: str
    source_task_id: str
    summary: str
    checksum: str
    content: str | None = None


# ---------------------------------------------------------------------------
# Registry command / result value objects (shared with Infrastructure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskListCursor:
    """Stable pagination cursor (created_at, id)."""

    created_at: datetime | None
    task_id: str


@dataclass(frozen=True)
class TaskListPage:
    """A page of Task list results."""

    items: tuple[Task, ...]
    next_cursor: TaskListCursor | None = None


@dataclass(frozen=True)
class ClaimResult:
    """Result of an atomic claim."""

    task: Task
    run: TaskRun


@dataclass(frozen=True)
class FinishRunCommand:
    """In-transaction run finalization specification.

    ``target_task_status`` lets the caller (TaskRunService) own the task
    status decision. When None, the registry applies its default
    outcome->status mapping (backward compat). When set, the registry uses
    the provided status as the task's new status; outcome-specific side
    effects (e.g. clearing failures on COMPLETED) still apply.
    """

    task_id: str
    run_id: int
    claim_lock: str
    outcome: TaskRunOutcome
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[TaskArtifact, ...] = ()
    target_task_status: TaskStatus | None = None
    error: str | None = None


@dataclass(frozen=True)
class FinishRunResult:
    """Result of an atomic run finish."""

    task: Task
    run: TaskRun
    terminal_event: TaskEvent


@dataclass(frozen=True)
class RecoverRunCommand:
    """In-transaction stale-run recovery specification."""

    task_id: str
    run_id: int
    claim_lock: str
    outcome: TaskRunOutcome
    error: str | None = None


@dataclass(frozen=True)
class BulkUpdateItem:
    """A single item in a bulk update request."""

    task_id: str
    fields: Mapping[str, Any]
    expected_version: int


@dataclass(frozen=True)
class BulkUpdateCommand:
    """Bulk update command (single transaction, all-or-nothing)."""

    items: tuple[BulkUpdateItem, ...]


@dataclass(frozen=True)
class BulkUpdateResult:
    """Result of a bulk update."""

    updated: tuple[Task, ...]


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a notify delivery attempt."""

    delivered: bool
    error: str | None = None


@dataclass(frozen=True)
class ProposalResolutionCommand:
    """In-transaction proposal resolution specification.

    Captures a user's approval decision on a pending ``change_proposed``
    event. ``decision`` is one of ``"approved"`` / ``"rejected"`` /
    ``"revised"`` (revise = give the worker revision instructions and
    re-queue for another run). ``event_kind`` is the corresponding
    ``change_approved`` / ``change_rejected`` / ``change_revised`` kind
    string; ``note`` carries the user's revision note (non-empty for
    revise, optional otherwise). The Domain command layer does not
    validate ``note`` / ``decision`` semantics; that is the service /
    registry's responsibility.

    The command intentionally does NOT carry ``proposal_event_id``: the
    Registry locates the pending proposal inside the transaction (by
    ``task_id`` + unmatched ``change_proposed``). ``expected_version`` is
    used by the Registry for optimistic-lock CAS on the Task row.
    """

    task_id: str
    expected_version: int
    decision: str
    event_kind: str
    note: str | None = None


@dataclass(frozen=True)
class ProposalResolutionResult:
    """Result of an atomic proposal resolution.

    ``proposal_event_id`` is the id of the resolved ``change_proposed``
    event (non-nullable ``int``): the Registry always locates a pending
    proposal when constructing this result, raising ``TaskStateError``
    when none exists. ``task`` is the persisted Task after the state
    transition; ``decision_event`` is the newly-appended
    ``change_approved`` / ``change_rejected`` / ``change_revised``
    event.
    """

    proposal_event_id: int
    task: Task
    decision_event: TaskEvent


# ---------------------------------------------------------------------------
# Ports (async Protocols)
# ---------------------------------------------------------------------------


class TaskRegistry(Protocol):
    """Async port for Task persistence with atomic claim/lease/finish.

    The dependency-graph methods (``create_graph`` / ``add_link`` /
    ``remove_link`` / ``list_links`` / ``list_children`` / ``list_parents``)
    and the old ``list_ready`` / ``recompute_ready`` helpers have been
    removed: the new state machine has no READY/BLOCKED states and no
    parent-child dependency graph. Dispatch now consumes
    ``list_queued_due`` (QUEUED + scheduled_at due + not archived).
    """

    # --- CRUD ---
    async def create_task(self, task: Task) -> Task: ...
    async def get_task(self, task_id: str) -> Task | None: ...
    async def list_tasks(
        self,
        board: str = "default",
        cursor: TaskListCursor | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> TaskListPage: ...
    async def update_task(
        self,
        task_id: str,
        fields: Mapping[str, Any],
        expected_version: int,
    ) -> Task: ...
    async def bulk_update(self, command: BulkUpdateCommand) -> BulkUpdateResult: ...
    async def delete_task(self, task_id: str) -> bool: ...

    # --- claim / heartbeat / finish / recover (CAS) ---
    async def claim_task(
        self,
        task_id: str,
        claim_lock: str,
        lease_seconds: int,
    ) -> ClaimResult | None: ...
    async def record_heartbeat(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        now: datetime,
    ) -> Task: ...
    async def finish_run(self, command: FinishRunCommand) -> FinishRunResult: ...
    async def recover_run(self, command: RecoverRunCommand) -> FinishRunResult: ...

    # --- proposal resolution (CAS) ---
    async def resolve_proposal(
        self,
        command: ProposalResolutionCommand,
    ) -> ProposalResolutionResult: ...
    async def latest_waiting_approval_in_session(
        self,
        session_id: str,
    ) -> Task | None: ...

    # --- dispatch helpers ---
    async def list_queued_due(
        self,
        now: datetime,
        limit: int = 100,
        board: str = "default",
    ) -> tuple[Task, ...]: ...
    async def list_running(self, board: str = "default") -> tuple[Task, ...]: ...

    # --- comments ---
    async def add_comment(
        self,
        task_id: str,
        author: str,
        body: str,
    ) -> TaskComment: ...
    async def list_comments(self, task_id: str) -> tuple[TaskComment, ...]: ...

    # --- events ---
    async def append_event(
        self,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
        run_id: int | None = None,
    ) -> TaskEvent: ...
    async def list_events(
        self,
        task_id: str,
        since: int = 0,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]: ...

    # --- runs ---
    async def list_runs(self, task_id: str, limit: int = 50) -> tuple[TaskRun, ...]: ...

    # --- attachments ---
    async def add_attachment(
        self,
        task_id: str,
        filename: str,
        stored_name: str,
        content_type: str,
        size: int,
        checksum: str,
        uploaded_by: str,
    ) -> TaskAttachment: ...
    async def list_attachments(self, task_id: str) -> tuple[TaskAttachment, ...]: ...
    async def get_attachment(self, attachment_id: str) -> TaskAttachment | None: ...
    async def delete_attachment(self, attachment_id: str) -> bool: ...

    # --- notify subscriptions ---
    async def subscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool: ...
    async def list_notify_subs(self, task_id: str) -> tuple[dict[str, Any], ...]: ...
    async def unsubscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool: ...
    async def update_notify_sub_last_event(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        last_terminal_event_id: int,
    ) -> bool: ...


class TaskDispatcher(Protocol):
    """Async port for process-internal worker management.

    Production implementation holds ``asyncio.Task`` handles keyed by
    ``task_run_id``; tests may substitute a fake. ``worker_token`` is an
    opaque process-generated identifier -- never a serialized asyncio.Task
    or memory address.
    """

    async def spawn(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
    ) -> str: ...
    async def cancel(self, worker_token: str) -> bool: ...
    async def inspect(self) -> dict[str, Any]: ...


class TaskNotifier(Protocol):
    """Async port for terminal-event delivery."""

    async def deliver(
        self,
        task: Task,
        terminal_event: TaskEvent,
    ) -> DeliveryResult: ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskNotFoundError(Exception):
    """Raised when a Task or related entity does not exist."""


class TaskValidationError(Exception):
    """Raised when domain validation rejects a field or state."""


class TaskClaimError(Exception):
    """Raised when a claim token does not match on CAS."""


class TaskStateError(Exception):
    """Raised when a state transition violates the contract table."""


class TaskConflictError(Exception):
    """Raised on optimistic-lock version conflict or duplicate idempotency."""


class TaskAttachmentError(Exception):
    """Raised when an attachment fails validation or storage."""
