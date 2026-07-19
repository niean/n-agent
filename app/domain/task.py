"""Task aggregate, value objects, ports, and exceptions (Domain Layer).

Implements the Hermes Kanban-aligned Task subdomain. Pure domain: no FastAPI,
LangGraph, SQLite, OpenAI SDK, or `app.infrastructure` imports. Matches the
frozen-dataclass + enum + async Protocol pattern of `schedule.py` / `skill.py`.

State transition contract (spec):
  TRIAGE    -> TODO / ARCHIVED
  TODO      -> SCHEDULED / READY / BLOCKED / ARCHIVED
              (READY requires assignee + deps all DONE + scheduled_at empty or due)
  SCHEDULED -> READY / TODO / ARCHIVED
              (READY only when due and deps all DONE)
  READY     -> RUNNING / TODO / BLOCKED / ARCHIVED
              (RUNNING only by atomic claim)
  RUNNING   -> REVIEW / DONE / TODO / BLOCKED
              (TODO for retryable failure or reclaim; terminal must verify claim token)
  REVIEW    -> DONE / TODO / BLOCKED / ARCHIVED
  BLOCKED   -> TODO / ARCHIVED
  DONE      -> REVIEW / ARCHIVED
  ARCHIVED  -> (no generic transitions; unarchive is explicit domain op)
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
    """Task lifecycle states (Kanban columns)."""

    TRIAGE = "triage"
    TODO = "todo"
    SCHEDULED = "scheduled"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    ARCHIVED = "archived"


class BlockKind(str, Enum):
    """Reason categories for blocking a task.

    DEPENDENCY routes the task back to TODO (waiting on parent completion);
    all other kinds place the task in BLOCKED.
    """

    DEPENDENCY = "dependency"
    NEEDS_INPUT = "needs_input"
    CAPABILITY = "capability"
    TRANSIENT = "transient"


class TaskWorkspaceKind(str, Enum):
    """Workspace strategies accepted by the Task domain.

    WORKTREE is intentionally not declared; the spec scopes this iteration to
    SCRATCH/DIR only.
    """

    SCRATCH = "scratch"
    DIR = "dir"


class TaskRunStatus(str, Enum):
    """Lifecycle of an individual TaskRun (execution attempt)."""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    TERMINATED = "terminated"
    RECLAIMED = "reclaimed"


class TaskRunOutcome(str, Enum):
    """Terminal outcome for a TaskRun.

    Only COMPLETED/BLOCKED/GAVE_UP/CRASHED/TIMED_OUT/TERMINATED trigger
    notification delivery; FAILED/SPAWN_FAILED/RECLAIMED are retryable and
    do not notify.
    """

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"
    TERMINATED = "terminated"
    SPAWN_FAILED = "spawn_failed"
    GAVE_UP = "gave_up"
    RECLAIMED = "reclaimed"


# ---------------------------------------------------------------------------
# Execution policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """Per-task execution configuration.

    `allowed_tools` only governs additional general-purpose tools; the seven
    task tools are always authorized separately via the trusted task context
    (`permitted_managed_tools`), never through this field.
    """

    allowed_tools: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# State transition table (authoritative source of truth)
# ---------------------------------------------------------------------------

# Allowed generic transitions per spec contract table. ARCHIVED has no generic
# outgoing edges; unarchive is an explicit domain operation. Public constant
# so that TaskPolicy (same subdomain) can evaluate transition legality without
# reconstructing a Task instance.
TASK_TRANSITION_TABLE: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.TRIAGE: frozenset({TaskStatus.TODO, TaskStatus.ARCHIVED}),
    TaskStatus.TODO: frozenset(
        {TaskStatus.SCHEDULED, TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.SCHEDULED: frozenset({TaskStatus.READY, TaskStatus.TODO, TaskStatus.ARCHIVED}),
    TaskStatus.READY: frozenset(
        {TaskStatus.RUNNING, TaskStatus.TODO, TaskStatus.BLOCKED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.TODO, TaskStatus.BLOCKED}
    ),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.DONE, TaskStatus.TODO, TaskStatus.BLOCKED, TaskStatus.ARCHIVED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.TODO, TaskStatus.ARCHIVED}),
    TaskStatus.DONE: frozenset({TaskStatus.REVIEW, TaskStatus.ARCHIVED}),
    TaskStatus.ARCHIVED: frozenset(),
}


# ---------------------------------------------------------------------------
# Task aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """Task aggregate root (frozen dataclass with domain behavior).

    All domain behavior returns a new instance via ``dataclasses.replace``;
    callers must capture the return value. The aggregate never mutates in
    place.

    Field grouping follows the spec Components section:
      - 标识 (identity): id, title, body, assignee, priority, created_by,
        created_at, updated_at, version
      - 状态 (status): status, block_kind, block_reason, block_recurrences,
        started_at, completed_at
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
      - 归档恢复 (archive recovery): pre_archive_status
    """

    # 标识
    id: str
    title: str
    body: str = ""
    assignee: str | None = None
    priority: int = 0
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1

    # 状态
    status: TaskStatus = TaskStatus.TRIAGE
    block_kind: BlockKind | None = None
    block_reason: str | None = None
    block_recurrences: int = 0
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

    # 归档恢复
    pre_archive_status: TaskStatus | None = None

    # -----------------------------------------------------------------
    # State transition contract
    # -----------------------------------------------------------------

    def can_transition_to(self, target: TaskStatus) -> bool:
        """Return True if the generic transition ``self.status -> target`` is
        allowed by the spec contract table.

        Note: ARCHIVED has no generic outgoing edges; use ``unarchive()``
        for explicit recovery. READY->RUNNING is allowed by the table but
        actual transition requires an atomic claim (``claim()``).
        """
        return target in TASK_TRANSITION_TABLE.get(self.status, frozenset())

    def can_promote_to_ready(self, parents_done: bool, now: datetime) -> bool:
        """Return True if this task can be promoted to READY.

        Conditions (per spec):
          - current status is TODO or SCHEDULED
          - has an assignee
          - all parent dependencies are DONE (``parents_done=True``)
          - ``scheduled_at`` is None or already due (``scheduled_at <= now``)
        """
        if self.status not in (TaskStatus.TODO, TaskStatus.SCHEDULED):
            return False
        if not self.assignee:
            return False
        if not parents_done:
            return False
        if self.scheduled_at is not None:
            if self.scheduled_at > now:
                return False
        return True

    # -----------------------------------------------------------------
    # Block routing
    # -----------------------------------------------------------------

    def block(self, kind: BlockKind, reason: str) -> Task:
        """Return a new Task blocked with the given kind/reason.

        DEPENDENCY routes the task back to TODO (waiting for parent
        completion); all other kinds place the task in BLOCKED and
        increment ``block_recurrences`` (used by the unblock-loop breaker
        in TaskPolicy).
        """
        if kind == BlockKind.DEPENDENCY:
            return replace(
                self,
                status=TaskStatus.TODO,
                block_kind=kind,
                block_reason=reason,
                # DEPENDENCY does not count toward the unblock-loop budget
            )
        return replace(
            self,
            status=TaskStatus.BLOCKED,
            block_kind=kind,
            block_reason=reason,
            block_recurrences=self.block_recurrences + 1,
        )

    # -----------------------------------------------------------------
    # Claim / release (CAS)
    # -----------------------------------------------------------------

    def claim(self, run_id: int, claim_lock: str, expires_at: datetime) -> Task:
        """Atomically claim a READY task, producing a RUNNING instance.

        Raises TaskStateError if the task is not READY. The Registry performs
        the actual atomic CAS; this method is the domain-level specification
        for in-transaction validation.
        """
        if self.status != TaskStatus.READY:
            raise TaskStateError(
                f"claim requires READY status, got {self.status.value}"
            )
        return replace(
            self,
            status=TaskStatus.RUNNING,
            current_run_id=run_id,
            claim_lock=claim_lock,
            claim_expires=expires_at,
        )

    def release_claim(self, claim_lock: str) -> Task:
        """Release the current claim, clearing the lock and run id.

        Raises TaskClaimError if ``claim_lock`` does not match. Status is NOT
        changed by release; the caller (TaskRunService) sets the terminal
        status in the same transaction.
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

        Raises TaskClaimError if the token does not match.
        """
        if not self.claim_lock or self.claim_lock != claim_lock:
            raise TaskClaimError("claim_lock mismatch on heartbeat")
        return replace(self, last_heartbeat_at=now)

    def is_stale(self, now: datetime, heartbeat_timeout: int) -> bool:
        """Return True if the RUNNING task has not heartbeated within
        ``heartbeat_timeout`` seconds.

        Only RUNNING tasks with a claim can be considered stale; tasks in
        other states or without a heartbeat are not stale.
        """
        if self.status != TaskStatus.RUNNING:
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

        Does not change status; the caller decides whether to retry
        (status -> TODO) or give up (status -> BLOCKED with NEEDS_INPUT)
        based on ``should_give_up()``.
        """
        return replace(
            self,
            consecutive_failures=self.consecutive_failures + 1,
            last_failure_error=error,
        )

    def should_give_up(self) -> bool:
        """Return True when ``consecutive_failures > max_retries``.

        ``max_retries`` is the number of retries allowed after the first
        failure, so GAVE_UP triggers when total failures exceed max_retries
        by one (i.e., ``consecutive_failures > max_retries``).
        """
        return self.consecutive_failures > self.max_retries

    def complete(self, summary: str) -> Task:
        """Mark the task DONE and clear failure counters."""
        return replace(
            self,
            status=TaskStatus.DONE,
            result=summary,
            consecutive_failures=0,
            completed_at=datetime.now(timezone.utc),
        )

    # -----------------------------------------------------------------
    # Archive / unarchive (explicit domain operations)
    # -----------------------------------------------------------------

    def archive(self) -> Task:
        """Archive the task, recording ``pre_archive_status`` for recovery.

        archive() bypasses the generic transition table because it is an
        explicit domain operation. RUNNING tasks must be terminated first
        (RUNNING is not terminable through archive).
        """
        if self.status == TaskStatus.RUNNING:
            raise TaskStateError("cannot archive RUNNING task; terminate first")
        return replace(
            self,
            pre_archive_status=self.status,
            status=TaskStatus.ARCHIVED,
        )

    def unarchive(self) -> Task:
        """Restore the task to its pre-archive status, or TODO if unknown.

        Explicit domain operation; does not go through the generic transition
        table.
        """
        restored = self.pre_archive_status or TaskStatus.TODO
        return replace(
            self,
            status=restored,
            pre_archive_status=None,
        )


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
class TaskLink:
    """Dependency edge: child depends on parent (parent must be DONE before
    child can be promoted to READY)."""

    parent_id: str
    child_id: str


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
    """

    type: str
    name: str
    mime: str
    size: int
    storage_ref: str
    source_task_id: str
    summary: str
    checksum: str


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
class CreateGraphCommand:
    """Single-transaction graph creation (tasks + links + comments)."""

    tasks: tuple[Task, ...]
    links: tuple[TaskLink, ...] = ()
    comments: tuple[TaskComment, ...] = ()


@dataclass(frozen=True)
class CreateGraphResult:
    """Result of graph creation."""

    tasks: tuple[Task, ...]
    links: tuple[TaskLink, ...]
    comments: tuple[TaskComment, ...]


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a notify delivery attempt."""

    delivered: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Ports (async Protocols)
# ---------------------------------------------------------------------------


class TaskRegistry(Protocol):
    """Async port for Task persistence with atomic claim/lease/finish."""

    # --- CRUD ---
    async def create_task(self, task: Task) -> Task: ...
    async def get_task(self, task_id: str) -> Task | None: ...
    async def list_tasks(
        self,
        board: str = "default",
        cursor: TaskListCursor | None = None,
        limit: int = 100,
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

    # --- dispatch helpers ---
    async def list_ready(self, board: str = "default", limit: int = 100) -> tuple[Task, ...]: ...
    async def list_running(self, board: str = "default") -> tuple[Task, ...]: ...
    async def recompute_ready(self, board: str = "default") -> tuple[str, ...]: ...

    # --- dependency graph ---
    async def create_graph(self, command: CreateGraphCommand) -> CreateGraphResult: ...
    async def add_link(self, parent_id: str, child_id: str) -> TaskLink: ...
    async def remove_link(self, parent_id: str, child_id: str) -> bool: ...
    async def list_links(self, task_id: str) -> tuple[TaskLink, ...]: ...
    async def list_children(self, parent_id: str) -> tuple[Task, ...]: ...
    async def list_parents(self, child_id: str) -> tuple[Task, ...]: ...

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
