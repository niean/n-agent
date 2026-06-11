from __future__ import annotations

from typing import Any


class HeuristicSummarizer:
    def __init__(self, max_chars: int = 4000):
        self.max_chars = max_chars

    async def summarize(self, messages: list[dict[str, Any]], existing_summary: str = "") -> str:
        text = "\n".join(f"{message.get('role', 'unknown')}: {message.get('content', '')}" for message in messages)
        if len(text) <= self.max_chars:
            return existing_summary
        return "heuristic summary: " + text[-self.max_chars :]
