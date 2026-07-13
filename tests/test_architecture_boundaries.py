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
    for module in ("policy.py", "tool_policy.py"):
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


def test_application_does_not_import_infrastructure():
    assert_no_forbidden_imports("app/application", ("app.infrastructure",))


def test_interfaces_do_not_import_infrastructure_or_sqlite():
    assert_no_forbidden_imports("app/interfaces", ("sqlite3", "app.infrastructure"))


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
