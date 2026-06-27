from app.infrastructure.memory.multi_project import MultiProjectMemory


def _make_project(tmp_path, name: str, memory_content: str) -> None:
    project_dir = tmp_path / "locals" / "external-memory" / name
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "memory.md").write_text(memory_content, encoding="utf-8")


def test_prefetch_merges_across_projects(tmp_path):
    _make_project(tmp_path, "proj_a", "Python 项目使用 FastAPI")
    _make_project(tmp_path, "proj_b", "Java 项目使用 Spring")
    mem = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    mem.initialize(session_id="")
    mem.set_enabled_projects(["proj_a", "proj_b"])
    result = mem.prefetch("Python FastAPI", session_id="s1")
    assert "## Project: proj_a" in result
    assert "Python 项目使用 FastAPI" in result
    assert "Spring" not in result


def test_prefetch_returns_empty_when_no_enabled_project(tmp_path):
    _make_project(tmp_path, "proj_a", "Python FastAPI")
    mem = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    mem.initialize(session_id="")
    mem.set_enabled_projects([])
    result = mem.prefetch("Python", session_id="s1")
    assert result == ""


def test_prefetch_returns_empty_when_no_match(tmp_path):
    _make_project(tmp_path, "proj_a", "天气晴朗阳光明媚")
    mem = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    mem.initialize(session_id="")
    mem.set_enabled_projects(["proj_a"])
    result = mem.prefetch("Python", session_id="s1")
    assert result == ""


def test_prefetch_returns_empty_when_query_empty(tmp_path):
    _make_project(tmp_path, "proj_a", "Python FastAPI")
    mem = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    mem.initialize(session_id="")
    mem.set_enabled_projects(["proj_a"])
    result = mem.prefetch("", session_id="s1")
    assert result == ""


def test_prefetch_same_project_multiple_entries_uses_single_prefix(tmp_path):
    content = "Python FastAPI\n---\nPython SQLAlchemy\n---\nJava Spring"
    _make_project(tmp_path, "proj_a", content)
    mem = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    mem.initialize(session_id="")
    mem.set_enabled_projects(["proj_a"])
    result = mem.prefetch("Python", session_id="s1")
    # Single project prefix even if multiple entries from same project
    assert result.count("## Project: proj_a") == 1
