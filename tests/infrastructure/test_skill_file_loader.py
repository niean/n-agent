import pytest
from app.domain.skill import SkillValidationError
from app.infrastructure.skill.file_loader import SkillFileLoader, SkillFileLoaderConfig


def _write_skill(root, rel, body):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_scan_basic_and_nested(tmp_path):
    _write_skill(tmp_path, "alpha/SKILL.md", "---\nname: alpha\ndescription: a\nplatforms: [linux]\n---\nbody\n")
    _write_skill(tmp_path, "mlops/wandb/SKILL.md", "---\nname: wandb\nplatforms: [linux]\n---\nbody\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, warnings = await loader.scan()
    names = {s.name for s in skills}
    assert names == {"alpha", "wandb"}
    assert warnings == []


@pytest.mark.asyncio
async def test_name_fallback_to_dir(tmp_path):
    _write_skill(tmp_path, "fallback/SKILL.md", "---\ndescription: x\nplatforms: [linux]\n---\nbody\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, warnings = await loader.scan()
    assert {s.name for s in skills} == {"fallback"}
    assert warnings == []


@pytest.mark.asyncio
async def test_duplicate_name_warning(tmp_path):
    _write_skill(tmp_path, "a/SKILL.md", "---\nname: dup\nplatforms: [linux]\n---\n")
    _write_skill(tmp_path, "b/SKILL.md", "---\nname: dup\nplatforms: [linux]\n---\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, warnings = await loader.scan()
    assert len(skills) == 1
    assert any(w.reason == "duplicate_name" for w in warnings)


@pytest.mark.asyncio
async def test_yaml_error_warning(tmp_path):
    _write_skill(tmp_path, "broken/SKILL.md", "---\nname: [oops\n---\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, warnings = await loader.scan()
    assert skills == []
    assert any(w.reason == "yaml_error" for w in warnings)


@pytest.mark.asyncio
async def test_platform_unsupported(tmp_path):
    _write_skill(tmp_path, "winonly/SKILL.md", "---\nname: winonly\nplatforms: [windows]\n---\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, warnings = await loader.scan()
    assert len(skills) == 1
    from app.domain.skill import SkillReadiness
    assert skills[0].readiness is SkillReadiness.UNSUPPORTED


@pytest.mark.asyncio
async def test_platform_default_means_all_platforms(tmp_path):
    """缺省/空 platforms 必须视为全平台兼容（与 Hermes skill_matches_platform 一致）。"""
    _write_skill(tmp_path, "any/SKILL.md", "---\nname: any\ndescription: x\n---\nbody\n")
    _write_skill(tmp_path, "empty/SKILL.md", "---\nname: empty\nplatforms: []\n---\nbody\n")
    from app.domain.skill import SkillReadiness
    for current in ("darwin", "linux", "win32"):
        loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform=current))
        skills, _ = await loader.scan()
        assert {s.name for s in skills} == {"any", "empty"}
        assert all(s.readiness is SkillReadiness.AVAILABLE for s in skills), current


@pytest.mark.asyncio
async def test_platform_macos_maps_to_darwin(tmp_path):
    _write_skill(tmp_path, "macskill/SKILL.md", "---\nname: macskill\nplatforms: [macos]\n---\nbody\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="darwin"))
    skills, _ = await loader.scan()
    from app.domain.skill import SkillReadiness
    assert skills[0].readiness is SkillReadiness.AVAILABLE


@pytest.mark.asyncio
async def test_platform_linux_skill_unsupported_on_macos(tmp_path):
    _write_skill(tmp_path, "linonly/SKILL.md", "---\nname: linonly\nplatforms: [linux]\n---\nbody\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="darwin"))
    skills, _ = await loader.scan()
    from app.domain.skill import SkillReadiness
    assert skills[0].readiness is SkillReadiness.UNSUPPORTED


@pytest.mark.asyncio
async def test_render_view_substitutes_macros(tmp_path):
    _write_skill(tmp_path, "demo/SKILL.md", "---\nname: demo\nplatforms: [linux]\n---\nDIR=${HERMES_SKILL_DIR} SID=${HERMES_SESSION_ID}\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, _ = await loader.scan()
    rendered = await loader.render(skills[0], session_id="sess-1")
    assert str((tmp_path / "demo").resolve()) in rendered
    assert "SID=sess-1" in rendered


@pytest.mark.asyncio
async def test_render_linked_file_traversal_rejected(tmp_path):
    _write_skill(tmp_path, "demo/SKILL.md", "---\nname: demo\nplatforms: [linux]\n---\n")
    (tmp_path / "demo" / "references").mkdir()
    (tmp_path / "demo" / "references" / "x.md").write_text("ref-content")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, _ = await loader.scan()
    ok = await loader.read_linked_file(skills[0], "references/x.md")
    assert ok == "ref-content"
    with pytest.raises(
        SkillValidationError,
        match=r"^Path traversal \('\.\.'\) is not allowed\.$",
    ):
        await loader.read_linked_file(skills[0], "../escape.md")


@pytest.mark.asyncio
async def test_excluded_dirs(tmp_path):
    _write_skill(tmp_path, ".git/skill/SKILL.md", "---\nname: gitskill\nplatforms: [linux]\n---\n")
    _write_skill(tmp_path, ".archive/old/SKILL.md", "---\nname: oldskill\nplatforms: [linux]\n---\n")
    _write_skill(tmp_path, "kept/SKILL.md", "---\nname: kept\nplatforms: [linux]\n---\n")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, _ = await loader.scan()
    assert {s.name for s in skills} == {"kept"}


@pytest.mark.asyncio
async def test_read_script_bytes_requires_scripts_link_and_rejects_symlinks(tmp_path):
    _write_skill(tmp_path, "demo/SKILL.md", "---\nname: demo\nplatforms: [linux]\n---\n")
    scripts = tmp_path / "demo" / "scripts"
    scripts.mkdir()
    script = scripts / "photo.py"
    script.write_bytes(b"payload")
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, _ = await loader.scan()
    assert await loader.read_script_bytes(skills[0], "scripts/photo.py") == b"payload"
    with pytest.raises(SkillValidationError, match="^skill_script_path_denied$"):
        await loader.read_script_bytes(skills[0], "references/photo.py")
    with pytest.raises(SkillValidationError, match="^skill_script_path_denied$"):
        await loader.read_script_bytes(skills[0], "scripts/../photo.py")
    script.unlink()
    script.symlink_to(tmp_path / "outside.py")
    with pytest.raises(SkillValidationError, match="^skill_script_path_denied$"):
        await loader.read_script_bytes(skills[0], "scripts/photo.py")


@pytest.mark.asyncio
async def test_read_script_bytes_rejects_intermediate_symlink(tmp_path):
    _write_skill(tmp_path, "demo/SKILL.md", "---\nname: demo\nplatforms: [linux]\n---\n")
    real = tmp_path / "real-scripts"
    real.mkdir()
    (real / "photo.py").write_bytes(b"payload")
    (tmp_path / "demo" / "scripts").symlink_to(real, target_is_directory=True)
    loader = SkillFileLoader(SkillFileLoaderConfig(root=tmp_path, current_platform="linux"))
    skills, _ = await loader.scan()
    with pytest.raises(SkillValidationError, match="^skill_script_path_denied$"):
        await loader.read_script_bytes(skills[0], "scripts/photo.py")


@pytest.mark.asyncio
async def test_read_script_bytes_missing_file_preserves_stable_not_found(tmp_path):
    _write_skill(
        tmp_path,
        "demo/SKILL.md",
        "---\nname: demo\nplatforms: [linux]\n---\n",
    )
    (tmp_path / "demo" / "scripts").mkdir()
    loader = SkillFileLoader(
        SkillFileLoaderConfig(root=tmp_path, current_platform="linux")
    )
    skills, _ = await loader.scan()

    with pytest.raises(FileNotFoundError) as caught:
        await loader.read_script_bytes(skills[0], "scripts/missing.py")
    assert caught.value.filename == "missing.py"
