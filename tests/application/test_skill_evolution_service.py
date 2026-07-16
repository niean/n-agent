import pytest
from unittest.mock import AsyncMock, MagicMock
from app.application.skill_evolution_service import SkillEvolutionService, _SKILL_REVIEW_PROMPT


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
    await svc.run_background_review(session_id="s1", digest="x")  # 不抛异常
