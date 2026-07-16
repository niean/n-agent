import pytest
from app.infrastructure.skill.skill_pending_store import SkillPendingStore
from app.domain.skill import SkillPendingWrite, SkillWriteAction, SkillWriteOrigin

@pytest.fixture
def store(tmp_path):
    return SkillPendingStore(str(tmp_path/"pending.db"))

def _w(pid="p1"):
    return SkillPendingWrite(pid, SkillWriteAction.PATCH, "x", SkillWriteOrigin.FOREGROUND,
                             "s", "d", {"name":"x"}, "pending", None, None, None)

@pytest.mark.asyncio
async def test_stage_list_get(store):
    pid = await store.stage(_w())
    rows = await store.list()
    assert len(rows) == 1 and rows[0].state == "pending"
    assert (await store.get(pid)).skill_name == "x"

@pytest.mark.asyncio
async def test_approve_take_atomic_and_idempotent(store):
    pid = await store.stage(_w())
    first = await store.approve_take(pid)
    assert first is not None and first.state == "pending"
    second = await store.approve_take(pid)
    assert second is None  # 已 take，幂等返回 None

@pytest.mark.asyncio
async def test_reject(store):
    pid = await store.stage(_w())
    assert await store.reject(pid) is True
    assert await store.get(pid) is None
