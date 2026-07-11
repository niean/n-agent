from __future__ import annotations

import json
import logging
import time

import pytest

from app.domain.context import CONTEXT_SUMMARY_PREFIX
from app.domain.provider import LLMResult
from app.infrastructure.context.context_compressor import ContextCompressor


class FakeLLMProvider:
    """Minimal LLMProvider for ContextCompressor unit tests."""

    def __init__(self, response: LLMResult | Exception = None):
        self._response = response
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def list_models(self):
        return []

    async def supports_tools(self, model: str) -> bool:
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.call_count += 1
        self.last_kwargs = {
            "messages": messages, "tools": tools, "stream": stream,
            "model": model, "options": options,
        }
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def make_compressor(**overrides):
    defaults = dict(
        llm_provider=FakeLLMProvider(),
        model="test-model",
        context_length=1000,
        threshold_percent=0.50,
        protect_first_n=2,
        protect_last_n=3,
        summary_target_ratio=0.20,
        cooldown_seconds=300,
        fallback_summarizer=None,
    )
    defaults.update(overrides)
    return ContextCompressor(**defaults)


def _make_llm_result(content: str) -> LLMResult:
    return LLMResult(
        message={"role": "assistant", "content": content},
        finish_reason="stop",
        usage={},
        raw=None,
    )


def test_estimate_tokens_text_content():
    c = make_compressor()
    # 4 char/token + 10 overhead per message
    msg = {"role": "user", "content": "a" * 40}
    tokens = c._estimate_tokens([msg])
    assert tokens == 20  # 40/4 + 10


def test_estimate_tokens_list_content_with_image():
    c = make_compressor()
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "hello"},  # 5 chars -> 1 token
            {"type": "image_url", "image_url": {"url": "data:..."}},  # 1500 tokens
        ],
    }
    tokens = c._estimate_tokens([msg])
    # text: 5/4=1, image: 1500, overhead: 10 -> 1511
    assert tokens == 1511


def test_estimate_tokens_tool_calls():
    c = make_compressor()
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "function": {"name": "calc", "arguments": '{"x":1}'}}],
    }
    tokens = c._estimate_tokens([msg])
    # content empty -> 0; tool_calls JSON stable dumps -> 68 chars -> 17; overhead 10 -> 27
    assert tokens == 27


def test_estimate_tokens_non_string_content_uses_stable_json():
    c = make_compressor()
    msg = {"role": "user", "content": {"nested": ["x" * 40]}}
    tokens = c._estimate_tokens([msg])
    # {"nested": ["xxxx...x"]} stable dumps length = 56 chars -> 14; overhead 10 -> 24
    assert tokens == 24


def test_estimate_tokens_skips_bad_message_keeps_valid():
    """Spec: a bad single message must not abort compression; valid messages still counted."""
    c = make_compressor()
    valid_msg = {"role": "user", "content": "a" * 40}  # 40/4 + 10 = 20 tokens
    bad_msg = "not-a-dict"  # .get() will raise AttributeError
    tokens = c._estimate_tokens([bad_msg, valid_msg])
    assert tokens == 20  # bad_msg skipped (0 tokens), valid_msg counted


def test_should_compress_handles_bad_message_without_raising(monkeypatch):
    c = make_compressor(context_length=1000, threshold_percent=0.50)
    def boom(_messages):
        raise RuntimeError("bad message")
    monkeypatch.setattr(c, "_estimate_tokens", boom)
    assert c.should_compress([{"role": "user", "content": object()}]) is False


def test_compute_threshold_tokens():
    c = make_compressor(context_length=1000, threshold_percent=0.50)
    assert c._compute_threshold_tokens() == 500


def test_should_compress_below_threshold_returns_false():
    c = make_compressor(context_length=1000, threshold_percent=0.50)
    # threshold = 500 tokens. Need > 500 tokens -> > 2000 chars (4 char/token)
    msg = {"role": "user", "content": "a" * 100}  # 25 + 10 = 35 tokens
    assert c.should_compress([msg]) is False


