from __future__ import annotations

import asyncio

import pytest

from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness, SkillSource
from app.infrastructure.skill.file_loader import SkillFileLoader, SkillFileLoaderConfig


def _make_skill(name: str, source: SkillSource = SkillSource.AGENT) -> Skill:
    return Skill(
        id=name,
        name=name,
        relative_path=f"{name}/SKILL.md",
        description="test skill",
        platforms=[],
        frontmatter=SkillFrontmatter(
            name=name,
            description="test skill",
            version="1",
            platforms=[],
            tags=[],
            related_skills=[],
            author="",
            license="",
            setup_help=None,
            required_env_vars=[],
            raw={},
        ),
        enabled=True,
        readiness=SkillReadiness.AVAILABLE,
        last_scan_status=None,
        last_scan_error=None,
        last_seen_at=None,
        created_at=None,
        updated_at=None,
        source=source,
    )


def _create_skill_dir(root, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\n---\nbody\n", encoding="utf-8"
    )


def _loader(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    return SkillFileLoader(SkillFileLoaderConfig(root=root)), root


def test_delete_then_restore_roundtrip(tmp_path):
    loader, root = _loader(tmp_path)
    _create_skill_dir(root, "deploy-staging")
    asyncio.run(loader.delete_skill(_make_skill("deploy-staging")))
    archived = list((root / ".archive").iterdir())
    assert len(archived) == 1
    dest = asyncio.run(loader.restore_skill("deploy-staging"))
    assert dest == root / "deploy-staging"
    assert (dest / "SKILL.md").exists()
    assert not list((root / ".archive").iterdir())


def test_restore_not_found(tmp_path):
    loader, _ = _loader(tmp_path)
    with pytest.raises(FileNotFoundError):
        asyncio.run(loader.restore_skill("nonexistent"))


def test_restore_destination_exists(tmp_path):
    loader, root = _loader(tmp_path)
    _create_skill_dir(root, "old-skill")
    asyncio.run(loader.delete_skill(_make_skill("old-skill")))
    (root / "old-skill").mkdir()  # 阻塞恢复
    with pytest.raises(FileExistsError):
        asyncio.run(loader.restore_skill("old-skill"))


def test_list_archived_returns_dicts(tmp_path):
    loader, root = _loader(tmp_path)
    _create_skill_dir(root, "deploy-staging")
    asyncio.run(loader.delete_skill(_make_skill("deploy-staging")))
    archived = asyncio.run(loader.list_archived())
    assert len(archived) == 1
    entry = archived[0]
    assert entry["name"] == "deploy-staging"
    assert "archive_path" in entry
    assert "archived_at" in entry
    assert entry["archived_at"].startswith("20")


def test_restore_hyphenated_skill_name_no_false_match(tmp_path):
    """连字符 skill name：restore('deploy') 不误中 'deploy-staging-...'。"""
    loader, root = _loader(tmp_path)
    _create_skill_dir(root, "deploy-staging")
    asyncio.run(loader.delete_skill(_make_skill("deploy-staging")))
    with pytest.raises(FileNotFoundError):
        asyncio.run(loader.restore_skill("deploy"))
    dest = asyncio.run(loader.restore_skill("deploy-staging"))
    assert dest == root / "deploy-staging"


def test_list_archived_empty(tmp_path):
    loader, _ = _loader(tmp_path)
    assert asyncio.run(loader.list_archived()) == []


def test_list_archived_multiple_with_hyphens(tmp_path):
    """多个含连字符 skill 归档后 list_archived 正确解析各自 name。"""
    loader, root = _loader(tmp_path)
    _create_skill_dir(root, "deploy-staging")
    _create_skill_dir(root, "pr-triage-salvage")
    asyncio.run(loader.delete_skill(_make_skill("deploy-staging")))
    asyncio.run(loader.delete_skill(_make_skill("pr-triage-salvage")))
    archived = asyncio.run(loader.list_archived())
    names = sorted(e["name"] for e in archived)
    assert names == ["deploy-staging", "pr-triage-salvage"]
