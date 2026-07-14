from __future__ import annotations

from app.domain.context_policy import (
    CompressionPlan,
    ContextCandidateSet,
    ContextMemoryCandidate,
    ContextMessageCandidate,
    ContextPlan,
    ContextPolicyRequest,
    DefaultContextPolicy,
    InjectionPlan,
    TokenAllocation,
    ToolContextCandidates,
)


def _msg(
    mid: str,
    role: str,
    content: str = "x" * 200,
    *,
    is_summary: bool = False,
    is_summarized: bool = False,
    has_tool_calls: bool = False,
    tool_call_id: str | None = None,
    name: str | None = None,
) -> ContextMessageCandidate:
    return ContextMessageCandidate(
        id=mid,
        role=role,
        content=content,
        is_summary=is_summary,
        is_summarized=is_summarized,
        has_tool_calls=has_tool_calls,
        tool_call_id=tool_call_id,
        name=name,
    )


def _request(
    messages: tuple[ContextMessageCandidate, ...],
    *,
    memory_blocks: tuple[ContextMemoryCandidate, ...] = (),
    context_length: int = 1000,
    compression_threshold: float = 0.50,
    compression_target_ratio: float = 0.20,
    protect_first_n: int = 3,
    protect_last_n: int = 10,
    cooldown_seconds: int = 300,
    tail_budget_enabled: bool = False,
    force: bool = False,
    in_cooldown: bool = False,
    existing_summary: str = "",
    model_context_window: int = 1000,
) -> ContextPolicyRequest:
    return ContextPolicyRequest(
        candidates=ContextCandidateSet(messages=messages, memory_blocks=memory_blocks),
        tool_candidates=ToolContextCandidates(),
        model_context_window=model_context_window,
        context_length=context_length,
        compression_threshold=compression_threshold,
        compression_target_ratio=compression_target_ratio,
        protect_first_n=protect_first_n,
        protect_last_n=protect_last_n,
        cooldown_seconds=cooldown_seconds,
        tail_budget_enabled=tail_budget_enabled,
        force=force,
        in_cooldown=in_cooldown,
        existing_summary=existing_summary,
    )


# ---------------------------------------------------------------------------
# Context length / threshold / target
# ---------------------------------------------------------------------------