def test_should_compress_above_threshold_returns_true():
    c = make_compressor(context_length=1000, threshold_percent=0.50)
    # threshold = 500 tokens. > 500 -> > 1960 chars (1960/4 + 10 = 500)
    msg = {"role": "user", "content": "a" * 2000}  # 500 + 10 = 510 tokens
    assert c.should_compress([msg]) is True


def test_should_compress_force_bypasses_threshold():
    c = make_compressor(context_length=1000, threshold_percent=0.50)
    msg = {"role": "user", "content": "a" * 10}
    assert c.should_compress([msg], force=True) is True


def test_should_compress_in_cooldown_returns_false():
    c = make_compressor(context_length=1000, threshold_percent=0.50, cooldown_seconds=300)
    msg = {"role": "user", "content": "a" * 2000}
    assert c.should_compress([msg]) is True
    c._record_compression_success()  # enter cooldown
    assert c.should_compress([msg]) is False  # in cooldown
    assert c.should_compress([msg], force=True) is True  # force bypasses cooldown


def test_should_compress_cooldown_expired():
    times = [0, 0, 2]
    c = make_compressor(
        context_length=1000, threshold_percent=0.50, cooldown_seconds=1,
        _clock=lambda: times.pop(0),
    )
    msg = {"role": "user", "content": "a" * 2000}
    assert c.should_compress([msg]) is True
    c._record_compression_success()
    assert c.should_compress([msg]) is False  # cooldown (time=0)
    assert c.should_compress([msg]) is True  # cooldown expired (time=2)


def test_tool_group_span_identifies_tool_call_with_results():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "done"},
    ]
    # tool_group_span(msgs, 1) should return (1, 3) -- assistant at idx1 + tool at idx2
    span = c._tool_group_span(msgs, 1)
    assert span == (1, 3)


def test_tool_group_span_identifies_span_when_idx_is_tool_result():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "done"},
    ]
    assert c._tool_group_span(msgs, 2) == (1, 3)


def test_tool_group_span_no_tool_calls_returns_single():
    c = make_compressor()
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    span = c._tool_group_span(msgs, 1)
    assert span == (1, 2)


def test_align_boundary_backward_moves_out_of_tool_group():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "final"},
    ]
    # boundary at idx 2 (inside tool group 1-3) should move backward to 1
    assert c._align_boundary_backward(msgs, 2) == 1
    # boundary at idx 3 (outside group) stays
    assert c._align_boundary_backward(msgs, 3) == 3
    assert c._align_boundary_backward(msgs, 0) == 0


def test_align_boundary_forward_moves_out_of_tool_group():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "final"},
    ]
    # boundary at idx 1 (start of tool group 1-3) should move forward to 3
    assert c._align_boundary_forward(msgs, 1) == 3
    assert c._align_boundary_forward(msgs, len(msgs)) == len(msgs)


def test_align_boundary_forward_does_not_move_for_non_tool_message():
    """Non-tool messages have single-message spans; boundary must not move."""
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "final"},
    ]
    # idx 0 is a user message (span (0,1), single message) -> must not move
    assert c._align_boundary_forward(msgs, 0) == 0
    # idx 3 is a non-tool assistant (span (3,4), single message) -> must not move
    assert c._align_boundary_forward(msgs, 3) == 3


def test_align_boundary_forward_moves_from_inside_tool_group():
    """Boundary inside a tool group (not at start) must move forward to group end."""
    c = make_compressor()
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "after"},
    ]
    # idx 0 is start of tool group [0,2) -> move to 2
    assert c._align_boundary_forward(msgs, 0) == 2
    # idx 1 is inside tool group [0,2) -> move to 2
    assert c._align_boundary_forward(msgs, 1) == 2


def test_sanitize_removes_orphan_tool_result():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "orphan", "content": "r"},  # orphan, no preceding tool_call
        {"role": "assistant", "content": "a"},
    ]
    sanitized = c._sanitize_tool_pairs(msgs)
    assert len(sanitized) == 2
    assert sanitized[1]["role"] == "assistant"


def test_sanitize_removes_orphan_tool_call_from_assistant():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "content": "no tool result followed"},  # tool_call t1 has no result
    ]
    sanitized = c._sanitize_tool_pairs(msgs)
    # The assistant with tool_calls t1 should have tool_calls removed (or be removed)
    # Design choice: remove tool_calls field, keep message
    found = [m for m in sanitized if m.get("tool_calls")]
    assert found == []


