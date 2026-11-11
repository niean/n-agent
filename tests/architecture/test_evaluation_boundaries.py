# Evaluation 子域 DDD 边界测试：app/domain/evaluation/** 必须是纯领域代码，
# 禁止依赖 OTel / SQLite / Langfuse / FastAPI 等框架与外部 SDK，
# 禁止反向依赖 application / infrastructure / interfaces。
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVALUATION = ROOT / "app" / "domain" / "evaluation"

# 纯领域禁止导入的模块：外部 SDK / 框架 / 持久化 / 跨层依赖
FORBIDDEN_PREFIXES = (
    "opentelemetry",
    "sqlite3",
    "langfuse",
    "fastapi",
    "pydantic",
    "app.application",
    "app.infrastructure",
    "app.interfaces",
)


def _evaluation_module_files() -> list[Path]:
    # rglob 覆盖未来新增的子包
    return sorted(EVALUATION.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", _evaluation_module_files())
def test_evaluation_module_imports_pure(path: Path):
    modules = _imported_modules(path)
    violations = [
        m
        for m in modules
        if any(m == prefix or m.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    ]
    assert not violations, f"{path.relative_to(ROOT)} imports forbidden modules: {violations}"


def test_ports_module_has_no_application_or_infrastructure_imports():
    modules = _imported_modules(EVALUATION / "ports.py")
    cross_layer = (
        "app.application",
        "app.infrastructure",
        "app.interfaces",
    )
    violations = [
        m
        for m in modules
        if any(m == prefix or m.startswith(prefix + ".") for prefix in cross_layer)
    ]
    assert not violations, f"ports.py imports cross-layer modules: {violations}"


def test_evaluation_package_exists_with_expected_modules():
    names = {p.name for p in _evaluation_module_files()}
    assert {"__init__.py", "models.py", "state.py", "ports.py"} <= names


# ---------------------------------------------------------------------------
# 端口契约：五个 Protocol 全部为 async 方法（spec 5.1）
# ---------------------------------------------------------------------------

PORT_PROTOCOLS = (
    "EvaluationStore",
    "EvidenceStore",
    "Evaluator",
    "EvaluationTargetRunner",
    "ScoreSink",
)


def test_ports_declare_five_async_protocols():
    tree = ast.parse((EVALUATION / "ports.py").read_text())
    classes = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    for name in PORT_PROTOCOLS:
        assert name in classes, f"ports.py 缺少端口 Protocol: {name}"
        cls = classes[name]
        bases = {ast.unparse(base) for base in cls.bases}
        assert any("Protocol" in base for base in bases), (
            f"{name} 必须继承 typing.Protocol"
        )
        methods = [
            item
            for item in cls.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assert methods, f"{name} 必须至少声明一个端口方法"
        for method in methods:
            assert isinstance(method, ast.AsyncFunctionDef), (
                f"{name}.{method.name} 必须是 async 方法"
            )
