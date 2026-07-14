"""Integration tests for LLM policy封口 in AgentGraphRunner.call_llm.

Tests that provider.chat is NOT called when:
- InformationFlow denies release to LLM_PROVIDER
- BudgetService denies reserve
- LLMPolicy denies model selection (vision unsupported)

Also tests:
- Successful call: budget settle + usage recording
- Vision unsupported maps to friendly final reply (not 500/error)
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.budget_service import BudgetService
from app.application.information_flow_service import InformationFlowService
from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    InformationFlowPolicyConfig,
)
from app.application.tool_service import (
    ToolService,
    builtin_tool_definitions,
)
from app.domain.agent import AgentState
from app.domain.budget import BudgetActualUsage, BudgetReserveKind, BudgetReserveRequest
from app.domain.information_flow import ReleaseTarget, SecretCatalog
from app.domain.llm_policy import LLMPolicy
from app.domain.policy import PolicyOutcome
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor


# ---------------------------------------------------------------------------
# Test providers
# ---------------------------------------------------------------------------


class CountingProvider:
    """Provider that counts chat calls and returns a simple response."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_model = ""

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("test", "test", "fake")]

    async def supports_tools(self, model: str) -> bool:
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult:
        self.calls += 1
        self.last_model = model
        return LLMResult(
            message={"role": "assistant", "content": "hello"},
            finish_reason="stop",
            usage={"total_tokens": 42, "prompt_tokens": 10, "completion_tokens": 32},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_runner(
    tmp_path,
    *,
    provider=None,
    budget_config=None,
    info_flow_config=None,
    secrets=None,
    vision_capability=None,
    llm_policy=None,
) -> tuple[AgentGraphRunner, Any]:
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = provider or CountingProvider()
    budget_service = BudgetService(budget_config or BudgetPolicyConfig())
    info_flow = InformationFlowService(
        info_flow_config or InformationFlowPolicyConfig(),
        secrets or SecretCatalog(),
    )
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        vision_capability=vision_capability,
        budget_service=budget_service,
        information_flow_service=info_flow,
        llm_policy=llm_policy or LLMPolicy(),
    )
    return runner, provider


# ---------------------------------------------------------------------------
# InformationFlow deny -> provider.chat == 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_information_flow_deny_prevents_provider_call(tmp_path):
    """When InformationFlow denies release to LLM_PROVIDER, provider.chat is not called."""
    secret_value = "SUPER_SECRET_API_KEY_12345"
    secrets = SecretCatalog(secret_values=frozenset({secret_value}))
    # redact_secrets=False -> secret content cannot be released
    info_config = InformationFlowPolicyConfig(redact_secrets=False)
    runner, provider = await _make_runner(
        tmp_path,
        info_flow_config=info_config,
        secrets=secrets,
    )

    state = await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[{"role": "user", "content": f"my key is {secret_value}"}],
        ),
        "test",
    )

    assert provider.calls == 0
    assert state.error is not None
    assert "information_release" in state.error or "denied" in state.error


# ---------------------------------------------------------------------------
# Budget deny -> provider.chat == 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_deny_prevents_provider_call(tmp_path):
    """When BudgetService denies reserve (max_llm_calls=0), provider.chat is not called."""
    budget_config = BudgetPolicyConfig(max_llm_calls=0)
    runner, provider = await _make_runner(
        tmp_path,
        budget_config=budget_config,
    )

    state = await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
    )

    assert provider.calls == 0
    assert state.final_message is not None
    content = state.final_message.get("content", "")
    assert "用量上限" in content or "budget" in content.lower()
    # T9: Budget exhaustion maps to BUDGET_EXHAUSTED -> finish_reason "length"
    assert state.finish_reason == "length"


# ---------------------------------------------------------------------------
# LLMPolicy deny (vision) -> provider.chat == 0, friendly reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_unsupported_returns_friendly_message_not_500(tmp_path):
    """When LLMPolicy denies vision, provider.chat is not called and a friendly
    message is returned (not an HTTP 500 or error state)."""
    runner, provider = await _make_runner(
        tmp_path,
        vision_capability=lambda: False,
    )

    state = await runner.run(
        AgentState(
            session_id="s1",
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
                    ],
                }
            ],
        ),
        "test",
    )

    assert provider.calls == 0
    assert state.error is None
    assert state.finish_reason == "stop"
    assert isinstance(state.final_message, dict)
    content = state.final_message.get("content", "")
    assert "不支持图片输入" in content


