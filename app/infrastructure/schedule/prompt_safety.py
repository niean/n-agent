from __future__ import annotations

from app.domain.schedule import PromptSafetyResult


class DeterministicPromptSafetyScanner:
    def scan(self, prompt: str) -> PromptSafetyResult:
        lowered = prompt.lower()
        blocked_patterns = (
            ("confirm", "confirm permission bypass"),
            ("绕过", "permission bypass"),
            ("bypass", "permission bypass"),
            ("api key", "secret leakage"),
            ("secret", "secret leakage"),
            ("密钥", "secret leakage"),
            ("泄露", "secret leakage"),
            ("scheduler", "scheduler self modification"),
            ("任务", "scheduler self modification"),
            ("recurring job", "scheduler self modification"),
        )
        for pattern, reason in blocked_patterns:
            if pattern in lowered:
                return PromptSafetyResult(False, reason)
        return PromptSafetyResult(True)
