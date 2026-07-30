import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def assert_no_forbidden_imports(directory: str, forbidden: tuple[str, ...]):
    for path in (ROOT / directory).rglob("*.py"):
        modules = imported_modules(path)
        violations = [module for module in modules if module in forbidden or module.startswith(forbidden)]
        assert not violations, f"{path.relative_to(ROOT)} imports forbidden modules: {violations}"


def test_domain_has_no_framework_or_infrastructure_imports():
    assert_no_forbidden_imports(
        "app/domain",
        ("fastapi", "langgraph", "sqlite3", "openai", "app.infrastructure"),
    )


def test_policy_domain_modules_do_not_cross_domain_boundary():
    for module in ("policy.py", "tool_policy.py", "budget.py", "budget_policy.py", "gateway_policy.py", "memory_policy.py", "turn_policy.py", "sandbox_policy.py", "schedule_policy.py"):
        modules = imported_modules(ROOT / "app/domain" / module)
        forbidden = (
            "app.application",
            "app.infrastructure",
            "app.interfaces",
            "langgraph",
            "acp",
        )
        violations = [
            imported
            for imported in modules
            if imported in forbidden or imported.startswith(forbidden)
        ]
        assert not violations, f"app/domain/{module} imports forbidden modules: {violations}"


def test_context_policy_does_not_import_memory_or_tool_policy():
    """T7: context_policy.py is pure Domain -- must not import memory_policy,
    tool_policy, MemoryStore, pydantic, or Infrastructure."""
    modules = imported_modules(ROOT / "app/domain" / "context_policy.py")
    forbidden = (
        "app.domain.memory_policy",
        "app.domain.tool_policy",
        "app.domain.memory",
        "app.domain.tool",
        "app.application",
        "app.infrastructure",
        "app.interfaces",
        "pydantic",
        "langgraph",
        "fastapi",
        "openai",
    )
    violations = [
        imported
        for imported in modules
        if imported in forbidden or imported.startswith(forbidden)
    ]
    assert not violations, f"app/domain/context_policy.py imports forbidden modules: {violations}"


def test_llm_policy_does_not_import_context_or_information_flow():
    """T8: llm_policy.py is pure Domain -- must not import context_policy,
    information_flow_policy, pydantic, or Infrastructure."""
    modules = imported_modules(ROOT / "app/domain" / "llm_policy.py")
    forbidden = (
        "app.domain.context_policy",
        "app.domain.context",
        "app.domain.information_flow",
        "app.domain.information_flow_policy",
        "app.application",
        "app.infrastructure",
        "app.interfaces",
        "pydantic",
        "langgraph",
        "fastapi",
        "openai",
    )
    violations = [
        imported
        for imported in modules
        if imported in forbidden or imported.startswith(forbidden)
    ]
    assert not violations, f"app/domain/llm_policy.py imports forbidden modules: {violations}"


def test_turn_policy_pure_domain():
    """T9: turn_policy.py is pure Domain -- no LangGraph, no asyncio, no
    pydantic, no Infrastructure, no time calls. Only stdlib + app.domain."""
    modules = imported_modules(ROOT / "app/domain" / "turn_policy.py")
    forbidden = (
        "app.application",
        "app.infrastructure",
        "app.interfaces",
        "langgraph",
        "acp",
        "asyncio",
        "pydantic",
        "fastapi",
        "openai",
        "time",
    )
    violations = [
        imported
        for imported in modules
        if imported in forbidden or imported.startswith(forbidden)
    ]
    assert not violations, f"app/domain/turn_policy.py imports forbidden modules: {violations}"


def test_schedule_policy_pure_domain():
    """T11: schedule_policy.py is pure Domain -- no sqlite, no pydantic,
    no Infrastructure, no asyncio.  Only stdlib + app.domain."""
    modules = imported_modules(ROOT / "app/domain" / "schedule_policy.py")
    forbidden = (
        "app.application",
        "app.infrastructure",
        "app.interfaces",
        "langgraph",
        "acp",
        "asyncio",
        "pydantic",
        "fastapi",
        "openai",
        "sqlite3",
    )
    violations = [
        imported
        for imported in modules
        if imported in forbidden or imported.startswith(forbidden)
    ]
    assert not violations, f"app/domain/schedule_policy.py imports forbidden modules: {violations}"


def test_application_does_not_import_infrastructure():
    assert_no_forbidden_imports("app/application", ("app.infrastructure",))


def test_interfaces_do_not_import_infrastructure_or_sqlite():
    assert_no_forbidden_imports("app/interfaces", ("sqlite3", "app.infrastructure"))


def test_browser_host_runtime_does_not_import_interfaces():
    modules = imported_modules(ROOT / "app/browser_host_runtime.py")
    violations = [
        module
        for module in modules
        if module == "app.interfaces" or module.startswith("app.interfaces.")
    ]
    assert not violations


def test_browser_host_protocol_dependency_direction():
    protocol = ROOT / "app/infrastructure/browser/host_protocol.py"
    client = ROOT / "app/infrastructure/browser/host_cdp_backend.py"
    server = ROOT / "app/infrastructure/browser/host_bridge_server.py"

    forbidden_protocol_imports = (
        "app.infrastructure.browser.host_cdp_backend",
        "app.infrastructure.browser.host_bridge_server",
        "app.interfaces",
    )
    assert not [
        module
        for module in imported_modules(protocol)
        if module in forbidden_protocol_imports
        or module.startswith(forbidden_protocol_imports)
    ]
    assert (
        "app.infrastructure.browser.host_cdp_backend"
        not in imported_modules(server)
    )
    for consumer in (client, server):
        assert (
            "app.infrastructure.browser.host_protocol"
            in imported_modules(consumer)
        )


def test_skill_domain_pure():
    text = (ROOT / "app/domain/skill.py").read_text()
    for forbidden in (
        "import sqlite3",
        "import fastapi",
        "import langgraph",
        "import openai",
        "from app.infrastructure",
        "from app.interfaces",
    ):
        assert forbidden not in text, f"forbidden import in domain/skill.py: {forbidden}"


def test_skill_application_no_infrastructure():
    text = (ROOT / "app/application/skill_service.py").read_text()
    assert "from app.infrastructure" not in text


def test_plugin_domain_pure():
    text = (ROOT / "app/domain/plugin.py").read_text()
    for forbidden in (
        "import sqlite3",
        "import fastapi",
        "import langgraph",
        "import openai",
        "from app.infrastructure",
        "from app.interfaces",
    ):
        assert forbidden not in text, f"forbidden import in domain/plugin.py: {forbidden}"


def test_plugin_application_no_infrastructure():
    text = (ROOT / "app/application/plugin_service.py").read_text()
    assert "from app.infrastructure" not in text
