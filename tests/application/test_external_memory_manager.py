from app.application.external_memory_manager import ExternalMemoryManager
from app.infrastructure.memory.multi_project import MultiProjectMemory


def test_multi_project_memory_uses_selected_project_names(tmp_path):
    project_dir = tmp_path / "locals" / "external-memory" / "project_memory_1"
    project_dir.mkdir(parents=True)
    (project_dir / "memory.md").write_text("project_memory_1 says hello", encoding="utf-8")

    memory = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    memory.initialize(session_id="")
    manager = ExternalMemoryManager()
    manager.add_provider(memory)

    prompt = manager.build_system_prompt(enabled_override=["builtin", "project_memory_1"])

    assert "## External Memory: project_memory_1" in prompt
    assert "project_memory_1 says hello" in prompt


def test_multi_project_memory_is_not_enabled_by_builtin_default(tmp_path):
    project_dir = tmp_path / "locals" / "external-memory" / "project_memory_1"
    project_dir.mkdir(parents=True)
    (project_dir / "memory.md").write_text("project memory should stay hidden", encoding="utf-8")

    memory = MultiProjectMemory(tmp_path, memory_base_path="./locals/external-memory")
    memory.initialize(session_id="")
    manager = ExternalMemoryManager()
    manager.add_provider(memory)

    prompt = manager.build_system_prompt(enabled_override=["builtin"])

    assert "project memory should stay hidden" not in prompt