def test_sanitize_does_not_keep_tool_result_from_non_contiguous_assistant():
    c = make_compressor()
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "content": "breaks tool group"},
        {"role": "tool", "tool_call_id": "t1", "content": "late orphan"},
    ]
    sanitized = c._sanitize_tool_pairs(msgs)
    assert not any(m.get("role") == "tool" for m in sanitized)


def test_prune_old_tool_results_truncates_long_arguments():
    c = make_compressor()
    long_args = json.dumps({"data": "x" * 1000})
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": long_args}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r"},
    ]
    pruned = c._prune_old_tool_results(msgs, protect_tail_count=0)
    args = pruned[0]["tool_calls"][0]["function"]["arguments"]
    assert len(args) <= 500
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == long_args


def test_prune_old_tool_results_truncates_long_tool_content():
    c = make_compressor()
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": "x" * 2000},
    ]
    pruned = c._prune_old_tool_results(msgs, protect_tail_count=0)
    assert len(pruned[0]["content"]) <= 500
    assert msgs[0]["content"] == "x" * 2000


def test_prune_old_tool_results_protects_tail_from_truncation():
    """Tail messages (last protect_tail_count) must be passed through unchanged."""
    c = make_compressor()
    long_args = json.dumps({"data": "x" * 1000})
    long_content = "y" * 2000
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": long_args}}]},
        {"role": "tool", "tool_call_id": "t1", "content": long_content},
    ]
    # protect_tail_count=2 protects both messages from truncation
    pruned = c._prune_old_tool_results(msgs, protect_tail_count=2)
    assert pruned[0]["tool_calls"][0]["function"]["arguments"] == long_args
    assert pruned[1]["content"] == long_content


@pytest.mark.asyncio
async def test_generate_summary_calls_llm_with_empty_tools_and_no_stream():
    fake = FakeLLMProvider(response=_make_llm_result("## 目标\n做某事"))
    c = make_compressor(llm_provider=fake, model="m1")
    summary = await c._generate_summary(
        [{"role": "user", "content": "hi"}], existing_summary="",
    )
    assert summary == "## 目标\n做某事"
    assert fake.last_kwargs["tools"] == []
    assert fake.last_kwargs["stream"] is False
    assert fake.last_kwargs["model"] == "m1"


@pytest.mark.asyncio
async def test_generate_summary_llm_failure_returns_none():
    fake = FakeLLMProvider(response=RuntimeError("boom"))
    c = make_compressor(llm_provider=fake)
    summary = await c._generate_summary(
        [{"role": "user", "content": "hi"}], existing_summary="",
    )
    assert summary is None


@pytest.mark.asyncio
async def test_generate_summary_non_llm_result_returns_none():
    fake = FakeLLMProvider(response=object())
    c = make_compressor(llm_provider=fake)
    summary = await c._generate_summary(
        [{"role": "user", "content": "hi"}], existing_summary="",
    )
    assert summary is None


@pytest.mark.asyncio
async def test_generate_summary_prompt_requires_structured_sections():
    fake = FakeLLMProvider(response=_make_llm_result("ok"))
    c = make_compressor(llm_provider=fake)
    await c._generate_summary([{"role": "user", "content": "hi"}], existing_summary="")
    prompt = fake.last_kwargs["messages"][0]["content"]
    for label in ("## 目标", "## 进展", "## 决策", "## 文件", "## 待办"):
        assert label in prompt


def test_build_static_fallback_summary_with_messages():
    c = make_compressor()
    msgs = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "final answer"},
    ]
    fb = c._build_static_fallback_summary(msgs, existing_summary="")
    assert "first question" in fb
    assert "final answer" in fb


def test_build_static_fallback_summary_empty_messages_returns_existing():
    c = make_compressor()
    fb = c._build_static_fallback_summary([], existing_summary="old summary")
    assert fb == "old summary"


