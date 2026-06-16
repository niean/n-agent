from pathlib import Path

from app.infrastructure.path_security import has_traversal_component, validate_within_dir


def test_has_traversal_component_detects_dotdot():
    assert has_traversal_component("../etc/passwd") is True
    assert has_traversal_component("a/../b") is True
    assert has_traversal_component("a/./b") is True


def test_has_traversal_component_allows_normal_relative():
    assert has_traversal_component("references/x.md") is False
    assert has_traversal_component("templates/sub/y.md") is False


def test_has_traversal_component_rejects_absolute(tmp_path):
    assert has_traversal_component(str(tmp_path / "x")) is True


def test_validate_within_dir_ok(tmp_path):
    root = tmp_path
    inside = (root / "a/b.md")
    inside.parent.mkdir(parents=True)
    inside.write_text("x")
    assert validate_within_dir(inside, root) is None


def test_validate_within_dir_outside(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    other = tmp_path / "other.md"
    other.write_text("x")
    err = validate_within_dir(other, root)
    assert err is not None
    assert "outside" in err.lower() or "not within" in err.lower()
