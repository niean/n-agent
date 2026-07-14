import pytest

from app.application.chat_service import ChatCompletionResult
from app.application.scheduled_agent_executor import SCHEDULED_EXECUTION_PROMPT, ScheduledAgentExecutor
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
async def test_executor_passes_unattended_via_trusted_metadata():
    """T11: executor passes execution_mode via trusted_metadata, not free options dict."""
    chat = FakeChatService()
    result = await ScheduledAgentExecutor(chat, FakeScanner()).run(_task())

    assert result.status is ScheduledTaskExecutionStatus.SUCCEEDED
    # execution_mode is in trusted_metadata (structured), not options (free dict)
    assert chat.requests[0].trusted_metadata.get("execution_mode") == "unattended"
    assert "execution_context_mode" not in chat.requests[0].options
    assert "tool_exposure_policy" not in chat.requests[0].options
    assert chat.requests[0].session_id == "session-1"
    assert chat.requests[0].messages[0] == {"role": "system", "content": SCHEDULED_EXECUTION_PROMPT}
    assert chat.requests[0].messages[1] == {"role": "user", "content": "summarize"}


@pytest.mark.asyncio
async def test_executor_blocks_without_calling_chat_service():
    chat = FakeChatService()
    result = await ScheduledAgentExecutor(chat, FakeScanner(False)).run(_task())

    assert result.status is ScheduledTaskExecutionStatus.BLOCKED
    assert result.error == "blocked prompt"
    assert chat.requests == []
