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
async def test_review_fork_does_not_persist_into_user_session(svc):
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))

    await svc.run_background_review(session_id="s1", digest="对话摘要")

    request = svc.chat.complete.call_args.args[0]
    assert request.persist_messages is False


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


@pytest.mark.asyncio
async def test_review_ingress_source_sets_gateway_source(svc):
    # Curator consolidation fork 必须把来源透传成 gateway.source，使会话 source 派生为 curator
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(
        session_id="curator-11111111-1111-4111-8111-111111111111",
        digest="x",
        ingress_source="curator",
    )
    request = svc.chat.complete.call_args.args[0]
    assert request.trusted_metadata["gateway.source"] == "curator"
    assert request.trusted_metadata["skill_write_origin"] == "background_review"


@pytest.mark.asyncio
async def test_review_default_source_is_skill_review(svc):
    # Reusing a user session must not turn a background run into an API ingress.
    svc.chat.complete = AsyncMock(return_value=MagicMock(message={"content": ""}))
    await svc.run_background_review(session_id="s1", digest="x")
    request = svc.chat.complete.call_args.args[0]
    assert request.trusted_metadata["gateway.source"] == "skill_review"
    assert request.trusted_metadata["skill_write_origin"] == "background_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_source", ["dashboard", "api", "feishu"])
async def test_review_source_reaches_real_chat_snapshot_without_changing_session(tmp_path, session_source):
    from app.application.chat_service import ChatCompletionService
    from app.application.policy_snapshot import RunPolicySnapshotFactory, SettingsPolicyProfileProvider
    from app.application.session_service import SessionService
    from app.config import Settings
    from app.domain.session import ConversationSession
    from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
    from tests.application.test_chat_service import RecordingRunner

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="user-session", source=session_source))
    runner = RecordingRunner()
    chat = ChatCompletionService(
        store, runner, SessionService(store),
        policy_snapshot_factory=RunPolicySnapshotFactory(SettingsPolicyProfileProvider(Settings())),
    )
    evolution = SkillEvolutionService(chat, MagicMock())
    await evolution.run_background_review("user-session", "digest")
    assert runner.options["_policy_snapshot"].run_context.source == "skill_review"
    assert (await store.get_session("user-session")).source == session_source
    assert runner.calls[0]["state"].persist_messages is False