@pytest.mark.asyncio
async def test_fallback_summarizer_used_before_static_summary():
    """When fallback_summarizer is set and returns a summary, static fallback is not used."""
    class FakeSummarizer:
        def __init__(self):
            self.calls = 0
        async def summarize(self, messages, existing_summary=""):
            self.calls += 1
            return "fallback summarizer summary"
    summarizer = FakeSummarizer()
    c = make_compressor(fallback_summarizer=summarizer)
    result = await c._build_fallback_summary(
        [{"role": "user", "content": "middle " * 50},
         {"role": "assistant", "content": "answer " * 50}],
        existing_summary="",
    )
    assert result == "fallback summarizer summary"
    assert summarizer.calls == 1


@pytest.mark.asyncio
async def test_fallback_summarizer_failure_falls_back_to_static():
    """When fallback_summarizer raises, static fallback is used."""
    class BoomSummarizer:
        async def summarize(self, messages, existing_summary=""):
            raise RuntimeError("summarizer boom")
    c = make_compressor(fallback_summarizer=BoomSummarizer())
    result = await c._build_fallback_summary(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        existing_summary="",
    )
    assert "q" in result
    assert "a" in result


def test_insert_summary_message_creates_user_message_with_markers():
    c = make_compressor()
    head = [{"role": "user", "content": "q1"}]
    tail = [{"role": "user", "content": "q2"}]
    result = c._insert_summary_message(head, "summary text", tail)
    # Find the summary message
    summary_msgs = [m for m in result if "[CONTEXT SUMMARY]: " in str(m.get("content", ""))]
    assert len(summary_msgs) == 1
    assert summary_msgs[0]["content"] == "[CONTEXT SUMMARY]: summary text"
    assert summary_msgs[0]["role"] == "user"


def test_insert_summary_message_preserves_head_and_tail_order():
    c = make_compressor()
    head = [{"role": "user", "content": "q1"}]
    tail = [{"role": "assistant", "content": "a2"}]
    result = c._insert_summary_message(head, "summary", tail)
    assert result[0]["content"] == "q1"
    assert result[-1]["content"] == "a2"


@pytest.mark.asyncio
async def test_compress_below_threshold_returns_not_compressed():
    c = make_compressor(context_length=10000, threshold_percent=0.50)
    msgs = [{"role": "user", "content": "short"}]
    result = await c.compress(msgs)
    assert result.compressed is False
    assert result.skipped_reason == "below_threshold"
    assert result.messages == msgs


@pytest.mark.asyncio
async def test_compress_force_triggers_even_below_threshold():
    fake = FakeLLMProvider(response=_make_llm_result("forced summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=10000, threshold_percent=0.50,
        protect_first_n=0, protect_last_n=0,
        tail_budget_enabled=True,
    )
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "user", "content": "q2"},
    ]
    result = await c.compress(msgs, force=True)
    assert result.compressed is True
    assert "forced summary" in result.summary


@pytest.mark.asyncio
async def test_compress_in_cooldown_returns_cooldown_reason():
    """compress during cooldown (no force) returns skipped_reason='cooldown'."""
    c = make_compressor(
        context_length=200, threshold_percent=0.50,
        protect_first_n=1, protect_last_n=1,
        cooldown_seconds=300,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})
    msgs.append({"role": "user", "content": "tail"})
    first = await c.compress(msgs)
    assert first.compressed is True
    # Second call without force: in cooldown
    second = await c.compress(msgs)
    assert second.compressed is False
    assert second.skipped_reason == "cooldown"


@pytest.mark.asyncio
async def test_compress_three_segment_replaces_middle_with_summary():
    fake = FakeLLMProvider(response=_make_llm_result("## 目标\n测试压缩"))
    c = make_compressor(
        llm_provider=fake,
        context_length=200,
        threshold_percent=0.50,
        protect_first_n=1,
        protect_last_n=1,
        summary_target_ratio=0.20,
    )
    # Build messages: head(1) + middle(several) + tail(1), total > 100 tokens
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})  # ~100 chars each
    msgs.append({"role": "user", "content": "tail"})
    result = await c.compress(msgs)
    assert result.compressed is True
    # First message is head, last is tail, middle replaced by summary
    assert result.messages[0]["content"] == "head"
    assert result.messages[-1]["content"] == "tail"
    # Summary message in the middle
    summary_msgs = [m for m in result.messages if "[CONTEXT SUMMARY]: " in str(m.get("content", ""))]
    assert len(summary_msgs) == 1
    assert result.compressed_tokens < result.original_tokens
    assert "## 目标" in result.summary


