import json
import pytest
from datetime import datetime, timezone

from app.application.skill_service import (
    SkillInput,
    SkillService,
    SkillScanReport,
    SkillScanWarning,
)
from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness, SkillNotFoundError


def _skill(name, readiness=SkillReadiness.AVAILABLE, enabled=True, relative_path=None):
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name, "description": "d"},
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

    async def upsert_skill(self, skill):
        self.store[skill.name] = skill
        return skill

    async def delete_skill(self, name):
        return self.store.pop(name, None) is not None

    async def set_enabled(self, name, enabled):
        s = self.store.get(name)
        if s is None:
            raise SkillNotFoundError(name)
        from dataclasses import replace
        updated = replace(s, enabled=enabled)
        self.store[name] = updated
        return updated

    async def replace_all_skills(self, skills):
        old = self.store
        self.store = {}
        for s in skills:
            prev = old.get(s.name)
            from dataclasses import replace
            self.store[s.name] = replace(s, enabled=prev.enabled if prev else s.enabled)
        return list(self.store.values())


class FakeLoader:
    def __init__(self):
        self.scan_skills = []
        self.scan_warnings = []
        self.rendered = "RENDERED"
        self.linked = "LINKED"
        self.linked_files = {"references": [], "templates": [], "scripts": [], "assets": []}

    async def scan(self):
        return list(self.scan_skills), list(self.scan_warnings)

    async def render(self, skill, session_id=""):
        return self.rendered

    async def read_linked_file(self, skill, file_path):
        return self.linked

    async def list_linked_files(self, skill):
        return dict(self.linked_files)


@pytest.mark.asyncio
async def test_scan_now_persists_and_returns_report():
    registry, loader = FakeRegistry(), FakeLoader()
    loader.scan_skills = [_skill("a"), _skill("b")]
    loader.scan_warnings = [SkillScanWarning("a/SKILL.md", "duplicate_name", first_path="x")]
    service = SkillService(registry, loader)
    report = await service.scan_now()
    assert isinstance(report, SkillScanReport)
    assert report.skills_count == 2
    assert len(report.warnings) == 1


@pytest.mark.asyncio
async def test_render_view_returns_payload():
    registry, loader = FakeRegistry(), FakeLoader()
    s = _skill("a")
    await registry.upsert_skill(s)
    service = SkillService(registry, loader)
    payload = await service.render_view("a", session_id="sess-1")
    assert payload["success"] is True
    assert payload["name"] == "a"
    assert payload["content"] == "RENDERED"
    assert "linked_files" in payload


@pytest.mark.asyncio
async def test_render_view_disabled_returns_failure():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a", enabled=False))
    service = SkillService(registry, loader)
    payload = await service.render_view("a")
    assert payload["success"] is False
    assert "disabled" in payload["error"].lower()


@pytest.mark.asyncio
async def test_render_view_unsupported_returns_failure():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a", readiness=SkillReadiness.UNSUPPORTED))
    service = SkillService(registry, loader)
    payload = await service.render_view("a")
    assert payload["success"] is False
    assert payload["readiness"] == "unsupported"


@pytest.mark.asyncio
async def test_render_view_missing_returns_available_list():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    payload = await service.render_view("missing")
    assert payload["success"] is False
    assert "available" in payload


@pytest.mark.asyncio
async def test_render_linked_file_returns_content():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    payload = await service.render_linked_file("a", "references/x.md")
    assert payload["success"] is True
    assert payload["content"] == "LINKED"
    assert payload["file"] == "references/x.md"


@pytest.mark.asyncio
async def test_set_enabled_updates_registry():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    service = SkillService(registry, loader)
    updated = await service.set_enabled("a", False)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_create_update_delete_skill_metadata():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    created = await service.create_skill(
        SkillInput(
            name="manual",
            relative_path="manual/SKILL.md",
            description="Manual skill",
            platforms=["linux"],
            frontmatter={"tags": ["ops"]},
        )
    )
    assert created.name == "manual"
    assert created.frontmatter.raw["tags"] == ["ops"]

    updated = await service.update_skill(
        "manual",
        SkillInput(
            name="manual",
            relative_path="manual/SKILL.md",
            description="Updated",
            platforms=["linux", "darwin"],
            enabled=False,
        )
    )
    assert updated.description == "Updated"
    assert updated.enabled is False
    assert updated.platforms == ["linux", "darwin"]

    await service.delete_skill("manual")
    assert await registry.get_skill("manual") is None


@pytest.mark.asyncio
async def test_create_skill_rejects_path_traversal():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    with pytest.raises(Exception):
        await service.create_skill(SkillInput(name="bad", relative_path="../SKILL.md"))


@pytest.mark.asyncio
async def test_list_skills_filters_unsupported_for_llm():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("a"))
    await registry.upsert_skill(_skill("b", readiness=SkillReadiness.UNSUPPORTED))
    await registry.upsert_skill(_skill("c", enabled=False))
    service = SkillService(registry, loader)
    visible = await service.list_for_llm()
    assert {s.name for s in visible} == {"a"}
