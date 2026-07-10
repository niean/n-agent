from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextCompressionResult, ContextEngine
from app.domain.memory import Summarizer
from app.domain.provider import LLMProvider, LLMResult

logger = logging.getLogger(__name__)


_SUMMARY_PROMPT_TEMPLATE_FIRST = """你是一个对话摘要助手。请将以下对话历史压缩为结构化摘要，用于后续对话的上下文恢复。

待压缩对话：
{turns_to_summarize}

请输出以下结构（中文），每节不超过 3 行：
## 目标
用户想完成什么

## 进展
已完成的关键步骤

## 决策
已确定的关键决策

## 文件
涉及的文件或资源

## 待办
未完成的工作
"""

_SUMMARY_PROMPT_TEMPLATE_ITERATIVE = """你是一个对话摘要助手。请基于已有摘要，把新增对话整合进摘要，用于后续对话的上下文恢复。

PREVIOUS SUMMARY:
{existing_summary}

NEW TURNS TO INCORPORATE:
{turns_to_summarize}

请输出整合后的完整摘要（中文），每节不超过 3 行：
## 目标
用户想完成什么

## 进展
已完成的关键步骤

## 决策
已确定的关键决策

## 文件
涉及的文件或资源

## 待办
未完成的工作
"""


class ContextCompressor(ContextEngine):
    def __init__(
        self,
        llm_provider: LLMProvider,
        model: str | Callable[[], str],
        context_length: int,
        threshold_percent: float,
        protect_first_n: int,
        protect_last_n: int,
        summary_target_ratio: float,
        cooldown_seconds: int,
        fallback_summarizer: Summarizer | None = None,
        _clock: Callable[[], float] = time.monotonic,
    ):
        self.llm_provider = llm_provider
        self._model = model
        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = summary_target_ratio
        self.cooldown_seconds = cooldown_seconds
        self.fallback_summarizer = fallback_summarizer
        self._clock = _clock
        self._last_compressed_at: float | None = None

    def _resolve_model(self) -> str:
        return self._model() if callable(self._model) else self._model

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            try:
                content = msg.get("content", "")
                if isinstance(content, str):
                    total += len(content) // 4 + 10
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "text":
                                total += len(part.get("text", "")) // 4
                            elif part.get("type") == "image_url":
                                total += 1500
                            else:
                                total += len(json.dumps(part, ensure_ascii=False, sort_keys=True, default=str)) // 4
                    total += 10
                else:
                    total += len(json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)) // 4 + 10
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    total += len(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True, default=str)) // 4
                for key in ("name", "tool_call_id"):
                    val = msg.get(key)
                    if val:
                        total += len(json.dumps(val, ensure_ascii=False, sort_keys=True, default=str)) // 4
            except Exception:
                # Spec: a bad single message must not abort compression checks.
                continue
        return total

    def _compute_threshold_tokens(self) -> int:
        return max(1, int(self.context_length * self.threshold_percent))

    def _in_cooldown(self) -> bool:
        if self._last_compressed_at is None:
            return False
        return (self._clock() - self._last_compressed_at) < self.cooldown_seconds

    def _record_compression_success(self) -> None:
        self._last_compressed_at = self._clock()

    def should_compress(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_tokens: int | None = None,
        force: bool = False,
    ) -> bool:
        if force:
            return True
        if self._in_cooldown():
            return False
        try:
            tokens = prompt_tokens if prompt_tokens is not None else self._estimate_tokens(messages)
        except Exception:
            logger.warning("context_compressor: token estimation failed")
            return False
        return tokens > self._compute_threshold_tokens()

    def _find_latest_context_summary(
        self, messages: list[dict[str, Any]],
    ) -> tuple[int, str] | None:
        """在 messages(list[dict]) 里从后往前查找最后一个 role='user' 且 content 为 str
        且以 CONTEXT_SUMMARY_PREFIX 开头的消息，返回 (idx, body)。
        body 是剥离前缀并 strip 后的纯摘要文本；找不到返回 None。
        """
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            if content.startswith(CONTEXT_SUMMARY_PREFIX):
                body = content[len(CONTEXT_SUMMARY_PREFIX):].strip()
                return (idx, body)
        return None

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        current_tokens: int | None = None,
        force: bool = False,
        existing_summary: str = "",
    ) -> ContextCompressionResult:
        original_tokens = current_tokens if current_tokens is not None else self._estimate_tokens(messages)
        # Cooldown check (only when not forced)
        if not force and self._in_cooldown():
            return ContextCompressionResult(
                messages=messages, summary=existing_summary, compressed=False,
                skipped_reason="cooldown",
                original_tokens=original_tokens, compressed_tokens=None,
            )
        # Threshold check (only when not forced)
        if not force and not self.should_compress(
            messages, prompt_tokens=original_tokens, force=False,
        ):
            return ContextCompressionResult(
                messages=messages, summary=existing_summary, compressed=False,
                skipped_reason="below_threshold",
                original_tokens=original_tokens, compressed_tokens=None,
            )
        if len(messages) <= self.protect_first_n + self.protect_last_n:
            return ContextCompressionResult(
                messages=messages, summary=existing_summary, compressed=False,
                skipped_reason="too_few_messages",
                original_tokens=original_tokens, compressed_tokens=None,
            )
        try:
            # Three-segment: head + middle(to summarize) + tail
            head_end = self._align_boundary_forward(messages, self.protect_first_n)
            tail_budget = max(1, int(self.context_length * self.summary_target_ratio))
            tail_start = self._find_tail_cut_by_tokens(messages, head_end, tail_budget)
            tail_start = self._align_boundary_backward(messages, tail_start)
            if tail_start <= head_end:
                return ContextCompressionResult(
                    messages=messages, summary=existing_summary, compressed=False,
                    skipped_reason="too_few_messages",
                    original_tokens=original_tokens, compressed_tokens=None,
                )
            # 增量压缩：定位上次摘要
            found = self._find_latest_context_summary(messages)
            if found is not None:
                summary_idx, body = found
            else:
                summary_idx, body = None, ""

            # middle 4 分支
            if summary_idx is None or summary_idx < head_end:
                # 首次路径：middle 从 head_end 开始，previous_summary 为空
                middle = self._prune_old_tool_results(messages[head_end:tail_start], protect_tail_count=0)
                previous_summary = ""
            elif summary_idx >= tail_start - 1:
                # 上次摘要 in tail 保护范围：跳过
                return ContextCompressionResult(
                    messages=messages, summary=existing_summary, compressed=False,
                    skipped_reason="summary_in_tail",
                    original_tokens=original_tokens, compressed_tokens=None,
                )
            else:
                # 正常增量：middle 从 summary_idx+1 开始
                middle = self._prune_old_tool_results(
                    messages[summary_idx + 1:tail_start], protect_tail_count=0,
                )
                previous_summary = body

            if not middle:
                return ContextCompressionResult(
                    messages=messages, summary=existing_summary, compressed=False,
                    skipped_reason="too_few_messages",
                    original_tokens=original_tokens, compressed_tokens=None,
                )

            head = messages[:head_end]
            tail = messages[tail_start:]
            summary = await self._generate_summary(middle, existing_summary=previous_summary)
            if summary is None:
                summary = await self._build_fallback_summary(middle, existing_summary=previous_summary)
            if not summary:
                summary = previous_summary or self._build_static_fallback_summary(middle)
            combined = self._insert_summary_message(head, summary, tail)
            # IMPORTANT: Check tool_boundary BEFORE too_few_messages.
            # A single orphan tool message is a protocol violation (would cause 400),
            # which is a tool_boundary issue, not a too_few issue.
            if self._has_orphan_tool_messages(combined):
                return ContextCompressionResult(
                    messages=messages, summary=existing_summary, compressed=False,
                    skipped_reason="tool_boundary",
                    original_tokens=original_tokens, compressed_tokens=None,
                )
            if not combined or len([m for m in combined if m.get("role") != "system"]) <= 1:
                return ContextCompressionResult(
                    messages=messages, summary=existing_summary, compressed=False,
                    skipped_reason="too_few_messages",
                    original_tokens=original_tokens, compressed_tokens=None,
                )
            compressed_tokens = self._estimate_tokens(combined)
            self._record_compression_success()
            return ContextCompressionResult(
                messages=combined, summary=summary, compressed=True,
                skipped_reason=None,
                original_tokens=original_tokens, compressed_tokens=compressed_tokens,
            )
        except Exception as exc:
            logger.error("context_compressor: compress failed: %s", exc)
            return ContextCompressionResult(
                messages=messages, summary=existing_summary, compressed=False,
                skipped_reason="error",
                original_tokens=original_tokens, compressed_tokens=None,
            )

    def _find_tail_cut_by_tokens(
        self, messages: list[dict[str, Any]], head_end: int, token_budget: int,
    ) -> int:
        protected_start = max(head_end, len(messages) - self.protect_last_n)
        tail_start = len(messages)
        used = 0
        # Walk backward from end, but never consider messages[head_end] for the
        # tail: middle must keep at least one message to summarize.
        for idx in range(len(messages) - 1, head_end, -1):
            candidate_start = self._align_boundary_backward(messages, idx)
            candidate = messages[candidate_start:tail_start]
            candidate_tokens = self._estimate_tokens(candidate)
            if idx < protected_start and used + candidate_tokens > token_budget:
                break
            tail_start = candidate_start
            used = self._estimate_tokens(messages[tail_start:])
            if tail_start <= head_end:
                break
        # Guarantee at least one message in middle (tail_start > head_end).
        # If the walk placed everything in the tail, force the oldest tail
        # message back into middle. Align forward to avoid splitting a tool
        # group that starts at head_end.
        if tail_start <= head_end:
            tail_start = self._align_boundary_forward(messages, head_end + 1)
        return max(head_end + 1, tail_start)

    def _has_orphan_tool_messages(self, messages: list[dict[str, Any]]) -> bool:
        valid_ids: set[str] = set()
        for idx, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                _, end = self._tool_group_span(messages, idx)
                result_ids = {
                    m.get("tool_call_id")
                    for m in messages[idx + 1:end]
                    if m.get("role") == "tool"
                }
                call_ids = {tc.get("id") for tc in msg.get("tool_calls", []) if tc.get("id")}
                if not call_ids or not call_ids.issubset(result_ids):
                    return True
                valid_ids.update(call_ids)
            if msg.get("role") == "tool" and msg.get("tool_call_id") not in valid_ids:
                return True
        return False

    def _tool_group_span(self, messages: list[dict[str, Any]], idx: int) -> tuple[int, int]:
        """Return [start, end) span of the tool group containing idx. end exclusive."""
        if idx < 0 or idx >= len(messages):
            return (idx, idx)
        msg = messages[idx]
        tool_calls = msg.get("tool_calls")
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            for start in range(idx - 1, -1, -1):
                candidate_calls = messages[start].get("tool_calls") or []
                ids = {tc.get("id") for tc in candidate_calls if tc.get("id")}
                if tc_id in ids:
                    group_start, group_end = self._tool_group_span(messages, start)
                    if group_start <= idx < group_end:
                        return (group_start, group_end)
            return (idx, idx + 1)
        if not tool_calls:
            return (idx, idx + 1)
        ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
        end = idx + 1
        while end < len(messages):
            nxt = messages[end]
            if nxt.get("role") == "tool" and nxt.get("tool_call_id") in ids:
                end += 1
            else:
                break
        return (idx, end)

    def _align_boundary_backward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Move slice boundary idx backward if it would split a tool group."""
        if idx <= 0 or idx >= len(messages):
            return idx
        start, end = self._tool_group_span(messages, idx - 1)
        if start < idx < end:
            return start
        start, end = self._tool_group_span(messages, idx)
        if start < idx < end:
            return start
        return idx

    def _align_boundary_forward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Move slice boundary idx forward if it would split a tool group."""
        if idx < 0 or idx >= len(messages):
            return idx
        start, end = self._tool_group_span(messages, idx)
        # Only move when the span is an actual multi-message tool group (end - start > 1).
        # A single-message span (non-tool message or lone assistant without results) must not move.
        if start <= idx < end and end - start > 1:
            return end
        return idx

    def _sanitize_tool_pairs(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove orphan tool results and assistant tool_calls without contiguous results."""
        result = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                contiguous_tools = []
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    if messages[j].get("tool_call_id") in ids:
                        contiguous_tools.append(messages[j])
                    j += 1
                result_ids = {m.get("tool_call_id") for m in contiguous_tools}
                kept_calls = [tc for tc in msg["tool_calls"] if tc.get("id") in result_ids]
                if kept_calls:
                    new_msg = dict(msg)
                    new_msg["tool_calls"] = kept_calls
                    result.append(new_msg)
                    result.extend(contiguous_tools)
                else:
                    new_msg = dict(msg)
                    new_msg.pop("tool_calls", None)
                    if new_msg.get("content") or new_msg.get("role") != "assistant":
                        result.append(new_msg)
                i = j
            elif msg.get("role") == "tool":
                i += 1
            else:
                result.append(msg)
                i += 1
        return result

    def _prune_old_tool_results(
        self, messages: list[dict[str, Any]], protect_tail_count: int,
    ) -> list[dict[str, Any]]:
        """Truncate long tool_call arguments in non-tail messages."""
        result = []
        for i, msg in enumerate(messages):
            if i >= len(messages) - protect_tail_count:
                result.append(msg)
                continue
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                new_msg = dict(msg)
                new_calls = []
                for tc in msg["tool_calls"]:
                    new_tc = dict(tc)
                    fn = dict(tc.get("function", {}))
                    args = fn.get("arguments", "")
                    if isinstance(args, str) and len(args) > 500:
                        fn["arguments"] = args[:500]
                    new_tc["function"] = fn
                    new_calls.append(new_tc)
                new_msg["tool_calls"] = new_calls
                result.append(new_msg)
            elif msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > 500:
                new_msg = dict(msg)
                new_msg["content"] = msg["content"][:500]
                result.append(new_msg)
            else:
                result.append(msg)
        return result

    async def _generate_summary(
        self, turns_to_summarize: list[dict[str, Any]], existing_summary: str = "",
    ) -> str | None:
        try:
            turns_text = "\n".join(
                f"{m.get('role', 'unknown')}: {self._extract_text(m.get('content', ''))}"
                for m in turns_to_summarize
            )
            if existing_summary:
                prompt = _SUMMARY_PROMPT_TEMPLATE_ITERATIVE.format(
                    existing_summary=existing_summary,
                    turns_to_summarize=turns_text,
                )
            else:
                prompt = _SUMMARY_PROMPT_TEMPLATE_FIRST.format(
                    turns_to_summarize=turns_text,
                )
            result = await self.llm_provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                stream=False,
                model=self._resolve_model(),
                options={},
            )
            if not isinstance(result, LLMResult):
                logger.warning("context_compressor: summary LLM returned non-LLMResult")
                return None
            content = result.message.get("content", "")
            return content if isinstance(content, str) else str(content)
        except Exception as exc:
            logger.warning("context_compressor: _generate_summary failed: %s", exc)
            return None

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
            return "".join(parts)
        return str(content)

    def _build_static_fallback_summary(
        self, turns: list[dict[str, Any]], existing_summary: str = "",
    ) -> str:
        user_msgs = [m for m in turns if m.get("role") == "user" and self._extract_text(m.get("content", ""))]
        asst_msgs = [m for m in turns if m.get("role") == "assistant" and self._extract_text(m.get("content", ""))]
        if not user_msgs and not asst_msgs:
            return existing_summary
        parts = []
        if user_msgs:
            parts.append("用户: " + self._extract_text(user_msgs[0].get("content", ""))[:200])
        if asst_msgs:
            parts.append("助手: " + self._extract_text(asst_msgs[-1].get("content", ""))[:200])
        return " | ".join(parts)

    async def _build_fallback_summary(
        self, turns: list[dict[str, Any]], existing_summary: str = "",
    ) -> str:
        if self.fallback_summarizer is not None:
            try:
                summary = await self.fallback_summarizer.summarize(turns, existing_summary)
                if summary:
                    return summary
            except Exception as exc:
                logger.warning("context_compressor: fallback_summarizer failed: %s", exc)
        return self._build_static_fallback_summary(turns, existing_summary=existing_summary)

    def _is_summary_message(self, msg: dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str):
            return False
        return content.startswith(CONTEXT_SUMMARY_PREFIX)

    def _insert_summary_message(
        self, head: list[dict[str, Any]], summary: str, tail: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        summary_msg = {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}{summary}"}
        # 移除 head/tail 中已有的摘要消息，避免同一次 result.messages 内出现新旧两条摘要
        cleaned_head = [m for m in head if not self._is_summary_message(m)]
        cleaned_tail = [m for m in tail if not self._is_summary_message(m)]
        combined = cleaned_head + [summary_msg] + cleaned_tail
        # Sanitize after insertion to handle any tool pair issues at boundaries
        return self._sanitize_tool_pairs(combined)