@pytest.mark.asyncio
async def test_compress_tail_respects_token_budget_and_protect_last_count():
    fake = FakeLLMProvider(response=_make_llm_result("summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=400,
        threshold_percent=0.10,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.10,  # tail budget ~= 40 tokens
        tail_budget_enabled=True,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(4):
        msgs.append({"role": "user", "content": f"middle {i} " * 30})
    msgs.extend([
        {"role": "user", "content": "small tail"},
        {"role": "user", "content": "large tail " * 80},
    ])
    result = await c.compress(msgs)
    assert result.compressed is True
    tail_after_summary = result.messages[-2:]
    assert tail_after_summary[-1]["content"].startswith("large tail")
    # When tail budget and protect_last_n conflict, implementation may keep only
    # budget-compatible recent messages, but must never split tool groups.
    assert len(result.messages) < len(msgs)


@pytest.mark.asyncio
async def test_compress_tail_budget_disabled_keeps_strict_tail_count():
    fake = FakeLLMProvider(response=_make_llm_result("summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=400,
        threshold_percent=0.10,
        protect_first_n=1,
        protect_last_n=2,
        summary_target_ratio=0.90,
        tail_budget_enabled=False,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": f"middle {i}"})
    msgs.extend([
        {"role": "user", "content": "tail 1"},
        {"role": "user", "content": "tail 2"},
    ])
    result = await c.compress(msgs)
    assert result.compressed is True
    assert [m["content"] for m in result.messages[-2:]] == ["tail 1", "tail 2"]
    assert len(result.messages) == 4  # head + summary + strict tail 2


@pytest.mark.asyncio
async def test_compress_llm_failure_uses_fallback_summary():
    fake = FakeLLMProvider(response=RuntimeError("boom"))
    c = make_compressor(
        llm_provider=fake,
        context_length=200,
        threshold_percent=0.50,
        protect_first_n=1,
        protect_last_n=1,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})
    msgs.append({"role": "user", "content": "tail"})
    result = await c.compress(msgs)
    assert result.compressed is True
    assert result.summary  # fallback summary non-empty


@pytest.mark.asyncio
async def test_compress_records_cooldown_on_success():
    c = make_compressor(
        context_length=200, threshold_percent=0.50, protect_first_n=1, protect_last_n=1,
        cooldown_seconds=300,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})
    msgs.append({"role": "user", "content": "tail"})
    await c.compress(msgs)
    assert c._last_compressed_at is not None


@pytest.mark.asyncio
async def test_compress_unexpected_error_does_not_record_cooldown(monkeypatch):
    c = make_compressor(
        context_length=200, threshold_percent=0.50,
        protect_first_n=1, protect_last_n=1,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})
    msgs.append({"role": "user", "content": "tail"})
    monkeypatch.setattr(c, "_insert_summary_message", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    result = await c.compress(msgs)
    assert result.compressed is False
    assert result.skipped_reason == "error"
    assert c._last_compressed_at is None


@pytest.mark.asyncio
async def test_compress_tool_group_not_split():
    fake = FakeLLMProvider(response=_make_llm_result("summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=200,
        threshold_percent=0.50,
        protect_first_n=1,
        protect_last_n=1,
    )
    msgs = [
        {"role": "user", "content": "head " * 30},
        {"role": "user", "content": "middle " * 20},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result " * 20},
        {"role": "user", "content": "tail " * 30},
    ]
    result = await c.compress(msgs)
    assert result.compressed is True
    # tool_call and tool_result must be in same segment (both in tail or both removed)
    has_tc = any(m.get("tool_calls") for m in result.messages)
    has_tr = any(m.get("role") == "tool" for m in result.messages)
    assert has_tc == has_tr  # both present or both absent


@pytest.mark.asyncio
async def test_compress_returns_tool_boundary_when_sanitize_cannot_make_legal(monkeypatch):
    fake = FakeLLMProvider(response=_make_llm_result("summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=200,
        threshold_percent=0.50,
        protect_first_n=1,
        protect_last_n=1,
    )
    msgs = [
        {"role": "user", "content": "head " * 30},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result " * 20},
        {"role": "user", "content": "tail " * 30},
    ]
    monkeypatch.setattr(c, "_sanitize_tool_pairs", lambda messages: [{"role": "tool", "tool_call_id": "orphan", "content": "bad"}])
    result = await c.compress(msgs)
    assert result.compressed is False
    assert result.skipped_reason == "tool_boundary"


@pytest.mark.asyncio
async def test_compress_sanitized_empty_returns_too_few_messages(monkeypatch):
    fake = FakeLLMProvider(response=_make_llm_result("summary"))
    c = make_compressor(
        llm_provider=fake,
        context_length=200,
        threshold_percent=0.50,
        protect_first_n=1,
        protect_last_n=1,
    )
    msgs = [{"role": "user", "content": "head " * 30}, {"role": "user", "content": "tail " * 30}]
    monkeypatch.setattr(c, "_sanitize_tool_pairs", lambda messages: [])
    result = await c.compress(msgs, force=True)
    assert result.compressed is False
    assert result.skipped_reason == "too_few_messages"


@pytest.mark.asyncio
async def test_compress_unexpected_error_returns_error_reason(monkeypatch):
    """Unexpected exception inside compress returns skipped_reason='error', not raise."""
    c = make_compressor(
        context_length=200, threshold_percent=0.50,
        protect_first_n=1, protect_last_n=1,
    )
    msgs = [{"role": "user", "content": "head"}]
    for i in range(5):
        msgs.append({"role": "user", "content": "middle " * 20 + str(i)})
    msgs.append({"role": "user", "content": "tail"})
    # Force _insert_summary_message to raise
    def boom(*args, **kwargs):
        raise RuntimeError("insert failed")
    monkeypatch.setattr(c, "_insert_summary_message", boom)
    result = await c.compress(msgs)
    assert result.compressed is False
    assert result.skipped_reason == "error"
    assert result.messages == msgs  # original returned


# ---- T3: Incremental compression tests ----


class _RecordingLLMProvider:
    """LLM provider that records the last prompt for assertion."""

    def __init__(self, response_content: str = "compressed summary"):
        self._response_content = response_content
        self.last_prompt: str = ""
        self.call_count = 0

    async def list_models(self):
        return []

    async def supports_tools(self, model: str) -> bool:
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.call_count += 1
        self.last_prompt = messages[0]["content"]
        return _make_llm_result(self._response_content)


def _make_incremental_compressor(response_content: str = "compressed summary"):
    return make_compressor(
        llm_provider=_RecordingLLMProvider(response_content),
        cooldown_seconds=0,
    )


def _build_messages(count: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(count)
    ]


def test_find_latest_context_summary_none():
    c = _make_incremental_compressor()
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert c._find_latest_context_summary(msgs) is None


def test_find_latest_context_summary_finds_last():
    c = _make_incremental_compressor()
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"},
        {"role": "user", "content": "msg2"},
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S2"},
        {"role": "user", "content": "msg3"},
    ]
    result = c._find_latest_context_summary(msgs)
    assert result is not None
    idx, body = result
    assert idx == 3
    assert body == "S2"


def test_find_latest_context_summary_strips_prefix_and_whitespace():
    c = _make_incremental_compressor()
    msgs = [
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}  S1 with spaces  "},
    ]
    result = c._find_latest_context_summary(msgs)
    assert result is not None
    _, body = result
    assert body == "S1 with spaces"


def test_find_latest_context_summary_skips_non_string_content():
    c = _make_incremental_compressor()
    msgs = [
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"},
        {"role": "user", "content": [{"type": "text", "text": f"{CONTEXT_SUMMARY_PREFIX}not a summary"}]},
    ]
    result = c._find_latest_context_summary(msgs)
    assert result is not None
    assert result[0] == 0


def test_find_latest_context_summary_skips_non_user_role():
    c = _make_incremental_compressor()
    msgs = [
        {"role": "assistant", "content": f"{CONTEXT_SUMMARY_PREFIX}not user"},
        {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}real"},
    ]
    result = c._find_latest_context_summary(msgs)
    assert result is not None
    assert result[0] == 1


@pytest.mark.asyncio
async def test_compress_middle_branch_no_summary_uses_full_middle():
    c = _make_incremental_compressor()
    msgs = _build_messages(20)
    result = await c.compress(msgs, force=True)
    assert result.compressed is True
    summaries = [m for m in result.messages if m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX)]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_compress_middle_branch_summary_in_head_uses_first_path():
    c = _make_incremental_compressor()
    msgs = _build_messages(20)
    msgs[1] = {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}old"}
    result = await c.compress(msgs, force=True)
    assert result.compressed is True
    summaries = [m for m in result.messages if m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX)]
    assert len(summaries) == 1
    assert "PREVIOUS SUMMARY" not in c.llm_provider.last_prompt


