"""T17: TaskOutboundDelivery tests.

Tests for feishu terminal-event delivery with idempotency keyed on
``(terminal_event_id, platform, chat_id, thread_id)`` and registry-backed
``last_terminal_event_id`` watermark. Unknown platforms are delivery_failed
(no heuristic fallback); delivery failures do not change Task terminal
state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.domain.task import (
    DeliveryResult,
    Task,
    TaskEvent,
    TaskRunOutcome,
    TaskStatus,
)
from app.infrastructure.task.outbound import TaskOutboundDelivery


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeFeishuClient:
    """Captures send_markdown_reply calls."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def send_markdown_reply(
        self, receive_id: str, content: str, receive_id_type: str = "chat_id"
    ) -> None:
        self.calls.append({
            "receive_id": receive_id,
            "content": content,
            "receive_id_type": receive_id_type,
        })
        if self._fail:
            raise RuntimeError("feishu delivery failure")


class FakeRegistry:
    """In-memory registry with notify_subs + last_terminal_event_id."""

    def __init__(self):
        self._subs: list[dict[str, Any]] = []
        self.list_calls: list[str] = []
        self.update_calls: list[tuple] = []

    async def list_notify_subs(self, task_id: str) -> tuple[dict[str, Any], ...]:
        self.list_calls.append(task_id)
        return tuple({**s} for s in self._subs if s["task_id"] == task_id)

    async def update_notify_sub_last_event(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        last_terminal_event_id: int,
    ) -> bool:
        tid = thread_id  # Registry stores None natively in fake
        for s in self._subs:
            if (
                s["task_id"] == task_id
                and s["platform"] == platform
                and s["chat_id"] == chat_id
                and (s["thread_id"] or None) == (tid or None)
            ):
                if s["last_terminal_event_id"] < last_terminal_event_id:
                    s["last_terminal_event_id"] = last_terminal_event_id
                    self.update_calls.append(
                        (task_id, platform, chat_id, thread_id, last_terminal_event_id)
                    )
                    return True
                return False
        return False

    def add_sub(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
        last_terminal_event_id: int = 0,
    ) -> None:
        self._subs.append({
            "task_id": task_id,
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "last_terminal_event_id": last_terminal_event_id,
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "t_1",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.DONE,
    result: str = "task completed successfully",
) -> Task:
    return Task(
        id=task_id,
        title=title,
        status=status,
        result=result,
        created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        version=2,
    )


def _terminal_event(event_id: int = 1, task_id: str = "t_1") -> TaskEvent:
    return TaskEvent(
        id=event_id,
        task_id=task_id,
        kind="finished",
        payload={"outcome": TaskRunOutcome.COMPLETED.value},
        run_id=1,
        created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def feishu_client():
    return FakeFeishuClient()


@pytest.fixture
def registry():
    return FakeRegistry()


@pytest.fixture
def delivery(feishu_client, registry):
    return TaskOutboundDelivery(feishu_client=feishu_client, registry=registry)


# ---------------------------------------------------------------------------
# Feishu delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_feishu(delivery, feishu_client, registry):
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    result = await delivery.deliver(_task(), _terminal_event(event_id=10))
    assert isinstance(result, DeliveryResult)
    assert result.delivered is True
    assert len(feishu_client.calls) == 1
    call = feishu_client.calls[0]
    assert call["receive_id"] == "chat-1"
    assert call["receive_id_type"] == "chat_id"
    # Idempotency cursor advanced
    subs = await registry.list_notify_subs("t_1")
    assert subs[0]["last_terminal_event_id"] == 10


@pytest.mark.asyncio
async def test_deliver_feishu_with_thread_id(delivery, feishu_client, registry):
    registry.add_sub(
        "t_1", platform="feishu", chat_id="chat-1", thread_id="th-1"
    )
    result = await delivery.deliver(_task(), _terminal_event(event_id=5))
    assert result.delivered is True
    assert len(feishu_client.calls) == 1


@pytest.mark.asyncio
async def test_deliver_content_includes_title_and_summary(
    delivery, feishu_client, registry
):
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    await delivery.deliver(
        _task(title="调研架构", result="产出报告.md"),
        _terminal_event(event_id=1, task_id="t_1"),
    )
    content = feishu_client.calls[0]["content"]
    assert "调研架构" in content
    assert "产出报告.md" in content
    # Terminal status mentioned
    assert "done" in content.lower() or "completed" in content.lower()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_idempotent_by_terminal_event_id(
    delivery, feishu_client, registry
):
    """Same terminal_event_id retried -> not delivered twice."""
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    event = _terminal_event(event_id=42)
    result1 = await delivery.deliver(_task(), event)
    result2 = await delivery.deliver(_task(), event)
    assert result1.delivered is True
    assert result2.delivered is True  # Idempotent success (no-op)
    assert len(feishu_client.calls) == 1  # Only first call delivered


@pytest.mark.asyncio
async def test_deliver_advances_watermark_monotonically(
    delivery, feishu_client, registry
):
    """Later terminal_event_id supersedes earlier; earlier is no-op."""
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    # Deliver event 10
    await delivery.deliver(_task(), _terminal_event(event_id=10, task_id="t_1"))
    assert len(feishu_client.calls) == 1
    # Retry event 10 -> no-op
    await delivery.deliver(_task(), _terminal_event(event_id=10, task_id="t_1"))
    assert len(feishu_client.calls) == 1
    # New event 15 -> delivered
    await delivery.deliver(_task(), _terminal_event(event_id=15, task_id="t_1"))
    assert len(feishu_client.calls) == 2
    # Retry event 10 (older) -> no-op (watermark is 15)
    await delivery.deliver(_task(), _terminal_event(event_id=10, task_id="t_1"))
    assert len(feishu_client.calls) == 2


@pytest.mark.asyncio
async def test_deliver_per_sub_idempotency(delivery, feishu_client, registry):
    """Idempotency is per (platform, chat_id, thread_id), not per task."""
    registry.add_sub("t_1", platform="feishu", chat_id="chat-A")
    registry.add_sub("t_1", platform="feishu", chat_id="chat-B")
    event = _terminal_event(event_id=7, task_id="t_1")
    await delivery.deliver(_task(), event)
    # Both subs received the delivery
    assert len(feishu_client.calls) == 2
    receive_ids = {c["receive_id"] for c in feishu_client.calls}
    assert receive_ids == {"chat-A", "chat-B"}
    # Retry -> neither re-delivers
    await delivery.deliver(_task(), event)
    assert len(feishu_client.calls) == 2


# ---------------------------------------------------------------------------
# Unknown platform / missing platform
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_platform_no_fallback(delivery, feishu_client, registry):
    registry.add_sub("t_1", platform="slack", chat_id="slack-chan")
    result = await delivery.deliver(_task(), _terminal_event(event_id=1))
    assert result.delivered is False
    assert result.error is not None
    assert "unsupported" in result.error.lower() or "unknown" in result.error.lower()
    # No feishu call
    assert feishu_client.calls == []


@pytest.mark.asyncio
async def test_missing_platform_no_fallback(delivery, feishu_client, registry):
    """Sub with missing/empty platform -> delivery_failed."""
    registry.add_sub("t_1", platform="", chat_id="orphan")
    result = await delivery.deliver(_task(), _terminal_event(event_id=1))
    assert result.delivered is False
    assert result.error is not None
    assert feishu_client.calls == []


# ---------------------------------------------------------------------------
# Feishu client failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feishu_client_failure_marks_failed(delivery, registry):
    """Feishu client raises -> delivery_failed; cursor NOT advanced."""
    failing = FakeFeishuClient(fail=True)
    delivery = TaskOutboundDelivery(feishu_client=failing, registry=registry)
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    result = await delivery.deliver(_task(), _terminal_event(event_id=3))
    assert result.delivered is False
    assert result.error is not None
    # Idempotency cursor NOT advanced (delivery failed)
    subs = await registry.list_notify_subs("t_1")
    assert subs[0]["last_terminal_event_id"] == 0


@pytest.mark.asyncio
async def test_feishu_client_not_configured(delivery, registry):
    """No feishu client -> delivery_failed for feishu subs."""
    delivery = TaskOutboundDelivery(feishu_client=None, registry=registry)
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    result = await delivery.deliver(_task(), _terminal_event(event_id=1))
    assert result.delivered is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# No subs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subs_returns_success(delivery, feishu_client, registry):
    """No subs for task -> returns delivered=True (no-op success)."""
    result = await delivery.deliver(_task(), _terminal_event(event_id=1))
    # No subs to deliver to -> considered success (nothing failed)
    assert result.delivered is True
    assert feishu_client.calls == []


# ---------------------------------------------------------------------------
# Does not change Task terminal state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_failure_does_not_change_task_state(
    delivery, feishu_client, registry
):
    """Delivery success or failure does NOT modify the Task's terminal state.

    The Task passed in is immutable; the delivery only writes to
    task_notify_subs.last_terminal_event_id.
    """
    failing = FakeFeishuClient(fail=True)
    delivery = TaskOutboundDelivery(feishu_client=failing, registry=registry)
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    task = _task(status=TaskStatus.DONE, result="done")
    original_status = task.status
    original_result = task.result
    await delivery.deliver(task, _terminal_event(event_id=1))
    # Task fields unchanged
    assert task.status == original_status
    assert task.result == original_result


# ---------------------------------------------------------------------------
# Multiple subs mixed outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_subs_mixed_platforms(delivery, feishu_client, registry):
    """One feishu sub + one unknown platform -> partial delivery."""
    registry.add_sub("t_1", platform="feishu", chat_id="chat-A")
    registry.add_sub("t_1", platform="slack", chat_id="slack-B")
    result = await delivery.deliver(_task(), _terminal_event(event_id=1))
    # Overall success: feishu delivered, unknown marked failed but does not
    # block the rest. Result reflects whether ALL subs succeeded.
    assert feishu_client.calls == [
        {"receive_id": "chat-A", "content": feishu_client.calls[0]["content"], "receive_id_type": "chat_id"}
    ]
    # Feishu sub advanced; slack sub not advanced (it had no successful delivery)
    subs = {s["platform"]: s for s in await registry.list_notify_subs("t_1")}
    assert subs["feishu"]["last_terminal_event_id"] == 1
    assert subs["slack"]["last_terminal_event_id"] == 0


# ---------------------------------------------------------------------------
# Uses registry (not in-process set) for idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_uses_registry_not_in_memory(delivery, feishu_client, registry):
    """Idempotency state persists in the registry; a fresh delivery instance
    reads the same watermark and does NOT re-deliver."""
    registry.add_sub("t_1", platform="feishu", chat_id="chat-1")
    event = _terminal_event(event_id=99, task_id="t_1")
    await delivery.deliver(_task(), event)
    # Recreate the delivery instance (no shared in-memory state)
    new_feishu = FakeFeishuClient()
    new_delivery = TaskOutboundDelivery(feishu_client=new_feishu, registry=registry)
    result = await new_delivery.deliver(_task(), event)
    assert result.delivered is True
    # No re-delivery -- registry's watermark blocks it
    assert new_feishu.calls == []
