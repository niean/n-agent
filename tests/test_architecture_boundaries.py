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


def test_application_does_not_import_infrastructure():
    assert_no_forbidden_imports("app/application", ("app.infrastructure",))


def test_interfaces_do_not_import_infrastructure_or_sqlite():
    assert_no_forbidden_imports("app/interfaces", ("sqlite3", "app.infrastructure"))
