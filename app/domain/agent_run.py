"""Execution identity, independent of dialogue and business attempt identity."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol


class RunOwnershipLost(RuntimeError):
    """The process may no longer execute or publish this run."""


class RunAdmissionConflict(ValueError):
    """The Session already has an unfinished foreground execution."""


class RunCancelledError(RuntimeError):
    """An explicit execution cancellation, distinct from process shutdown."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__("agent_run_cancelled")


@dataclass(frozen=True)
class RunPredecessor:
    run_id: str
    execution_kind: str
    task_run_id: int | None = None


@dataclass(frozen=True)
class RunRetryFacts:
    current_attempt_runs: tuple[RunPredecessor, ...] = ()
    earlier_attempt_runs: tuple[RunPredecessor, ...] = ()
    member_runs: tuple[RunPredecessor, ...] = ()


@dataclass(frozen=True)
class RunBinding:
    run_id: str
    session_id: str
    owner_token: str
    process_id: str
    ownership_check: Callable[[], Awaitable[None]] | None = field(default=None, compare=False, repr=False)


current_run: ContextVar[RunBinding | None] = ContextVar("agent_run", default=None)


@contextmanager
def bind_run(binding: RunBinding | None) -> Iterator[None]:
    token = current_run.set(binding)
    try:
        yield
    finally:
        current_run.reset(token)


async def check_current_run() -> None:
    binding = current_run.get()
    if binding is not None and binding.ownership_check is not None:
        await binding.ownership_check()


class AgentRunRegistry(Protocol):
    def register(self, **facts) -> dict: ...
    def activate(self, binding: RunBinding, lease_seconds: float) -> None: ...
    def check(self, binding: RunBinding) -> None: ...
    def heartbeat(self, binding: RunBinding, lease_seconds: float) -> None: ...
    def finish(self, binding: RunBinding, status: str, end_reason: str, error: str | None = None) -> None: ...
    def reconcile_expired(self) -> int: ...
    def get(self, run_id: str) -> dict | None: ...
    def list_for_session(self, session_id: str) -> list[dict]: ...
    def add_trace_link(self, biz_type: str, biz_id: str, trace_id: str) -> None: ...
    def trace_links(self, biz_type: str, biz_id: str) -> list[dict]: ...
    def find_by_trace(self, trace_id: str) -> list[dict]: ...
    def abandon_pending(self, binding: RunBinding) -> None: ...
    def retry_facts(self, task_run_id: int | None, delegation_member_id: str | None) -> RunRetryFacts: ...
