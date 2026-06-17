from __future__ import annotations

from pathlib import Path

from app.infrastructure.skill.seed_runner import seed_default_skills


def test_seed_creates_n_agent_skill_when_absent(tmp_path: Path):
    seed_default_skills(tmp_path)
    target = tmp_path / "n-agent" / "SKILL.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "name: n-agent" in content
    assert "## Cron Jobs" in content


def test_seed_does_not_overwrite_existing_user_copy(tmp_path: Path):
    target = tmp_path / "n-agent" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("CUSTOMIZED", encoding="utf-8")
    seed_default_skills(tmp_path)
    assert target.read_text(encoding="utf-8") == "CUSTOMIZED"


def test_seed_is_idempotent_on_repeated_calls(tmp_path: Path):
    seed_default_skills(tmp_path)
    seed_default_skills(tmp_path)
    seed_default_skills(tmp_path)
    target = tmp_path / "n-agent" / "SKILL.md"
    assert target.exists()
