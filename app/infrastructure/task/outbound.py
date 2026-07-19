"""T17: TaskOutboundDelivery -- feishu terminal-event delivery adapter.

Implements the ``TaskNotifier`` port (Domain). For each terminal event
delivered by ``TaskRunService``, this adapter:
  1. Reads ``task_notify_subs`` for the task from the registry.
  2. For each sub: skips if ``last_terminal_event_id >= terminal_event.id``
     (registry-backed idempotency watermark).
  3. Routes by ``platform``: ``feishu`` -> ``FeishuClient.send_markdown_reply``.
     Unknown or missing platform -> ``delivery_failed`` (no heuristic fallback).
  4. On successful delivery, advances the sub's ``last_terminal_event_id`` via
     ``registry.update_notify_sub_last_event``.

Design notes (spec):
  - Idempotency key is ``(terminal_event_id, platform, chat_id, thread_id)``,
    persisted in the registry (``task_notify_subs.last_terminal_event_id``).
    The adapter MUST NOT rely on a process-local set.
  - Unknown / missing platform returns ``delivery_failed`` and does NOT change
    the Task's terminal state.
  - Feishu client failure returns ``delivery_failed``; the watermark is NOT
    advanced, so the same terminal event can be retried later.
  - The adapter never modifies the Task aggregate itself.

Architecture boundary: this module imports ``app.domain`` (for the port and
value objects) and ``FeishuClient`` (infrastructure). It does NOT import
``app.application``.
"""
from __future__ import annotations

import logging
from typing import Any

from app.domain.task import (
    DeliveryResult,
    Task,
    TaskEvent,
    TaskRunOutcome,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal outcomes that should be notified (mirror TaskRunService _NOTIFIED_OUTCOMES)
# ---------------------------------------------------------------------------

_NOTIFIED_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.COMPLETED,
    TaskRunOutcome.WAITING_APPROVAL,
    TaskRunOutcome.FAILED,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.TERMINATED,
    TaskRunOutcome.EXPIRED,
})


# ---------------------------------------------------------------------------
# TaskOutboundDelivery
# ---------------------------------------------------------------------------


