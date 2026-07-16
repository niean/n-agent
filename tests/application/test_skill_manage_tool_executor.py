import pytest
from unittest.mock import AsyncMock, MagicMock
from app.application.skill_service import SkillManageToolExecutor, skill_manage_tool_definition
from app.domain.tool import ToolCallRequest, ToolExecutionContext
from app.domain.skill import SkillWriteOrigin, SkillWriteAction, SkillManageResult

def _ctx(trusted_origin=None):
    ctx = MagicMock()
    ctx.session_id = "s1"
    ctx.trusted_metadata = {"skill_write_origin": trusted_origin} if trusted_origin else {}
    ctx.metadata = {}
    return ctx

@pytest.mark.asyncio
async def test_origin_read_from_trusted_metadata():
    svc = MagicMock(); svc.manage_skill = AsyncMock()
    svc.manage_skill.return_value = SkillManageResult(success=True, staged=False, pending_id=None,
        skill_name="x", action=SkillWriteAction.CREATE, summary="", diff=None, error=None)
    ex = SkillManageToolExecutor(svc)
    req = ToolCallRequest(id="1", name="skill_manage",
        arguments={"action":"create","name":"x","content":"---\nname: x\n---\nb"})
    await ex.execute(req, _ctx(trusted_origin="background_review"))
    args, kwargs = svc.manage_skill.call_args
    assert args[0].origin == SkillWriteOrigin.BACKGROUND_REVIEW

@pytest.mark.asyncio
async def test_origin_defaults_foreground_when_absent():
    svc = MagicMock(); svc.manage_skill = AsyncMock()
    ex = SkillManageToolExecutor(svc)
    req = ToolCallRequest(id="1", name="skill_manage", arguments={"action":"create","name":"x","content":"---\nname: x\n---\nb"})
    await ex.execute(req, _ctx())
    assert svc.manage_skill.call_args.args[0].origin == SkillWriteOrigin.FOREGROUND

def test_skill_manage_tool_definition_safe():
    d = skill_manage_tool_definition()
    assert d.name == "skill_manage"
    assert d.risk_level.value == "safe"
    assert d.toolset == "skills"
