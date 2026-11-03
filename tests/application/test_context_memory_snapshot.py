"""Recalled memory survives a turn's tool loop without polluting history."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.application.context_service import ContextService
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX


def make_service(result):
    calls = []

    def read(query, **kwargs):
        calls.append((query, kwargs))
        return result

    return ContextService(
        memory_store=None,
        external_memory_manager=SimpleNamespace(),
        runtime_memory_service=SimpleNamespace(read_external_if_allowed=read),
    ), calls


def test_memory_survives_multiple_tools_and_compression_without_mutating_history():
    service, calls = make_service("<memory-context>Beijing</memory-context>")
    state = AgentState(session_id="s", working_messages=[{"role": "user", "content": "weather"}])
    for tool in ("skill_view", "web_fetch"):
        original = deepcopy(state.working_messages)
        messages = service.build_provider_messages(state)
        assert messages[0]["content"].count("<memory-context>") == 1
        assert state.working_messages == original
        state.working_messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{"id": tool}]},
            {"role": "tool", "tool_call_id": tool, "content": "result"},
        ])
    state.working_messages.insert(0, {"role": "user", "content": CONTEXT_SUMMARY_PREFIX + "old history"})
    messages = service.build_provider_messages(state)
    assert "Beijing" in messages[1]["content"]
    assert "Beijing" not in messages[0]["content"]
    assert len(calls) == 1


@pytest.mark.parametrize("result", ["", "<memory-context>Beijing</memory-context>"])
def test_snapshot_is_reused_even_for_empty_results_and_isolated_per_run(result):
    service, calls = make_service(result)
    for _ in range(2):
        state = AgentState(session_id="same-session", working_messages=[{"role": "user", "content": "weather"}])
        service.build_provider_messages(state)
        service.build_provider_messages(state)
    assert len(calls) == 2


def test_first_projection_after_tool_result_targets_latest_non_summary_user():
    service, calls = make_service("Beijing")
    state = AgentState(session_id="s", working_messages=[
        {"role": "user", "content": "old question"},
        {"role": "user", "content": [{"type": "text", "text": "weather"}]},
        {"role": "tool", "content": "result"},
    ])
    original = deepcopy(state.working_messages)
    messages = service.build_provider_messages(state)
    assert "Beijing" in str(messages[1]["content"])
    assert messages[0] == original[0]
    assert state.working_messages == original
    assert calls[0][0] == "weather"
