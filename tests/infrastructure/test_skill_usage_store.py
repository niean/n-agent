import pytest
from app.infrastructure.skill.skill_usage_store import SkillUsageStore
from app.domain.skill import SkillUsage

@pytest.fixture
def store(tmp_path):
    return SkillUsageStore(str(tmp_path/"usage.db"))

@pytest.mark.asyncio
async def test_get_missing_returns_none(store):
    assert await store.get("x") is None

@pytest.mark.asyncio
async def test_upsert_and_get(store):
    await store.upsert("x", SkillUsage(created_by="foreground", use_count=0, view_count=0,
        patch_count=0, created_at=None, last_used_at=None, last_viewed=None,
        last_patched_at=None, state="active", pinned=False, archived_at=None))
    u = await store.get("x")
    assert u.state == "active"

@pytest.mark.asyncio
async def test_increment_patch_and_set_pinned(store):
    await store.upsert("x", SkillUsage("foreground",0,0,0,None,None,None,None,"active",False,None))
    await store.increment_patch("x")
    await store.set_pinned("x", True)
    u = await store.get("x")
    assert u.patch_count == 1 and u.pinned is True