@pytest.mark.asyncio
async def test_compress_middle_branch_normal_incremental():
    c = _make_incremental_compressor()
    msgs = _build_messages(30)
    msgs.insert(2, {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}S1"})
    result = await c.compress(msgs, force=True)
    assert result.compressed is True
    summaries = [m for m in result.messages if m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX)]
    assert len(summaries) == 1
    assert "PREVIOUS SUMMARY" in c.llm_provider.last_prompt
    # middle 不含旧摘要之前的消息（msgs[0:2] 在 head 保护范围）
    # 用换行符做精确匹配，避免 "msg 1" 匹配到 "msg 10"/"msg 11" 等子串
    assert "user: msg 0\n" not in c.llm_provider.last_prompt
    assert "assistant: msg 1\n" not in c.llm_provider.last_prompt
    # 但应含 summary_idx+1 之后的消息
    assert "user: msg 2\n" in c.llm_provider.last_prompt


@pytest.mark.asyncio
async def test_compress_middle_branch_summary_in_tail_skips():
    c = _make_incremental_compressor()
    msgs = _build_messages(10)
    msgs[-1] = {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}old"}
    result = await c.compress(msgs, force=True)
    assert result.compressed is False
    assert result.skipped_reason == "summary_in_tail"


@pytest.mark.asyncio
async def test_generate_summary_first_path_no_existing_summary():
    c = _make_incremental_compressor()
    await c._generate_summary([{"role": "user", "content": "turn 1"}], existing_summary="")
    # 首次路径模板用"待压缩对话"标签，不含 PREVIOUS SUMMARY
    assert "待压缩对话" in c.llm_provider.last_prompt
    assert "PREVIOUS SUMMARY" not in c.llm_provider.last_prompt