class TaskOutboundDelivery:
    """Feishu delivery adapter implementing the TaskNotifier port.

    Injection:
      - ``feishu_client``: FeishuClient (or None if feishu is not configured).
        When None, feishu subs return ``delivery_failed`` with an explanatory
        error.
      - ``registry``: TaskRegistry -- used for ``list_notify_subs`` and
        ``update_notify_sub_last_event`` (idempotency watermark).
    """

    def __init__(self, feishu_client: Any | None, registry: Any):
        self.feishu_client = feishu_client
        self.registry = registry

    async def deliver(
        self,
        task: Task,
        terminal_event: TaskEvent,
    ) -> DeliveryResult:
        """Deliver terminal event to all matching subs.

        Iterates all subs for ``task.id``. For each sub:
          - If ``last_terminal_event_id >= terminal_event.id``: skip (idempotent).
          - Else route by platform: feishu -> FeishuClient.send_markdown_reply,
            unknown/missing -> delivery_failed.
          - On successful feishu delivery: advance the sub's watermark via
            ``registry.update_notify_sub_last_event``.

        Returns ``DeliveryResult(delivered=True)`` if at least one sub was
        delivered OR no subs exist; returns ``DeliveryResult(delivered=False,
        error=...)`` if any sub failed (partial delivery still advances
        successful subs' watermarks).
        """
        try:
            subs = await self.registry.list_notify_subs(task.id)
        except Exception as exc:
            logger.warning(
                "list_notify_subs failed for task %s: %s", task.id, exc,
            )
            return DeliveryResult(delivered=False, error=str(exc))

        if not subs:
            # No subs: success (nothing to deliver)
            return DeliveryResult(delivered=True)

        content = self._build_content(task, terminal_event)
        event_id = terminal_event.id
        any_failed = False
        failure_reasons: list[str] = []
        any_delivered = False

        for sub in subs:
            platform = sub.get("platform", "") or ""
            chat_id = sub.get("chat_id", "") or ""
            thread_id = sub.get("thread_id")
            last_event_id = int(sub.get("last_terminal_event_id", 0) or 0)

            # Idempotency check: skip if already delivered this or a later event
            if last_event_id >= event_id:
                any_delivered = True  # Considered success (idempotent)
                continue

            # Route by platform
            outcome = await self._deliver_single(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                content=content,
            )
            if outcome.delivered:
                # Advance watermark
                try:
                    await self.registry.update_notify_sub_last_event(
                        task_id=task.id,
                        platform=platform,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        last_terminal_event_id=event_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "update_notify_sub_last_event failed for task=%s "
                        "platform=%s chat_id=%s: %s",
                        task.id, platform, chat_id, exc,
                    )
                    # The delivery itself succeeded; watermark update is best-effort.
                    # We do NOT mark this as failed -- the message was sent.
                any_delivered = True
            else:
                any_failed = True
                if outcome.error:
                    failure_reasons.append(
                        f"{platform}/{chat_id}: {outcome.error}"
                    )

        if any_failed and not any_delivered:
            return DeliveryResult(
                delivered=False,
                error="; ".join(failure_reasons) if failure_reasons else "delivery_failed",
            )
        # Partial success: still consider delivered=True; failures are logged.
        # The Task terminal state is NOT changed regardless.
        if any_failed:
            logger.warning(
                "partial delivery for task %s event %s: %s",
                task.id, event_id, "; ".join(failure_reasons),
            )
        return DeliveryResult(delivered=True)

    # ------------------------------------------------------------------
    # Per-sub routing
    # ------------------------------------------------------------------

    async def _deliver_single(
        self,
        *,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        content: str,
    ) -> DeliveryResult:
        """Deliver to a single sub by platform. Returns DeliveryResult."""
        if not platform:
            return DeliveryResult(
                delivered=False, error="missing platform",
            )
        if platform == "feishu":
            return await self._deliver_feishu(chat_id, thread_id, content)
        return DeliveryResult(
            delivered=False,
            error=f"unsupported platform: {platform}",
        )

    async def _deliver_feishu(
        self,
        chat_id: str,
        thread_id: str | None,
        content: str,
    ) -> DeliveryResult:
        """Deliver via FeishuClient.send_markdown_reply."""
        if self.feishu_client is None:
            return DeliveryResult(
                delivered=False, error="feishu client not configured",
            )
        if not chat_id:
            return DeliveryResult(
                delivered=False, error="feishu delivery requires chat_id",
            )
        # FeishuClient.send_markdown_reply(receive_id, content, receive_id_type)
        # thread_id, when present, becomes the receive_id (feishu thread/chat id).
        # When absent, chat_id is the receive_id with receive_id_type=chat_id.
        receive_id = thread_id or chat_id
        receive_id_type = "chat_id"
        try:
            await self.feishu_client.send_markdown_reply(
                receive_id, content, receive_id_type,
            )
        except Exception as exc:
            logger.warning(
                "feishu delivery failed for chat_id=%s thread_id=%s: %s",
                chat_id, thread_id, exc,
            )
            return DeliveryResult(delivered=False, error=str(exc))
        return DeliveryResult(delivered=True)

    # ------------------------------------------------------------------
    # Content construction
    # ------------------------------------------------------------------

    def _build_content(self, task: Task, terminal_event: TaskEvent) -> str:
        """Build markdown content for the notification message.

        Includes task title, terminal status, and result summary. Keeps the
        content concise -- feishu messages should be readable in IM.
        """
        outcome = terminal_event.payload.get("outcome", "unknown")
        lines: list[str] = []
        lines.append(f"# 任务: {task.title}")
        lines.append("")
        lines.append(f"**状态**: {task.status.value}")
        lines.append(f"**结果**: {outcome}")
        if task.result:
            # Cap result length for IM readability
            result = task.result
            if len(result) > 1000:
                result = result[:1000] + "…"
            lines.append("")
            lines.append(f"**摘要**:\n{result}")
        lines.append("")
        lines.append(f"Task ID: `{task.id}`")
        return "\n".join(lines)
