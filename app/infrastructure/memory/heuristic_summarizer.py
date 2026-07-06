from __future__ import annotations

from typing import Any

from app.utils.content_utils import extract_text


class HeuristicSummarizer:
    def __init__(self, max_chars: int = 4000):
        self.max_chars = max_chars

    async def summarize(self, messages: list[dict[str, Any]], existing_summary: str = "") -> str:
        text = "\n".join(
            f"{message.get('role', 'unknown')}: {extract_text(message.get('content', ''))}"
            for message in messages
        )
        if len(text) > self.max_chars:
            return "heuristic summary: " + text[-self.max_chars :]
        user_messages = [
            message
            for message in messages
            if message.get("role") == "user"
            and extract_text(message.get("content", ""))
        ]
        assistant_messages = [
            message
            for message in messages
            if message.get("role") == "assistant"
            and extract_text(message.get("content", ""))
        ]
        if not user_messages and not assistant_messages:
            return existing_summary
        parts: list[str] = []
        if user_messages:
            parts.append("用户: " + extract_text(user_messages[0].get("content", ""))[:200])
        if assistant_messages:
            parts.append("助手: " + extract_text(assistant_messages[-1].get("content", ""))[:200])
        return " | ".join(parts)
