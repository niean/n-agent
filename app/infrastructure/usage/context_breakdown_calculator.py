# app/infrastructure/usage/context_breakdown_calculator.py
from __future__ import annotations

import json
from typing import Any

from app.domain.usage import ContextBreakdown


def _estimate_text_tokens(text: str) -> int:
    # heuristic: ~4 chars per token; empty/None text contributes 0 tokens
    if not text:
        return 0
    return max(1, len(text) // 4)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content", "")
    if isinstance(content, str):
        return _estimate_text_tokens(content) + 4  # role + framing overhead
    if isinstance(content, list):
        total = 4
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += _estimate_text_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    total += 1500  # image estimate
        return total
    return 4


def _estimate_tool_definition_tokens(tools: list[dict[str, Any]]) -> int:
    if not tools:
        return 0
    # serialize to JSON and estimate
    return _estimate_text_tokens(json.dumps(tools, ensure_ascii=False))


class ContextBreakdownCalculatorImpl:
    def compute(
        self, system_prompt: str, tool_definitions: list[dict],
        messages: list[dict], external_memory_block: str,
    ) -> ContextBreakdown:
        sp_tokens = _estimate_text_tokens(system_prompt)
        tool_tokens = _estimate_tool_definition_tokens(tool_definitions)
        mem_tokens = _estimate_text_tokens(external_memory_block)
        conv_tokens = sum(_estimate_message_tokens(m) for m in messages)
        return ContextBreakdown(
            system_prompt=sp_tokens,
            tool_definitions=tool_tokens,
            memory=mem_tokens,
            conversation=conv_tokens,
        )
