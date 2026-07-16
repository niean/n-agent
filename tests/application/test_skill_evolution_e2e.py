"""E2E integration tests for Skill self-evolution (Task 16 capstone).

Verifies T1-T15 components work together with real stores/registry/loader/policy.
Only the LLM/chat layer is faked.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.skill_evolution_service import SkillEvolutionService
from app.application.skill_service import (
    SkillService,
    SkillManageRequestBuilder,
    skill_manage_tool_definition,
)
from app.domain.skill import SkillSource, SkillWriteOrigin
from app.domain.skill_policy import SkillPolicy
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.infrastructure.skill.file_loader import SkillFileLoader, SkillFileLoaderConfig
from app.infrastructure.skill.skill_backup_store import SkillBackupStore
from app.infrastructure.skill.skill_pending_store import SkillPendingStore
from app.infrastructure.skill.skill_usage_store import SkillUsageStore


# ---------------------------------------------------------------------------
# Fake chat: captures the ChatCompletionInput passed to chat.complete
# ---------------------------------------------------------------------------

class FakeChatComplete:
    """Captures the ChatCompletionInput passed to chat.complete.

    SkillEvolutionService delegates tool execution to chat.complete (the
    AgentGraphRunner's job), so the fake only needs to record the request
    and return a dummy result.  The origin-injection assertion verifies
    SkillEvolutionService's responsibility: setting
    trusted_metadata["skill_write_origin"].
    """

    def __init__(self):
        self.captured_request: ChatCompletionInput | None = None

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult:
        self.captured_request = request
        return ChatCompletionResult(
            session_id=request.session_id or "test",
            model=request.model,
            message={"role": "assistant", "content": "done"},
        )

    def assert_trusted_metadata_origin(self, expected: str) -> None:
        assert self.captured_request is not None, "chat.complete was never called"
        actual = self.captured_request.trusted_metadata.get("skill_write_origin")
        assert actual == expected, (
            f"expected skill_write_origin={expected!r}, got {actual!r}"
        )


# ---------------------------------------------------------------------------
# Fixtures: real components (only chat is faked)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_skills_root(tmp_path) -> Path:
    """Temporary skills root directory."""
    return tmp_path


def _build_real_skill_service(
    tmp_skills_root: Path, write_approval: bool
) -> SkillService:
    """Build a SkillService backed by real stores/registry/loader/policy."""
    registry = SQLiteSkillRegistry(str(tmp_skills_root / "registry.db"))
    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=tmp_skills_root, current_platform="linux")
    )
    usage = SkillUsageStore(str(tmp_skills_root / "usage.db"))
    pending = SkillPendingStore(str(tmp_skills_root / "pending.db"))
    backup = SkillBackupStore(root=tmp_skills_root, keep=3)
    policy = SkillPolicy()
    return SkillService(
        registry=registry,
        loader=loader,
        usage=usage,
        pending=pending,
        backup=backup,
        policy=policy,
        write_approval=write_approval,
        guard_agent_created=True,
        backup_enabled=True,
    )


@pytest.fixture
def real_skill_service(tmp_skills_root) -> SkillService:
    return _build_real_skill_service(tmp_skills_root, write_approval=False)


@pytest.fixture
def real_skill_service_write_approval_on(tmp_skills_root) -> SkillService:
    return _build_real_skill_service(tmp_skills_root, write_approval=True)


@pytest.fixture
def real_skill_service_with_backup(tmp_skills_root) -> SkillService:
    return _build_real_skill_service(tmp_skills_root, write_approval=False)


@pytest.fixture
def fake_chat_complete() -> FakeChatComplete:
    return FakeChatComplete()


@pytest.fixture
def real_skill_evolution_service(
    real_skill_service, fake_chat_complete
) -> SkillEvolutionService:
    """SkillEvolutionService wired to fake chat + mock tool_service.

    The mock tool_service returns the skill_manage tool definition so
    run_background_review can build its toolset.  Tool *execution* is the
    AgentGraphRunner's job (not SkillEvolutionService's), so the fake chat
    only needs to capture the ChatCompletionInput for origin verification.
    """
    tool_service = MagicMock()
    tool_service.build_filtered_definitions = MagicMock(
        return_value=[skill_manage_tool_definition()],
    )
    return SkillEvolutionService(
        chat=fake_chat_complete,
        tool_service=tool_service,
        max_iterations=16,
        max_concurrent=1,
        enabled=True,
        nudge_interval=10,
        model=None,
        timeout_seconds=120,
    )


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_foreground_create_skill_e2e(real_skill_service, tmp_skills_root):
    r = await real_skill_service.manage_skill(SkillManageRequestBuilder.create(
        name="demo", content="---\nname: demo\n---\nbody",
        origin=SkillWriteOrigin.FOREGROUND))
    assert r.success
    skill = await real_skill_service.registry.get_skill("demo")
    assert skill.source == SkillSource.USER
    assert (tmp_skills_root / "demo" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_bg_review_e2e_with_fake_llm(
    real_skill_evolution_service, fake_chat_complete
):
    # fake_chat_complete captures the ChatCompletionInput; SkillEvolutionService
    # does not execute tool calls itself (that is AgentGraphRunner's job).
    # We verify the origin injection -- SkillEvolutionService's responsibility.
    await real_skill_evolution_service.run_background_review(
        "s1", "digest of a non-trivial task"
    )
    fake_chat_complete.assert_trusted_metadata_origin("background_review")


@pytest.mark.asyncio
async def test_write_approval_staged_and_replay(
    real_skill_service_write_approval_on, tmp_skills_root
):
    r = await real_skill_service_write_approval_on.manage_skill(
        SkillManageRequestBuilder.create(
            name="demo", content="---\nname: demo\n---\nbody",
            origin=SkillWriteOrigin.FOREGROUND))
    assert r.staged
    r2 = await real_skill_service_write_approval_on.approve_pending(r.pending_id)
    assert r2.success
    assert (tmp_skills_root / "demo" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_backup_rollback_e2e(
    real_skill_service_with_backup, tmp_skills_root
):
    await real_skill_service_with_backup.manage_skill(
        SkillManageRequestBuilder.create(
            name="demo", content="---\nname: demo\n---\nv1",
            origin=SkillWriteOrigin.FOREGROUND))
    sid = await real_skill_service_with_backup.backup.snapshot()
    await real_skill_service_with_backup.manage_skill(
        SkillManageRequestBuilder.edit(
            name="demo", content="---\nname: demo\n---\nv2",
            origin=SkillWriteOrigin.FOREGROUND))
    await real_skill_service_with_backup.backup.rollback(sid)
    assert "v1" in (tmp_skills_root / "demo" / "SKILL.md").read_text()


@pytest.mark.asyncio
async def test_pin_blocks_bg_review(real_skill_service):
    await real_skill_service.manage_skill(
        SkillManageRequestBuilder.create(
            name="demo", content="---\nname: demo\n---\nbody",
            origin=SkillWriteOrigin.FOREGROUND))
    await real_skill_service.usage.set_pinned("demo", True)
    r = await real_skill_service.manage_skill(
        SkillManageRequestBuilder.patch(
            name="demo", old_string="body", new_string="new",
            origin=SkillWriteOrigin.BACKGROUND_REVIEW))
    assert not r.success  # pinned + bg_review -> deny
