"""T15: TaskRunner -- dispatch loop driver + in-process TaskDispatcher.

Implements the ``TaskDispatcher`` port (spawn/cancel/inspect) and drives the
periodic ``dispatch_once`` tick. Production worker management holds
``asyncio.Task`` handles keyed by ``task_run_id`` (NOT ``task_id``), so a
late-arriving old worker cannot overwrite a newer run.

Circular dependency resolution: ``TaskRunService`` takes ``dispatcher`` (this
runner) as a constructor param, and this runner needs ``run_service`` to call
``run_claim`` from ``spawn``. ``run_service`` is late-bound via
``set_run_service`` after both are constructed in ``main.py``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.domain.task import Task

logger = logging.getLogger(__name__)


class TaskRunner:
    """Periodic dispatch loop + in-process TaskDispatcher implementation.

    Args:
        interval_seconds: tick interval for ``dispatch_once``.
        shutdown_grace_seconds: grace period to await/cancel workers on stop.
    """

    def __init__(
        self,
        interval_seconds: int = 30,
        shutdown_grace_seconds: int = 30,
    ) -> None:
        self.interval_seconds = interval_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        # run_service is late-bound (see module docstring).
        self._run_service: Any = None
        self._running_workers: dict[int, asyncio.Task] = {}
        self._token_to_run: dict[str, int] = {}
        self._run_meta: dict[int, dict[str, Any]] = {}
        self._crashed: list[dict[str, Any]] = []
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Late binding
    # ------------------------------------------------------------------

    def set_run_service(self, run_service: Any) -> None:
        """Late-bind the TaskRunService (called by main.py after wiring)."""
        self._run_service = run_service

    # ------------------------------------------------------------------
    # TaskDispatcher: spawn / cancel / inspect
    # ------------------------------------------------------------------

    async def spawn(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
    ) -> str:
        """Fire-and-forget: create an asyncio task running run_claim.

        Keyed by ``run_id`` so a late old worker (same task, prior run) does
        not overwrite a newer run's entry.
        """
        if self._run_service is None:
            raise RuntimeError("TaskRunner.run_service not bound; call set_run_service")
        worker_token = f"wt-{run_id}-{uuid4().hex[:8]}"
        asyncio_task = asyncio.create_task(
            self._run_worker(task, run_id, claim_lock, worker_token)
        )
        self._running_workers[run_id] = asyncio_task
        self._token_to_run[worker_token] = run_id
        self._run_meta[run_id] = {
            "run_id": run_id,
            "task_id": task.id,
            "worker_token": worker_token,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return worker_token

    async def _run_worker(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
        worker_token: str,
    ) -> None:
        """Worker coroutine: calls run_service.run_claim, cleans up on done."""
        try:
            await self._run_service.run_claim(task, run_id, claim_lock)
        except Exception:
            # run_claim is expected to swallow exceptions internally (it
            # catches executor crashes/timeout and CAS-finalizes). If it still
            # raises, record as crashed for recover_crashed_workers.
            logger.exception(
                "worker crashed unexpectedly: task=%s run=%s", task.id, run_id
            )
            self._crashed.append({
                "run_id": run_id,
                "task_id": task.id,
                "worker_token": worker_token,
                "error": "worker task raised",
            })
        finally:
            # Remove from active regardless of outcome. Late-arriving old
            # worker for the same run_id is a no-op here (newer run owns the
            # slot).
            current = self._running_workers.get(run_id)
            if current is asyncio.current_task():
                self._running_workers.pop(run_id, None)
                self._run_meta.pop(run_id, None)
            self._token_to_run.pop(worker_token, None)

    async def cancel(self, worker_token: str) -> bool:
        """Cancel an in-process worker by token. Returns True if found."""
        run_id = self._token_to_run.get(worker_token)
        if run_id is None:
            return False
        asyncio_task = self._running_workers.get(run_id)
        if asyncio_task is None or asyncio_task.done():
            self._token_to_run.pop(worker_token, None)
            return False
        asyncio_task.cancel()
        try:
            await asyncio.wait_for(asyncio_task, timeout=self.shutdown_grace_seconds)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            logger.warning(
                "worker %s did not cancel within grace; leaving", worker_token
            )
        except Exception:
            # Task raised during cancellation; already recorded as crashed.
            pass
        self._running_workers.pop(run_id, None)
        self._run_meta.pop(run_id, None)
        self._token_to_run.pop(worker_token, None)
        return True

    async def inspect(self) -> dict[str, Any]:
        """Return a serializable snapshot (no asyncio.Task references)."""
        # Purge done-but-not-yet-cleaned entries first.
        for run_id in list(self._running_workers.keys()):
            t = self._running_workers.get(run_id)
            if t is not None and t.done() and not t.cancelled():
                self._running_workers.pop(run_id, None)
                self._run_meta.pop(run_id, None)
        return {"active": list(self._run_meta.values())}

    async def get_crashed_workers(self) -> list[dict[str, Any]]:
        """Return and clear the crashed-worker queue."""
        crashed = list(self._crashed)
        self._crashed.clear()
        return crashed

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the periodic dispatch loop. Idempotent."""
        if self._started:
            return
        self._started = True
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(
            "TaskRunner started (interval=%ss)", self.interval_seconds
        )

    async def stop(self) -> None:
        """Stop the loop and converge/cancel workers within shutdown grace."""
        if not self._started:
            return
        self._started = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=self.interval_seconds + 1)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
            except Exception:
                pass
        # Cancel remaining workers within grace.
        if self._running_workers:
            tasks = list(self._running_workers.values())
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self.shutdown_grace_seconds,
            )
            self._running_workers.clear()
            self._token_to_run.clear()
            self._run_meta.clear()
        logger.info("TaskRunner stopped")

    async def _loop(self) -> None:
        """Periodic tick: dispatch_once every interval until stop."""
        while self._started and self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self._run_service.dispatch_once()
            except Exception:
                logger.exception("dispatch_once tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass
