from __future__ import annotations

import pytest

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEventType
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX, ContextCompressionResult
from app.domain.provider import LLMResult
from app.domain.session import ConversationSession
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor


class UsageCapturingProvider:
    """Provider that returns a configurable LLMResult with usage."""

    def __init__(self, usage: dict, model_response: str = "ok"):
        self._usage = usage
        self._model_response = model_response
        self.call_count = 0
        self.last_model: str | None = None

    async def list_models(self):
        return []

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.call_count += 1
        self.last_model = model
        return LLMResult(
            message={"role": "assistant", "content": self._model_response},
            finish_reason="stop",
            usage=dict(self._usage),
            raw=None,
        )


class FakeUsageService:
    """Captures record_call / record_compression invocations."""

    def __init__(self):
        self.record_calls: list[dict] = []
        self.record_compressions: list[dict] = []

    async def record_call(
        self, session_id, model, provider, raw_usage, latency_ms,
        provider_kind: str = "openai",
        requested_model: str | None = None,
        trigger_type: str | None = None,
        request_messages: str | None = None,
        response_message: str | None = None,
        tools: str | None = None,
        generation_params: str | None = None,
    ) -> None:
        self.record_calls.append({
            "session_id": session_id,
            "model": model,
            "provider": provider,
            "raw_usage": dict(raw_usage) if raw_usage else {},
            "latency_ms": latency_ms,
            "provider_kind": provider_kind,
            "requested_model": requested_model,
            "trigger_type": trigger_type,
            "request_messages": request_messages,
            "response_message": response_message,
            "tools": tools,
            "generation_params": generation_params,
        })

    async def record_compression(
        self, session_id, before_tokens, after_tokens,
        before_messages=None, after_messages=None,
    ) -> None:
        self.record_compressions.append({
            "session_id": session_id,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "before_messages": before_messages,
            "after_messages": after_messages,
        })


class FakeContextEngine:
    # T7: Config attributes read by ContextService._get_engine_config.
    context_length = 100
    threshold_percent = 0.01
    protect_first_n = 3
    protect_last_n = 10
    summary_target_ratio = 0.2
    cooldown_seconds = 300
    tail_budget_enabled = False

    def __init__(self, result: ContextCompressionResult):
        self._result = result

    def should_compress(self, messages, *, prompt_tokens=None, force=False):
        return True

    def is_in_cooldown(self) -> bool:
        return False

    async def compress(self, messages, *, current_tokens=None, force=False, existing_summary=""):
        return self._result


@pytest.mark.asyncio
async def test_call_llm_records_usage_when_usage_service_present(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))

    usage_dict = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    provider = UsageCapturingProvider(usage=usage_dict)
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )

    state = AgentState(
        session_id="sess-1",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    new_state = await runner.call_llm(state)

    assert new_state.error is None
    assert provider.call_count == 1
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["session_id"] == "sess-1"
    assert call["raw_usage"] == usage_dict
    assert call["provider_kind"] == "openai"
    assert call["latency_ms"] is not None and call["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_call_llm_does_not_retain_internal_fork_payload_usage(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-internal"))
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=UsageCapturingProvider(
            usage={"prompt_tokens": 100, "completion_tokens": 10}
        ),
        tool_service=ToolService(
            build_builtin_tool_executor(tmp_path), builtin_tool_definitions()
        ),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )
    state = AgentState(
        session_id="sess-internal",
        input_messages=[{"role": "user", "content": "internal digest"}],
        working_messages=[
            {"role": "system", "content": "## Identity\n\nsecret runtime prompt"},
            {"role": "user", "content": "internal digest"},
        ],
        persist_messages=False,
    )

    await runner.call_llm(state)

    assert usage_service.record_calls == []


@pytest.mark.asyncio
async def test_call_llm_skips_usage_when_service_none(tmp_path):
    """Backward compat: usage_service=None should not break call_llm."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-2"))

    provider = UsageCapturingProvider(usage={"prompt_tokens": 10})
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
    )

    state = AgentState(
        session_id="sess-2",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    new_state = await runner.call_llm(state)
    assert new_state.error is None
    assert new_state.final_message["content"] == "ok"


@pytest.mark.asyncio
async def test_call_llm_skips_usage_when_result_usage_empty(tmp_path):
    """Empty usage dict should not trigger record_call."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-3"))

    provider = UsageCapturingProvider(usage={})
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )

    state = AgentState(
        session_id="sess-3",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    await runner.call_llm(state)
    assert usage_service.record_calls == []


@pytest.mark.asyncio
async def test_call_llm_does_not_log_full_payload_by_default(tmp_path, caplog):
    """call_llm should NOT emit full request/response payload logs by default
    (InformationFlowPolicyConfig.log_llm_payloads=False = fail-closed)."""
    import logging

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-log"))

    provider = UsageCapturingProvider(
        usage={},  # empty usage -> usage_service.record_call NOT invoked
        model_response="hello world",
    )
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
    )

    state = AgentState(
        session_id="sess-log",
        input_messages=[{"role": "user", "content": "hi there"}],
        working_messages=[{"role": "user", "content": "hi there"}],
    )

    with caplog.at_level(logging.INFO, logger="app.application.agent_graph"):
        await runner.call_llm(state)

    request_logs = [r for r in caplog.records if "LLM request" in r.getMessage()]
    response_logs = [r for r in caplog.records if "LLM response" in r.getMessage()]
    # Default config denies LLM payload logging -- no full payload logs
    assert len(request_logs) == 0, f"expected 0 LLM request logs (default deny), got {len(request_logs)}"
    assert len(response_logs) == 0, f"expected 0 LLM response logs (default deny), got {len(response_logs)}"


@pytest.mark.asyncio
async def test_call_llm_logs_sanitized_payload_when_enabled(tmp_path, caplog):
    """When log_llm_payloads=True and a secret is present, the logged payload
    must be sanitized (secret value replaced with [REDACTED])."""
    import logging

    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_snapshot import InformationFlowPolicyConfig
    from app.domain.information_flow import SecretCatalog

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-log2"))

    provider = UsageCapturingProvider(
        usage={},
        model_response="the key is sk-secret123",
    )
    info_svc = InformationFlowService(
        InformationFlowPolicyConfig(log_llm_payloads=True, redact_secrets=True),
        SecretCatalog(secret_values=frozenset({"sk-secret123"})),
    )
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        information_flow_service=info_svc,
    )

    state = AgentState(
        session_id="sess-log2",
        input_messages=[{"role": "user", "content": "key=sk-secret123"}],
        working_messages=[{"role": "user", "content": "key=sk-secret123"}],
    )

    with caplog.at_level(logging.INFO, logger="app.application.agent_graph"):
        await runner.call_llm(state)

    request_logs = [r for r in caplog.records if "LLM request" in r.getMessage()]
    response_logs = [r for r in caplog.records if "LLM response" in r.getMessage()]
    assert len(request_logs) == 1
    assert len(response_logs) == 1
    # Secret must be redacted in logs
    assert "sk-secret123" not in request_logs[0].getMessage()
    assert "sk-secret123" not in response_logs[0].getMessage()
    assert "[REDACTED]" in request_logs[0].getMessage()
    assert "[REDACTED]" in response_logs[0].getMessage()


@pytest.mark.asyncio
async def test_call_llm_does_not_log_internal_fork_payload_when_enabled(
    tmp_path, caplog
):
    import logging

    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_snapshot import InformationFlowPolicyConfig
    from app.domain.information_flow import SecretCatalog

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-internal-log"))
    runner = AgentGraphRunner(
        llm_provider=UsageCapturingProvider(usage={}, model_response="review done"),
        tool_service=ToolService(
            build_builtin_tool_executor(tmp_path), builtin_tool_definitions()
        ),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        information_flow_service=InformationFlowService(
            InformationFlowPolicyConfig(log_llm_payloads=True),
            SecretCatalog(),
        ),
    )
    state = AgentState(
        session_id="sess-internal-log",
        input_messages=[{"role": "user", "content": "internal digest"}],
        working_messages=[
            {"role": "system", "content": "## Identity\n\nsecret runtime prompt"},
            {"role": "user", "content": "internal digest"},
        ],
        persist_messages=False,
    )

    with caplog.at_level(logging.INFO, logger="app.application.agent_graph"):
        await runner.call_llm(state)

    assert not any(
        "LLM request" in record.getMessage()
        or "LLM response" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_call_llm_records_usage_with_provider_config(tmp_path):
    """When llm_provider exposes current_config (ActiveProviderHolder-like),
    provider_kind/provider should be derived from it."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-4"))

    usage_dict = {"input_tokens": 80, "output_tokens": 30}
    provider = UsageCapturingProvider(usage=usage_dict)

    class FakeProviderConfig:
        provider_type = "anthropic"
        model = "claude-3-5-sonnet"

    provider.current_config = FakeProviderConfig()  # type: ignore[attr-defined]

    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )

    state = AgentState(
        session_id="sess-4",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["provider_kind"] == "anthropic"
    assert call["provider"] == "anthropic"
    assert call["model"] == "claude-3-5-sonnet"
    assert call["trigger_type"] == "user"


@pytest.mark.asyncio
async def test_call_llm_resolves_placeholder_model_to_real(tmp_path):
    """Runtime model="N-Agent" (placeholder) must be resolved to the provider's
    configured model for usage recording; the original placeholder is preserved
    in requested_model."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-ph"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict)

    class FakeProviderConfig:
        provider_type = "openai"
        model = "deepseek-chat"

    provider.current_config = FakeProviderConfig()  # type: ignore[attr-defined]

    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )

    state = AgentState(
        session_id="sess-ph",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    config = {"configurable": {"model": "N-Agent"}}
    await runner.call_llm(state, config)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["model"] == "deepseek-chat"
    assert call["requested_model"] == "N-Agent"


@pytest.mark.asyncio
async def test_call_llm_trigger_type_tool_after_tool_result(tmp_path):
    """When working_messages ends with a tool result message, trigger_type
    should be 'tool'."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-tt"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict)

    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )

    state = AgentState(
        session_id="sess-tt",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "noop", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        ],
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["trigger_type"] == "tool"
    assert call["request_messages"] is not None
    assert call["response_message"] is not None
    import json as _json
    parsed_req = _json.loads(call["request_messages"])
    assert isinstance(parsed_req, list)
    assert parsed_req[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_call_llm_records_tools_capability_context(tmp_path):
    """call_llm should capture the tools array (Capability Context) as JSON
    in usage recording, so observations can show tool definitions per call."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-tools"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict)
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )
    state = AgentState(
        session_id="sess-tools",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["tools"] is not None
    import json as _json
    parsed = _json.loads(call["tools"])
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    first = parsed[0]
    assert first["type"] == "function"
    assert "name" in first["function"]


@pytest.mark.asyncio
async def test_call_llm_captures_generation_params_filtered(tmp_path):
    """call_llm should capture generation params (public keys only, internal
    option keys filtered out) as JSON in usage recording."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-gp"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict)
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )
    state = AgentState(
        session_id="sess-gp",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
        run_options={
            "temperature": 0.7,
            "max_tokens": 4096,
            "top_p": 1.0,
            # internal keys that must be filtered out
            "tool_execution_context": {"session_id": "sess-gp"},
            "tool_exposure_policy": "safe_only",
            "execution_context_mode": "auto",
            "external_memory_enabled": ["kb"],
            "stream_event_sink": None,
            "activated_skills": ["a"],
        },
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["generation_params"] is not None
    import json as _json
    parsed = _json.loads(call["generation_params"])
    assert isinstance(parsed, dict)
    # public keys preserved
    assert parsed["temperature"] == 0.7
    assert parsed["max_tokens"] == 4096
    assert parsed["top_p"] == 1.0
    # internal keys filtered out
    assert "tool_execution_context" not in parsed
    assert "tool_exposure_policy" not in parsed
    assert "execution_context_mode" not in parsed
    assert "external_memory_enabled" not in parsed
    assert "stream_event_sink" not in parsed
    assert "activated_skills" not in parsed


@pytest.mark.asyncio
async def test_call_llm_generation_params_none_when_no_public_options(tmp_path):
    """When options only contain internal keys, generation_params should be None
    (empty dict serializes to None per call_llm's `if gen_params` guard)."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-gp2"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict)
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
    )
    state = AgentState(
        session_id="sess-gp2",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
        run_options={
            "tool_execution_context": {"session_id": "sess-gp2"},
            "tool_exposure_policy": "safe_only",
        },
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    assert call["generation_params"] is None


@pytest.mark.asyncio
async def test_call_llm_usage_payloads_sanitized_when_secret_present(tmp_path):
    """Usage recording must receive sanitized payloads when secrets are present.
    Token/cost are still recorded; only the payload text is redacted."""
    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_snapshot import InformationFlowPolicyConfig
    from app.domain.information_flow import SecretCatalog

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-san"))

    secret = "sk-secret123"
    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict, model_response=f"key={secret}")
    info_svc = InformationFlowService(
        InformationFlowPolicyConfig(store_usage_payloads=True, redact_secrets=True),
        SecretCatalog(secret_values=frozenset({secret})),
    )
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
        information_flow_service=info_svc,
    )
    state = AgentState(
        session_id="sess-san",
        input_messages=[{"role": "user", "content": f"key={secret}"}],
        working_messages=[{"role": "user", "content": f"key={secret}"}],
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    # Token/cost still recorded
    assert call["raw_usage"] == usage_dict
    # Payloads must be sanitized -- secret must not appear
    assert call["request_messages"] is not None
    assert secret not in call["request_messages"]
    assert "[REDACTED]" in call["request_messages"]
    assert call["response_message"] is not None
    assert secret not in call["response_message"]
    assert "[REDACTED]" in call["response_message"]


@pytest.mark.asyncio
async def test_call_llm_usage_denied_raw_none_but_tokens_recorded(tmp_path):
    """When store_usage_payloads=False, raw payload fields are None but
    token/cost are still recorded."""
    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_snapshot import InformationFlowPolicyConfig
    from app.domain.information_flow import SecretCatalog

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-deny"))

    usage_dict = {"prompt_tokens": 50, "completion_tokens": 20}
    provider = UsageCapturingProvider(usage=usage_dict, model_response="hello")
    info_svc = InformationFlowService(
        InformationFlowPolicyConfig(store_usage_payloads=False, redact_secrets=True),
        SecretCatalog(),
    )
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=provider,
        tool_service=ToolService(build_builtin_tool_executor(tmp_path), builtin_tool_definitions()),
        memory_store=store,
        summarizer=HeuristicSummarizer(),
        usage_service=usage_service,
        information_flow_service=info_svc,
    )
    state = AgentState(
        session_id="sess-deny",
        input_messages=[{"role": "user", "content": "hi"}],
        working_messages=[{"role": "user", "content": "hi"}],
    )
    await runner.call_llm(state)
    assert len(usage_service.record_calls) == 1
    call = usage_service.record_calls[0]
    # Token/cost still recorded
    assert call["raw_usage"] == usage_dict
    # Payloads denied -> None
    assert call["request_messages"] is None
    assert call["response_message"] is None


@pytest.mark.asyncio
async def test_compress_context_records_compression_when_service_present(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-5"))

    result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}],
        summary="summary",
        compressed=True,
        skipped_reason=None,
        original_tokens=500,
        compressed_tokens=50,
    )
    usage_service = FakeUsageService()
    runner = AgentGraphRunner(
        llm_provider=UsageCapturingProvider(usage={}),
        tool_service=None,
        memory_store=store,
        summarizer=None,
        context_engine=FakeContextEngine(result),
        usage_service=usage_service,
    )
    state = AgentState(
        session_id="sess-5",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long"},
        ],
        summary="",
    )
    await runner.context_service.compress_prepared_context(state)
    assert len(usage_service.record_compressions) == 1
    comp = usage_service.record_compressions[0]
    assert comp["session_id"] == "sess-5"
    assert comp["before_tokens"] == 500
    assert comp["after_tokens"] == 50


@pytest.mark.asyncio
async def test_compress_context_skips_compression_when_service_none(tmp_path):
    """Backward compat: usage_service=None should not break compress_context."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-6"))

    result = ContextCompressionResult(
        messages=[{"role": "user", "content": f"{CONTEXT_SUMMARY_PREFIX}summary"}],
        summary="summary",
        compressed=True,
        skipped_reason=None,
        original_tokens=500,
        compressed_tokens=50,
    )
    runner = AgentGraphRunner(
        llm_provider=UsageCapturingProvider(usage={}),
        tool_service=None,
        memory_store=store,
        summarizer=None,
        context_engine=FakeContextEngine(result),
    )
    state = AgentState(
        session_id="sess-6",
        input_messages=[],
        working_messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "long"},
        ],
        summary="",
    )
    new_state = await runner.context_service.compress_prepared_context(state)
    assert new_state.summary == "summary"
