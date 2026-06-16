import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.scheduled_agent_executor import ScheduledAgentExecutor
from app.domain.schedule import DeliveryTarget, PromptSafetyResult, ScheduledTask, ScheduledTaskExecutionStatus, ScheduleExpression, ScheduleTimezone
from datetime import datetime, timezone


def _task():
    return ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("* * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=DeliveryTarget.dashboard(),
        next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )


class FakeScanner:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def scan(self, prompt):
        return PromptSafetyResult(self.allowed, "blocked prompt" if not self.allowed else "")


class FakeChatService:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ChatCompletionResult("session-1", "N-Agent", {"role": "assistant", "content": "done"})


@pytest.mark.asyncio
async def test_executor_uses_unattended_safe_only_options():
    chat = FakeChatService()
    result = await ScheduledAgentExecutor(chat, FakeScanner()).run(_task())

    assert result.status is ScheduledTaskExecutionStatus.SUCCEEDED
    assert chat.requests[0].options == {"execution_context_mode": "unattended", "tool_exposure_policy": "safe_only"}
    assert chat.requests[0].session_id == "session-1"


@pytest.mark.asyncio
async def test_executor_blocks_without_calling_chat_service():
    chat = FakeChatService()
    result = await ScheduledAgentExecutor(chat, FakeScanner(False)).run(_task())

    assert result.status is ScheduledTaskExecutionStatus.BLOCKED
    assert result.error == "blocked prompt"
    assert chat.requests == []
