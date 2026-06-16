from __future__ import annotations

from dataclasses import dataclass

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.domain.schedule import PromptSafetyScanner, ScheduledTask, ScheduledTaskExecutionStatus


@dataclass(frozen=True)
class ScheduledAgentResult:
    status: ScheduledTaskExecutionStatus
    output: str | None = None
    error: str | None = None


class ScheduledAgentExecutor:
    def __init__(self, chat_service: ChatCompletionService, scanner: PromptSafetyScanner):
        self.chat_service = chat_service
        self.scanner = scanner

    async def run(self, task: ScheduledTask) -> ScheduledAgentResult:
        safety = self.scanner.scan(task.prompt)
        if not safety.allowed:
            return ScheduledAgentResult(ScheduledTaskExecutionStatus.BLOCKED, error=safety.reason)
        result = await self.chat_service.complete(
            ChatCompletionInput(
                model="N-Agent",
                messages=[{"role": "user", "content": task.prompt}],
                stream=False,
                session_id=task.session_id,
                options={"execution_context_mode": "unattended", "tool_exposure_policy": "safe_only"},
            )
        )
        assert isinstance(result, ChatCompletionResult)
        if result.finish_reason == "error":
            return ScheduledAgentResult(ScheduledTaskExecutionStatus.FAILED, error=str(result.message.get("content", "")))
        return ScheduledAgentResult(ScheduledTaskExecutionStatus.SUCCEEDED, output=str(result.message.get("content", "")))
