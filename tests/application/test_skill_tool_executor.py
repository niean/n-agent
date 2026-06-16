import json
import pytest
from datetime import datetime, timezone

from app.application.skill_service import (
    SkillService,
    SkillToolExecutor,
    skill_tool_definitions,
)
from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness
from app.domain.tool import RiskLevel, ToolCallRequest, ToolResultStatus, ToolSourceType


def _skill(name, readiness=SkillReadiness.AVAILABLE, enabled=True, relative_path=None):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=relative_path or f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=enabled, readiness=readiness, last_scan_status="ok",
        last_scan_error=None, last_seen_at=None,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )


class FakeRegistry:
    def __init__(self):
        self.store = {}

    async def list_skills(self, include_disabled=True):
        items = list(self.store.values())
        return items if include_disabled else [s for s in items if s.enabled]

    async def get_skill(self, name):
        return self.store.get(name)

    async def upsert_skill(self, s):
        self.store[s.name] = s
        return s

    async def delete_skill(self, name):
        return self.store.pop(name, None) is not None

    async def set_enabled(self, name, enabled):
        from dataclasses import replace
        s = self.store[name]
        self.store[name] = replace(s, enabled=enabled)
        return self.store[name]

    async def replace_all_skills(self, skills):
        self.store = {s.name: s for s in skills}
        return list(self.store.values())


class FakeLoader:
    async def scan(self):
        return [], []

    async def render(self, s, session_id=""):
        return "BODY"

    async def read_linked_file(self, s, fp):
        return "LINKED-" + fp

    async def list_linked_files(self, s):
        return {"references": [], "templates": [], "scripts": [], "assets": []}


def test_skill_tool_definitions_metadata():
    defs = {d.name: d for d in skill_tool_definitions()}
    assert set(defs.keys()) == {"skills_list", "skill_view"}
    for d in defs.values():
        assert d.risk_level is RiskLevel.SAFE
        assert d.source_type is ToolSourceType.BUILTIN
        assert d.toolset == "skills"
        assert d.input_schema.get("type") == "object"


@pytest.mark.asyncio
async def test_skills_list_returns_visible_only():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    await registry.upsert_skill(_skill("b", enabled=False))
    await registry.upsert_skill(_skill("c", readiness=SkillReadiness.UNSUPPORTED))
    service = SkillService(registry, loader)
    executor = SkillToolExecutor(service)
    result = await executor.execute(ToolCallRequest(id="1", name="skills_list", arguments={}))
    assert result.status is ToolResultStatus.SUCCESS
    payload = json.loads(result.content) if isinstance(result.content, str) else result.content
    assert payload["success"] is True
    assert {item["name"] for item in payload["skills"]} == {"a"}
    assert payload["count"] == 1
    assert "hint" in payload and "categories" in payload


@pytest.mark.asyncio
async def test_skills_list_filters_by_category():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha", relative_path="mlops/alpha/SKILL.md"))
    await registry.upsert_skill(_skill("beta", relative_path="ops/beta/SKILL.md"))
    service = SkillService(registry, loader)
    executor = SkillToolExecutor(service)
    result = await executor.execute(
        ToolCallRequest(id="1", name="skills_list", arguments={"category": "mlops"})
    )
    payload = json.loads(result.content) if isinstance(result.content, str) else result.content
    names = [s["name"] for s in payload["skills"]]
    assert names == ["alpha"]
    assert payload["categories"] == ["mlops"]


@pytest.mark.asyncio
async def test_skill_view_returns_rendered_content():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    executor = SkillToolExecutor(service)
    result = await executor.execute(ToolCallRequest(id="1", name="skill_view", arguments={"name": "a"}))
    payload = json.loads(result.content) if isinstance(result.content, str) else result.content
    assert payload["success"] is True
    assert payload["content"] == "BODY"


@pytest.mark.asyncio
async def test_skill_view_with_file_path():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    executor = SkillToolExecutor(service)
    result = await executor.execute(ToolCallRequest(
        id="1", name="skill_view", arguments={"name": "a", "file_path": "references/x.md"}
    ))
    payload = json.loads(result.content) if isinstance(result.content, str) else result.content
    assert payload["success"] is True
    assert payload["file"] == "references/x.md"
    assert payload["content"] == "LINKED-references/x.md"


@pytest.mark.asyncio
async def test_skill_view_unknown_returns_available_list():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    executor = SkillToolExecutor(service)
    result = await executor.execute(ToolCallRequest(id="1", name="skill_view", arguments={"name": "missing"}))
    payload = json.loads(result.content) if isinstance(result.content, str) else result.content
    assert payload["success"] is False
    assert "available" in payload