@pytest.mark.asyncio
async def test_generate_summary_iterative_path_with_existing_summary():
    c = _make_incremental_compressor()
    await c._generate_summary([{"role": "user", "content": "turn 1"}], existing_summary="S1")
    assert "PREVIOUS SUMMARY" in c.llm_provider.last_prompt
    assert "NEW TURNS TO INCORPORATE" in c.llm_provider.last_prompt


def test_insert_summary_message_uses_context_summary_prefix():
    c = _make_incremental_compressor()
    head = [{"role": "system", "content": "sys"}]
    tail = [{"role": "user", "content": "last"}]
    combined = c._insert_summary_message(head, "S1", tail)
    summaries = [m for m in combined if m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX)]
    assert len(summaries) == 1
    assert summaries[0]["content"] == f"{CONTEXT_SUMMARY_PREFIX}S1"
    assert summaries[0]["role"] == "user"


def test_insert_summary_message_removes_existing_summary_in_head_tail():
    c = _make_incremental_compressor()
    head = [{"role": "system", "content": "sys"}, {"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}old"}]
    tail = [{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}old2"}, {"role": "user", "content": "last"}]
    combined = c._insert_summary_message(head, "S1", tail)
    summaries = [m for m in combined if m.get("content", "").startswith(CONTEXT_SUMMARY_PREFIX)]
    assert len(summaries) == 1
    assert summaries[0]["content"] == f"{CONTEXT_SUMMARY_PREFIX}S1"
