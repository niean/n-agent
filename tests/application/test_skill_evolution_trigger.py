import pytest
from unittest.mock import AsyncMock, MagicMock

def _build_runner(evolution_service, nudge_interval):
    from app.application.agent_graph import AgentGraphRunner
    return AgentGraphRunner(
        llm_provider=MagicMock(), memory_store=MagicMock(), tool_service=MagicMock(),
        summarizer=MagicMock(),
        context_service=MagicMock(),
        evolution_service=evolution_service, nudge_interval=nudge_interval,
    )

@pytest.mark.asyncio
async def test_nudge_triggers_review_when_interval_reached():
    evolution = MagicMock(); evolution.maybe_trigger = AsyncMock()
    runner = _build_runner(evolution_service=evolution, nudge_interval=10)
    await runner._post_finalize_nudge(session_id="s1", turn_count=10,
                                      recent_messages=[{"role":"user","content":"done"}])
    evolution.maybe_trigger.assert_awaited_once()
    args = evolution.maybe_trigger.call_args
    assert (args.args[0] if args.args else args.kwargs["session_id"]) == "s1"

@pytest.mark.asyncio
async def test_nudge_skipped_when_below_interval():
    evolution = MagicMock(); evolution.maybe_trigger = AsyncMock()
    runner = _build_runner(evolution_service=evolution, nudge_interval=10)
    await runner._post_finalize_nudge(session_id="s1", turn_count=3, recent_messages=[])
    evolution.maybe_trigger.assert_not_awaited()

@pytest.mark.asyncio
async def test_nudge_skipped_when_no_evolution_service():
    runner = _build_runner(evolution_service=None, nudge_interval=10)
    await runner._post_finalize_nudge(session_id="s1", turn_count=10, recent_messages=[])  # 不抛异常
