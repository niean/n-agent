from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.policy_snapshot import IngressFacts
from app.domain.policy import ExecutionMode
from app.domain.schedule import PromptSafetyScanner, ScheduledTask, ScheduledTaskExecutionStatus

SCHEDULED_EXECUTION_PROMPT = (
    "你正在执行 N-Agent 定时任务。只需完成任务并把应通知用户的内容作为最终回答返回；"
    "不要声称缺少飞书 IM、Webhook 或发送工具，也不要尝试自行发送飞书消息。"
    "系统会在你返回最终回答后自动投递到已配置的目标，包括飞书 home chat。"
)


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
        granted_tools = list(task.execution_policy.allowed_tools)
        result = await self.chat_service.complete(
            ChatCompletionInput(
                model="N-Agent",
                messages=[
                    {"role": "system", "content": SCHEDULED_EXECUTION_PROMPT},
                    {"role": "user", "content": task.prompt},
                ],
                stream=False,
                session_id=task.session_id,
                ingress_facts=IngressFacts(
                    run_id=f"schedule-{uuid4()}",
                    session_id=task.session_id,
                    source="schedule",
                    actor_id=None,
                    execution_mode=ExecutionMode.UNATTENDED,
                    trusted_claims={
                        "execution_mode": ExecutionMode.UNATTENDED.value,
                        "granted_tools": granted_tools,
                    },
                ),
                # Schedule ingress facts: pass execution_mode as a structured
                # trusted_metadata field (not a free options dict).  The
                # ChatCompletionService derives tool_exposure_policy=safe_only
                # from execution_mode=unattended. granted_tools carries the
                # task-level tool grants so SAFE tools like host_terminal are
                # exposed to the unattended run despite their AGENT source_type.
                trusted_metadata={
                    "execution_mode": ExecutionMode.UNATTENDED.value,
                    "granted_tools": granted_tools,
                },
            )
        )
        assert isinstance(result, ChatCompletionResult)
        if result.finish_reason == "error":
            return ScheduledAgentResult(ScheduledTaskExecutionStatus.FAILED, error=str(result.message.get("content", "")))
        return ScheduledAgentResult(ScheduledTaskExecutionStatus.SUCCEEDED, output=str(result.message.get("content", "")))
