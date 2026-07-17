import pytest
from unittest.mock import AsyncMock, MagicMock
from app.application.skill_evolution_service import (
    BackgroundReviewResult,
    SkillEvolutionService,
    _SKILL_REVIEW_PROMPT,
)


def test_skill_review_prompt_has_anthropic_naming_guidance():
    # T7: background review prompt must guide Anthropic format + naming.
    assert "kebab-case" in _SKILL_REVIEW_PROMPT
    assert "skill-creator" in _SKILL_REVIEW_PROMPT
    assert "metadata" in _SKILL_REVIEW_PROMPT
    assert "中文 alias" in _SKILL_REVIEW_PROMPT
    assert "500" in _SKILL_REVIEW_PROMPT or "progressive disclosure" in _SKILL_REVIEW_PROMPT

@pytest.fixture
def svc():
    chat = MagicMock(); chat.complete = AsyncMock()
    tool_service = MagicMock()
    tool_service.build_filtered_definitions = MagicMock(return_value=[])
    return SkillEvolutionService(chat=chat, tool_service=tool_service,
        max_iterations=16, max_concurrent=1, enabled=True, nudge_interval=10,
        model=None, timeout_seconds=120)

@pytest.mark.asyncio
async def test_review_fork_calls_chat_with_skill_toolset_only(svc):
    await svc.run_background_review(session_id="s1", digest="对话摘要")
    svc.tool_service.build_filtered_definitions.assert_called_once()

@pytest.mark.asyncio
async def test_review_failure_isolated(svc):
    svc.chat.complete = AsyncMock(side_effect=RuntimeError("boom"))
    result = await svc.run_background_review(session_id="s1", digest="x")  # 不抛异常
    assert isinstance(result, BackgroundReviewResult)
    assert result.error is not None
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_review_returns_background_review_result(svc):
    svc.chat.complete = AsyncMock(
        return_value=MagicMock(message={"content": "done", "tool_calls": []})
    )
    result = await svc.run_background_review(session_id="s1", digest="x")
    assert isinstance(result, BackgroundReviewResult)
    assert result.final_text == "done"
    assert result.error is None
    assert result.tool_calls == []


@pytest.mark.asyncio
async def test_review_prompt_override(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(
        session_id="s1", digest="x", prompt="CUSTOM CONSOLIDATION PROMPT"
    )
    request = svc.chat.complete.call_args.args[0]
    assert request.messages[0]["content"] == "CUSTOM CONSOLIDATION PROMPT"


@pytest.mark.asyncio
async def test_review_prompt_none_uses_default(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(session_id="s1", digest="x", prompt=None)
    request = svc.chat.complete.call_args.args[0]
    assert request.messages[0]["content"] == _SKILL_REVIEW_PROMPT


@pytest.mark.asyncio
async def test_review_max_iterations_override(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(session_id="s1", digest="x", max_iterations=64)
    request = svc.chat.complete.call_args.args[0]
    assert request.options["max_iterations"] == 64


@pytest.mark.asyncio
async def test_review_allow_toolsets_skills_only(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(
        session_id="s1", digest="x", allow_toolsets={"skills"}
    )
    svc.tool_service.build_filtered_definitions.assert_called_once_with(
        allow_toolsets={"skills"},
        allow_tool_names={"skill_manage", "skills_list", "skill_view"},
    )


@pytest.mark.asyncio
async def test_review_default_toolsets_include_memory(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(session_id="s1", digest="x")
    svc.tool_service.build_filtered_definitions.assert_called_once_with(
        allow_toolsets={"skills", "memory"},
        allow_tool_names={"skill_manage", "skills_list", "skill_view"},
    )


@pytest.mark.asyncio
async def test_review_extracts_tool_calls_with_absorbed_into(svc):
    svc.chat.complete = AsyncMock(
        return_value=MagicMock(
            message={
                "content": "merged old into umbrella",
                "tool_calls": [
                    {
                        "function": {
                            "name": "skill_manage",
                            "arguments": '{"action": "delete", "name": "old", "absorbed_into": "umbrella"}',
                        }
                    }
                ],
            }
        )
    )
    result = await svc.run_background_review(session_id="s1", digest="x")
    assert result.final_text == "merged old into umbrella"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "skill_manage"
    assert "absorbed_into" in result.tool_calls[0]["arguments"]
