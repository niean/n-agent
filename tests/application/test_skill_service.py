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
        self.script_bytes = b"print('ok')\n"

    async def scan(self):
        return list(self.scan_skills), list(self.scan_warnings)

    async def render(self, skill, session_id=""):
        return self.rendered

    async def read_linked_file(self, skill, file_path):
        return self.linked

    async def list_linked_files(self, skill):
        return dict(self.linked_files)

    async def read_script_bytes(self, skill, script_relative_path):
        return self.script_bytes


@pytest.mark.asyncio
async def test_resolve_script_bytes_returns_immutable_path_free_facts():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("photo"))
    service = SkillService(registry, loader)
    result = await service.resolve_script_bytes("photo", "scripts/photo.py")
    assert result.skill_name == "photo"
    assert result.script_relative_path == "scripts/photo.py"
    assert result.content == loader.script_bytes
    assert len(result.sha256) == 64
    assert not hasattr(result, "path")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skill",
    [
        _skill("disabled", enabled=False),
        _skill("unsupported", readiness=SkillReadiness.UNSUPPORTED),
        _skill("scan-error", readiness=SkillReadiness.SCAN_ERROR),
    ],
)
async def test_resolve_script_bytes_requires_enabled_ready_scan_success(skill):
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(skill)
    service = SkillService(registry, loader)
    with pytest.raises(SkillNotFoundError):
        await service.resolve_script_bytes(skill.name, "scripts/photo.py")


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


@pytest.mark.asyncio
async def test_build_skills_index_groups_by_category():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha", relative_path="general/alpha/SKILL.md"))
    await registry.upsert_skill(_skill("beta", relative_path="coding/beta/SKILL.md"))
    await registry.upsert_skill(_skill("gamma", relative_path="coding/gamma/SKILL.md"))
    await registry.upsert_skill(_skill("delta", readiness=SkillReadiness.UNSUPPORTED, relative_path="general/delta/SKILL.md"))
    service = SkillService(registry, loader)
    idx = await service.build_skills_index()
    assert "## Available Skills" in idx
    # general and coding categories both present
    assert "- general:" in idx
    assert "- coding:" in idx
    # available skills listed
    assert "alpha" in idx
    assert "beta" in idx
    assert "gamma" in idx
    # unsupported skill excluded
    assert "delta" not in idx


@pytest.mark.asyncio
async def test_build_skills_index_empty_when_no_skills():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    idx = await service.build_skills_index()
    assert idx == ""


# ------------------------------------------------------------------
# convenience methods: list_pending / get_pending / reject_pending
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillPendingWrite, SkillWriteAction, SkillWriteOrigin
    registry, loader = FakeRegistry(), FakeLoader()
    pw = SkillPendingWrite(
        "pid1", SkillWriteAction.CREATE, "demo",
        SkillWriteOrigin.FOREGROUND, "create demo", "diff",
        {}, "pending", None, None, None,
    )
    pending = MagicMock()
    pending.list = AsyncMock(return_value=[pw])
    service = SkillService(registry, loader, pending=pending)
    result = await service.list_pending()
    assert result == [pw]


@pytest.mark.asyncio
async def test_list_pending_raises_without_store():
    registry, loader = FakeRegistry(), FakeLoader()
    service = SkillService(registry, loader)
    with pytest.raises(RuntimeError, match="pending store"):
        await service.list_pending()


@pytest.mark.asyncio
async def test_get_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillPendingWrite, SkillWriteAction, SkillWriteOrigin
    registry, loader = FakeRegistry(), FakeLoader()
    pw = SkillPendingWrite(
        "pid1", SkillWriteAction.CREATE, "demo",
        SkillWriteOrigin.FOREGROUND, "s", "d", {}, "pending", None, None, None,
    )
    pending = MagicMock()
    pending.get = AsyncMock(return_value=pw)
    service = SkillService(registry, loader, pending=pending)
    result = await service.get_pending("pid1")
    assert result is pw


@pytest.mark.asyncio
async def test_reject_pending_delegates_to_store():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    pending = MagicMock()
    pending.reject = AsyncMock(return_value=True)
    service = SkillService(registry, loader, pending=pending)
    result = await service.reject_pending("pid1")
    assert result is True


# ------------------------------------------------------------------
# convenience methods: list_usage / set_pinned
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_usage_iterates_skills():
    from unittest.mock import AsyncMock, MagicMock
    from app.domain.skill import SkillUsage
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    await registry.upsert_skill(_skill("beta"))
    usage = MagicMock()
    usage.get = AsyncMock(side_effect=lambda name: SkillUsage(
        created_by="foreground", use_count=1, view_count=2, patch_count=0,
        created_at=None, last_used_at=None, last_viewed=None,
        last_patched_at=None, state="active", pinned=False, archived_at=None,
    ) if name == "alpha" else None)
    service = SkillService(registry, loader, usage=usage)
    result = await service.list_usage()
    names = [name for name, _ in result]
    assert names == ["alpha"]


@pytest.mark.asyncio
async def test_set_pinned_validates_skill_exists():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    usage = MagicMock()
    usage.set_pinned = AsyncMock()
    service = SkillService(registry, loader, usage=usage)
    await service.set_pinned("alpha", True)
    usage.set_pinned.assert_awaited_once_with("alpha", True)


@pytest.mark.asyncio
async def test_set_pinned_raises_for_missing_skill():
    from unittest.mock import AsyncMock, MagicMock
    registry, loader = FakeRegistry(), FakeLoader()
    usage = MagicMock()
    usage.set_pinned = AsyncMock()
    service = SkillService(registry, loader, usage=usage)
    with pytest.raises(SkillNotFoundError):
        await service.set_pinned("missing", True)


@pytest.mark.asyncio
async def test_set_pinned_raises_without_usage_store():
    registry, loader = FakeRegistry(), FakeLoader()
    await registry.upsert_skill(_skill("alpha"))
    service = SkillService(registry, loader)
    with pytest.raises(RuntimeError, match="usage store"):
        await service.set_pinned("alpha", True)
