import pytest
from app.infrastructure.skill.skill_backup_store import SkillBackupStore

@pytest.fixture
def store(tmp_path):
    return SkillBackupStore(root=tmp_path, keep=3)

@pytest.mark.asyncio
async def test_snapshot_excludes_backups_dir(store, tmp_path):
    (tmp_path/"demo").mkdir()
    (tmp_path/"demo"/"SKILL.md").write_text("x")
    sid = await store.snapshot()
    assert sid in await store.list()

@pytest.mark.asyncio
async def test_rollback_restores_and_archives_current(store, tmp_path):
    (tmp_path/"demo").mkdir()
    (tmp_path/"demo"/"SKILL.md").write_text("v1")
    sid = await store.snapshot()
    (tmp_path/"demo"/"SKILL.md").write_text("v2")
    await store.rollback(sid)
    assert (tmp_path/"demo"/"SKILL.md").read_text() == "v1"
    assert (tmp_path/".archive").is_dir()

@pytest.mark.asyncio
async def test_keep_prunes_old_snapshots(store, tmp_path):
    for i in range(5):
        (tmp_path/"demo").mkdir(exist_ok=True)
        (tmp_path/"demo"/"SKILL.md").write_text(f"v{i}")
        await store.snapshot()
    assert len(await store.list()) <= 3
