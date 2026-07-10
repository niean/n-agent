from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


CONTEXT_SUMMARY_PREFIX = "[CONTEXT SUMMARY]: "


@dataclass(frozen=True)
class ContextCompressionResult:
    messages: list[dict[str, Any]]
    summary: str
    compressed: bool
    skipped_reason: str | None
    original_tokens: int | None
    compressed_tokens: int | None


class ContextEngine(Protocol):
    def should_compress(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_tokens: int | None = None,
        force: bool = False,
    ) -> bool: ...

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        current_tokens: int | None = None,
        force: bool = False,
        existing_summary: str = "",
    ) -> ContextCompressionResult: ...