def test_below_threshold_no_compression():
    """When total tokens are below threshold, compression plan is None."""
    msgs = (
        _msg("m1", "user", "short"),
        _msg("m2", "assistant", "short reply"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert plan.compression is None
    assert plan.selected_message_ids == ("m1", "m2")


def test_above_threshold_triggers_compression():
    """When total tokens exceed threshold, compression plan is produced."""
    msgs = tuple(_msg(f"m{i}", "user" if i % 2 == 0 else "assistant") for i in range(10))
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert plan.compression is not None
    assert plan.compression.head_n == 3
    assert plan.compression.tail_n == 10
    assert plan.compression.target_ratio == 0.20
    assert plan.compression.force is False


def test_force_triggers_compression_even_below_threshold():
    """force=True produces a CompressionPlan regardless of token count."""
    msgs = (_msg("m1", "user", "short"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs, force=True))
    assert plan.compression is not None
    assert plan.compression.force is True


# ---------------------------------------------------------------------------
# Head 3 / tail 10
# ---------------------------------------------------------------------------


def test_head_and_tail_protection_values_in_plan():
    """CompressionPlan carries protect_first_n and protect_last_n from config."""
    msgs = tuple(_msg(f"m{i}", "user") for i in range(20))
    plan = DefaultContextPolicy().evaluate(
        _request(msgs, protect_first_n=3, protect_last_n=10),
    )
    assert plan.compression is not None
    assert plan.compression.head_n == 3
    assert plan.compression.tail_n == 10


def test_custom_head_and_tail_values():
    """Custom protect values are reflected in the plan."""
    msgs = tuple(_msg(f"m{i}", "user") for i in range(20))
    plan = DefaultContextPolicy().evaluate(
        _request(msgs, protect_first_n=5, protect_last_n=5),
    )
    assert plan.compression is not None
    assert plan.compression.head_n == 5
    assert plan.compression.tail_n == 5


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_skips_compression():
    """When in_cooldown=True and force=False, no compression plan."""
    msgs = tuple(_msg(f"m{i}", "user") for i in range(10))
    plan = DefaultContextPolicy().evaluate(_request(msgs, in_cooldown=True))
    assert plan.compression is None


def test_force_overrides_cooldown():
    """When force=True, compression happens even in cooldown."""
    msgs = tuple(_msg(f"m{i}", "user") for i in range(10))
    plan = DefaultContextPolicy().evaluate(
        _request(msgs, in_cooldown=True, force=True),
    )
    assert plan.compression is not None
    assert plan.compression.force is True


# ---------------------------------------------------------------------------
# Latest summary selection
# ---------------------------------------------------------------------------


def test_latest_summary_selected_old_summaries_excluded():
    """Only the latest summary message is selected; old summaries are dropped."""
    msgs = (
        _msg("m1", "user", "old question"),
        _msg("m2", "assistant", "old reply"),
        _msg("s1", "user", "[CONTEXT SUMMARY]: summary 1", is_summary=True),
        _msg("m3", "user", "question 2"),
        _msg("m4", "assistant", "reply 2"),
        _msg("s2", "user", "[CONTEXT SUMMARY]: summary 2", is_summary=True),
        _msg("m5", "user", "latest question"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert "s2" in plan.selected_message_ids
    assert "s1" not in plan.selected_message_ids
    assert "m1" in plan.selected_message_ids
    assert "m5" in plan.selected_message_ids


def test_no_summary_selects_all_non_summarized():
    """Without any summary, all non-summarized messages are selected."""
    msgs = (
        _msg("m1", "user", "q1"),
        _msg("m2", "assistant", "r1", is_summarized=True),
        _msg("m3", "user", "q2"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert "m1" in plan.selected_message_ids
    assert "m2" not in plan.selected_message_ids
    assert "m3" in plan.selected_message_ids


def test_summarized_messages_excluded_when_summary_exists():
    """is_summarized messages are excluded when a latest summary exists."""
    msgs = (
        _msg("m1", "user", "head q"),
        _msg("m2", "assistant", "middle r", is_summarized=True),
        _msg("m3", "user", "middle q", is_summarized=True),
        _msg("m4", "assistant", "tail r"),
        _msg("s1", "user", "[CONTEXT SUMMARY]: summary", is_summary=True),
        _msg("m5", "user", "new q"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert "m1" in plan.selected_message_ids
    assert "m2" not in plan.selected_message_ids
    assert "m3" not in plan.selected_message_ids
    assert "m4" in plan.selected_message_ids
    assert "s1" in plan.selected_message_ids
    assert "m5" in plan.selected_message_ids


# ---------------------------------------------------------------------------
# Assistant tool-call + tool result group completeness
# ---------------------------------------------------------------------------


def test_tool_call_group_both_selected():
    """Assistant with tool_calls and corresponding tool result are both selected."""
    tool_calls = [{"id": "tc1", "type": "function", "function": {"name": "calc", "arguments": "{}"}}]
    msgs = (
        _msg("m1", "user", "question"),
        _msg("m2", "assistant", content={"content": "", "tool_calls": tool_calls}, has_tool_calls=True),
        _msg("m3", "tool", content='{"result": 42}', tool_call_id="tc1", name="calc"),
        _msg("m4", "user", "next question"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert "m2" in plan.selected_message_ids
    assert "m3" in plan.selected_message_ids


def test_tool_call_group_with_summary_preserves_group():
    """Tool-call group members on opposite sides of a summary are both selected."""
    tool_calls = [{"id": "tc1", "type": "function", "function": {"name": "calc", "arguments": "{}"}}]
    msgs = (
        _msg("m1", "user", "head q"),
        _msg("m2", "assistant", content={"content": "", "tool_calls": tool_calls}, has_tool_calls=True),
        _msg("m3", "user", "middle", is_summarized=True),
        _msg("m4", "tool", content='{"result": 42}', tool_call_id="tc1", name="calc"),
        _msg("s1", "user", "[CONTEXT SUMMARY]: summary", is_summary=True),
        _msg("m5", "user", "latest q"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    # Both assistant tool_call and tool result are selected
    assert "m2" in plan.selected_message_ids
    assert "m4" in plan.selected_message_ids


# ---------------------------------------------------------------------------
# External memory injection to last user message
# ---------------------------------------------------------------------------


def test_injection_targets_last_user_message():
    """InjectionPlan targets the last non-summary user message."""
    msgs = (
        _msg("m1", "user", "first question"),
        _msg("m2", "assistant", "reply"),
        _msg("m3", "user", "latest question"),
    )
    memory = (ContextMemoryCandidate(provider="kb", block_text="<memory>context</memory>"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs, memory_blocks=memory))
    assert plan.injection.target_message_id == "m3"
    assert plan.injection.position == "prepend"


def test_injection_no_user_message_returns_none_target():
    """When no user message exists, injection target is None."""
    msgs = (
        _msg("m1", "assistant", "reply"),
    )
    memory = (ContextMemoryCandidate(provider="kb", block_text="context"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs, memory_blocks=memory))
    assert plan.injection.target_message_id is None


def test_injection_no_memory_blocks_returns_none_target():
    """When no memory blocks exist, injection target is None."""
    msgs = (
        _msg("m1", "user", "question"),
    )
    plan = DefaultContextPolicy().evaluate(_request(msgs, memory_blocks=()))
    assert plan.injection.target_message_id is None


def test_injection_skips_summary_user_messages():
    """Injection targets the last non-summary user message, not a summary."""
    msgs = (
        _msg("m1", "user", "real question"),
        _msg("s1", "user", "[CONTEXT SUMMARY]: summary", is_summary=True),
        _msg("m2", "user", "latest question"),
    )
    memory = (ContextMemoryCandidate(provider="kb", block_text="context"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs, memory_blocks=memory))
    assert plan.injection.target_message_id == "m2"


# ---------------------------------------------------------------------------
# TokenAllocation
# ---------------------------------------------------------------------------


def test_token_allocation_produced():
    """TokenAllocation is produced with non-zero values."""
    msgs = tuple(_msg(f"m{i}", "user") for i in range(10))
    plan = DefaultContextPolicy().evaluate(
        _request(msgs, model_context_window=8000),
    )
    assert plan.token_allocation.total == 8000
    assert plan.token_allocation.system > 0
    assert plan.token_allocation.session > 0
    assert plan.token_allocation.turn > 0
    assert plan.token_allocation.tool > 0
    assert plan.token_allocation.system + plan.token_allocation.session + plan.token_allocation.turn + plan.token_allocation.tool <= 8000


# ---------------------------------------------------------------------------
# ContextPlan structure
# ---------------------------------------------------------------------------


def test_plan_is_frozen():
    """ContextPlan is a frozen dataclass."""
    msgs = (_msg("m1", "user", "q"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    try:
        plan.selected_message_ids = ()  # type: ignore[misc]
        assert False, "should have raised FrozenInstanceError"
    except AttributeError:
        pass


def test_plan_reasons_non_empty():
    """ContextPlan always has non-empty reasons."""
    msgs = (_msg("m1", "user", "q"),)
    plan = DefaultContextPolicy().evaluate(_request(msgs))
    assert len(plan.reasons) > 0


def test_compression_plan_is_frozen():
    """CompressionPlan is a frozen dataclass."""
    cp = CompressionPlan(head_n=3, tail_n=10, target_ratio=0.2)
    try:
        cp.head_n = 5  # type: ignore[misc]
        assert False, "should have raised FrozenInstanceError"
    except AttributeError:
        pass