# ---------------------------------------------------------------------------
# Successful call -> budget settle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_settles_budget(tmp_path):
    """On successful provider.chat, budget is settled with actual usage."""
    budget_config = BudgetPolicyConfig(max_llm_calls=10, max_token_cost=10000)
    runner, provider = await _make_runner(
        tmp_path,
        budget_config=budget_config,
    )

    # Wrap close to capture state before account is removed (T9: close removes account)
    captured_state = []
    original_close = runner._budget_service.close

    async def capturing_close(run_id):
        state = runner._budget_service.get_state(run_id)
        if state is not None:
            captured_state.append(state)
        await original_close(run_id)

    runner._budget_service.close = capturing_close  # type: ignore

    await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
    )

    assert provider.calls == 1
    # Verify budget was settled (captured before close removed the account)
    assert len(captured_state) == 1
    budget_state = captured_state[0]
    # After settle, consumed tokens should be 42 (from CountingProvider usage)
    # The reserve estimated 0 (no ContextPlan in this simple test), settle
    # adjusts to actual: consumed_tokens - estimated_tokens = 42 - 0 = 42
    assert budget_state.token_cost_reserved == 42
    assert budget_state.llm_calls_reserved == 1


# ---------------------------------------------------------------------------
# Successful call -> usage recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_records_usage(tmp_path):
    """On successful provider.chat, usage_service.record_call is invoked."""
    class SpyUsageService:
        def __init__(self):
            self.recorded = []

        async def record_call(self, **kwargs):
            self.recorded.append(kwargs)

        async def record_compression(self, **kwargs):
            pass

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = CountingProvider()
    budget_service = BudgetService(BudgetPolicyConfig())
    info_flow = InformationFlowService(
        InformationFlowPolicyConfig(),
        SecretCatalog(),
    )
    spy_usage = SpyUsageService()
    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        usage_service=spy_usage,
        budget_service=budget_service,
        information_flow_service=info_flow,
        llm_policy=LLMPolicy(),
    )

    await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
    )

    assert provider.calls == 1
    assert len(spy_usage.recorded) == 1
    call = spy_usage.recorded[0]
    assert call["raw_usage"]["total_tokens"] == 42


# ---------------------------------------------------------------------------
# Provider.chat = 0 on any deny (comprehensive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_policy_deny_tools_prevents_provider_call(tmp_path):
    """When LLMPolicy denies tool capability, provider.chat is not called."""
    from app.domain.llm_policy import (
        LLMConfig,
        ModelRequirements,
        ProviderCapability,
        ProviderConstraints,
    )

    class ToolDenyPolicy(LLMPolicy):
        """Always denies tools capability."""
        def evaluate(self, requested_model, provider_capabilities, requirements, constraints, config):
            # Inject tools requirement to trigger deny
            req = ModelRequirements(
                capabilities=requirements.capabilities | frozenset({"tools"}),
                token_need=requirements.token_need,
            )
            cap = ProviderCapability(
                provider_id="default", model_id="test",
                supports_tools=False, supports_vision=True,
            )
            return super().evaluate(requested_model, (cap,), req, constraints, config)

    runner, provider = await _make_runner(
        tmp_path,
        llm_policy=ToolDenyPolicy(),
    )

    state = await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
    )

    assert provider.calls == 0
    assert state.error is not None


# ---------------------------------------------------------------------------
# Budget reserve/settle wraps the call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_reserve_before_settle_after_provider_call(tmp_path):
    """Budget reserve happens before provider.chat, settle after."""
    class TracingProvider(CountingProvider):
        def __init__(self):
            super().__init__()
            self.budget_state_at_call = None

        async def chat(self, messages, tools, stream, model, options):
            return await super().chat(messages, tools, stream, model, options)

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    provider = TracingProvider()
    budget_config = BudgetPolicyConfig(max_llm_calls=10, max_token_cost=10000)
    budget_service = BudgetService(budget_config)
    info_flow = InformationFlowService(
        InformationFlowPolicyConfig(),
        SecretCatalog(),
    )

    # Wrap budget_service.reserve to capture state at call time
    original_reserve = budget_service.reserve

    async def tracing_reserve(run_id, request):
        decision = await original_reserve(run_id, request)
        # After reserve, before chat, capture the state
        provider.budget_state_at_call = budget_service.get_state(run_id)
        return decision

    budget_service.reserve = tracing_reserve  # type: ignore

    # Wrap close to capture final state before account is removed (T9)
    captured_final_state = []
    original_close = budget_service.close

    async def capturing_close(run_id):
        state = budget_service.get_state(run_id)
        if state is not None:
            captured_final_state.append(state)
        await original_close(run_id)

    budget_service.close = capturing_close  # type: ignore

    runner = AgentGraphRunner(
        provider,
        ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        budget_service=budget_service,
        information_flow_service=info_flow,
        llm_policy=LLMPolicy(),
    )

    await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "test",
    )

    # At call time: 1 LLM call reserved
    assert provider.budget_state_at_call is not None
    assert provider.budget_state_at_call.llm_calls_reserved == 1

    # After settle (captured before close removed the account): still 1
    # (call count stays, tokens adjusted to actual)
    assert len(captured_final_state) == 1
    final_state = captured_final_state[0]
    assert final_state.llm_calls_reserved == 1
    assert final_state.token_cost_reserved == 42
