from __future__ import annotations

from pathlib import Path

from app.infrastructure.plugin.seed_runner import seed_default_plugins


def test_seed_default_plugins_idempotent(tmp_path: Path):
    target = tmp_path / "plugins"
    seed_default_plugins(target)
    assert (target / "hello" / "plugin.yaml").exists()
    assert (target / "hello" / "__init__.py").exists()
    assert (target / "hello" / "schemas.py").exists()
    assert (target / "hello" / "tools.py").exists()

    # 第二次不覆盖
    override = (target / "hello" / "plugin.yaml")
    override.write_text("user-modified")
    seed_default_plugins(target)
    assert override.read_text() == "user-modified"


def test_seed_default_plugins_skips_existing_directory(tmp_path: Path):
    target = tmp_path / "plugins"
    seed_default_plugins(target)
    # 再次调用，已有文件保留
    seed_default_plugins(target)
    assert (target / "hello" / "plugin.yaml").exists()


def test_seed_default_plugins_creates_root(tmp_path: Path):
    target = tmp_path / "nested" / "plugins"
    seed_default_plugins(target)
    assert target.exists()
    assert (target / "hello" / "plugin.yaml").exists()
