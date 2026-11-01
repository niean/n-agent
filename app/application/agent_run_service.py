"""Register execution before work and fence processes that lose their lease."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, replace
from uuid import uuid4

from app.domain.agent_run import AgentRunRegistry, RunBinding, RunCancelledError, RunOwnershipLost
from app.application.agent_run_retry_policy import AgentRunRetryPolicy


logger = logging.getLogger(__name__)


@dataclass
class RunExecution:
    """Explicit hand-off of terminal responsibility from admission to Graph."""

    binding: RunBinding
    graph_owns_terminal: bool = False


class AgentRunService:
    def __init__(self, registry: AgentRunRegistry, *, lease_seconds: float = 30,
                 heartbeat_seconds: float = 5):
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("agent_run_invalid_lease_intervals")
        self.registry = registry
        self.process_id = str(uuid4())
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._bindings: dict[str, RunBinding] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._foreground: dict[str, str] = {}
        self._fenced: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._monitor: asyncio.Task | None = None
        self._closed = False
        self._retry_policy = AgentRunRetryPolicy()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("agent_run_service_closed")
        self.registry.reconcile_expired()
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._heartbeat_loop(), name="agent-run-leases")

    async def register(self, run_id: str, session_id: str, source: str,
                       execution_kind: str, *, parent_run_id: str | None = None,
                       retry_of_run_id: str | None = None, task_run_id: int | None = None,
                       schedule_execution_id: str | None = None,
                       delegation_member_id: str | None = None,
                       foreground: bool = False) -> RunBinding:
        if self._closed:
            raise RuntimeError("agent_run_service_closed")
        self.registry.reconcile_expired()
        binding = RunBinding(run_id, session_id, str(uuid4()), self.process_id)
        binding = replace(binding, ownership_check=lambda: self.check(binding))
        if retry_of_run_id is None and (execution_kind == "task_worker" or delegation_member_id is not None):
            retry_of_run_id = self._retry_policy.resolve(
                execution_kind,
                self.registry.retry_facts(task_run_id, delegation_member_id),
            )
        # This durable write must succeed before the caller performs work.
        self.registry.register(
            run_id=run_id, session_id=session_id, source=source,
            execution_kind=execution_kind, process_id=self.process_id,
            owner_token=binding.owner_token, lease_seconds=self._lease_seconds,
            parent_run_id=parent_run_id, retry_of_run_id=retry_of_run_id,
            task_run_id=task_run_id, schedule_execution_id=schedule_execution_id,
            delegation_member_id=delegation_member_id, foreground=foreground,
        )
        self._bindings[run_id] = binding
        if foreground:
            self._foreground[session_id] = run_id
        await self.start()
        return binding

    async def activate(self, binding: RunBinding, task: asyncio.Task | None = None) -> None:
        if self._closed:
            raise RuntimeError("agent_run_service_closed")
        row = self.registry.get(binding.run_id)
        if (
            row is not None
            and row["status"] == "cancelled"
            and row["end_reason"] == "cancelled_before_start"
            and row["owner_token"] == binding.owner_token
            and row["process_id"] == binding.process_id
        ):
            # A stream may be consumed only after its pending admission was
            # explicitly cancelled. Its durable terminal result is final.
            raise RunCancelledError(binding.run_id)
        await self.check(binding)
        self.registry.activate(binding, self._lease_seconds)
        if task is not None:
            self._tasks[binding.run_id] = task

    async def check(self, binding: RunBinding) -> None:
        if self._closed:
            raise RuntimeError("agent_run_service_closed")
        if binding.run_id in self._fenced:
            raise RunOwnershipLost("agent_run_ownership_lost")
        self.registry.check(binding)

    async def finish(self, binding: RunBinding, status: str, end_reason: str,
                     error: str | None = None) -> None:
        if binding.run_id in self._fenced:
            self._forget(binding)
            raise RunOwnershipLost("agent_run_ownership_lost")
        try:
            self.registry.finish(binding, status, end_reason, error)
        except Exception:
            # The business result cannot be advertised as durably finalized.
            # Stop renewals; the persisted row later converges to unknown.
            self._fenced.add(binding.run_id)
            raise
        finally:
            self._forget(binding)

    def _forget(self, binding: RunBinding) -> None:
        self._bindings.pop(binding.run_id, None)
        self._tasks.pop(binding.run_id, None)
        self._cancel_requested.discard(binding.run_id)
        if self._foreground.get(binding.session_id) == binding.run_id:
            self._foreground.pop(binding.session_id, None)

    def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None:
            binding = self._bindings.get(run_id)
            if binding is None:
                return False
            row = self.registry.get(run_id)
            if row is None or row["status"] != "pending":
                return False
            try:
                self.registry.finish(binding, "cancelled", "cancelled_before_start")
            except RunOwnershipLost:
                # Expiry/reconciliation may win the race with a pending stop.
                # It is no longer ours to cancel, not a successful cancellation.
                self._forget(binding)
                return False
            self._forget(binding)
            return True
        if task.done():
            return False
        self._cancel_requested.add(run_id)
        task.cancel()
        return True

    def was_cancel_requested(self, run_id: str) -> bool:
        """Distinguish an explicit Run stop from process shutdown or lease loss."""
        return run_id in self._cancel_requested

    def cancel_session(self, session_id: str) -> bool:
        run_id = self._foreground.get(session_id)
        return self.cancel(run_id) if run_id is not None else False

    def is_cancelled(self, run_id: str) -> bool:
        """Also safe in graph branch callbacks executed in worker threads."""
        if run_id in self._fenced:
            return True
        task = self._tasks.get(run_id)
        return task is not None and (task.cancelled() or task.cancelling() > 0)

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._heartbeat_seconds)
                # Pending streams that nobody consumes are deliberately not
                # renewed; they become not_started after their admission lease.
                for run_id, task in list(self._tasks.items()):
                    binding = self._bindings.get(run_id)
                    if binding is None or task.done():
                        continue
                    try:
                        self.registry.heartbeat(binding, self._lease_seconds)
                    except Exception:
                        self._fenced.add(run_id)
                        task.cancel()
                        logger.exception("agent run lease renewal failed; execution fenced", extra={"run_id": run_id})
                try:
                    self.registry.reconcile_expired()
                except Exception:
                    logger.exception("agent run orphan reconciliation failed")
                for run_id, binding in list(self._bindings.items()):
                    if run_id in self._tasks:
                        continue
                    try:
                        self.registry.check(binding)
                    except RunOwnershipLost:
                        self._forget(binding)
                    except Exception:
                        self._fenced.add(run_id)
                        self._forget(binding)
                        logger.exception("pending agent run ownership check failed; execution fenced", extra={"run_id": run_id})
        except asyncio.CancelledError:
            pass

    async def close(self, timeout: float = 5) -> None:
        self._closed = True
        try:
            tasks = [task for task in self._tasks.values() if not task.done() and task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                # A task suppressing cancellation must not defeat shutdown's
                # timeout. Unknown results are left for lease expiry.
                await asyncio.wait(tasks, timeout=timeout)
            for binding in list(self._bindings.values()):
                try:
                    self.registry.abandon_pending(binding)
                except Exception as exc:
                    logger.error(
                        "pending agent run shutdown persistence failed: run_id=%s error_type=%s",
                        binding.run_id, type(exc).__name__,
                    )
        finally:
            if self._monitor is not None:
                self._monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await self._monitor

    def close_nowait(self) -> None:
        """Synchronous entry teardown; no fabricated terminal results."""
        self._closed = True
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._monitor is not None:
            self._monitor.cancel()

    def link_trace(self, biz_type: str, biz_id: str, trace_id: str) -> bool:
        """Observability association may fail without stopping execution."""
        try:
            self.registry.add_trace_link(biz_type, biz_id, trace_id)
            return True
        except Exception:
            logger.warning("trace association persistence failed", extra={"biz_type": biz_type, "biz_id": biz_id}, exc_info=True)
            return False

    def get(self, run_id: str) -> dict | None:
        return self.registry.get(run_id)

    def list_for_session(self, session_id: str) -> list[dict]:
        return self.registry.list_for_session(session_id)

    def trace_links(self, biz_type: str, biz_id: str) -> list[dict]:
        return self.registry.trace_links(biz_type, biz_id)

    def find_by_trace(self, trace_id: str) -> list[dict]:
        return self.registry.find_by_trace(trace_id)
