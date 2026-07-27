"""Architecture boundary tests for the Policy governance mesh (T12).

Asserts via AST scanning:
1. Domain Policy files are pure -- no Application/Infrastructure imports.
2. One domain Policy does NOT import another domain Policy.
3. RunPolicySnapshot does not hold RunBudgetAccount / approval pending /
   manager / store (only immutable per-run facts + typed configs).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = ROOT / "app" / "domain"
APPLICATION = ROOT / "app" / "application"

# All domain Policy files (the domain Policies + shared kernel).
POLICY_FILES = [
    "policy.py",
    "turn_policy.py",
    "context_policy.py",
    "llm_policy.py",
    "tool_policy.py",
    "memory_policy.py",
    "sandbox_policy.py",
    "gateway_policy.py",
    "schedule_policy.py",
    "budget_policy.py",
    "information_flow_policy.py",
    "host_terminal_policy.py",
    "skill_policy.py",
    "curator_policy.py",
    "task_policy.py",
    "browser_policy.py",
]

# Domain types files consumed by Policies (not Policy files themselves).
POLICY_DOMAIN_TYPE_FILES = {
    "budget.py",
    "information_flow.py",
}

# The set of Policy module names (for cross-import detection).
POLICY_MODULE_NAMES = {
    f"app.domain.{f[:-3]}" for f in POLICY_FILES if f != "policy.py"
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# ---------------------------------------------------------------------------
# 1. Domain Policy purity -- no Application / Infrastructure imports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", POLICY_FILES)
def test_domain_policy_file_has_no_application_or_infrastructure_imports(filename: str):
    path = DOMAIN / filename
    modules = _imported_modules(path)
    forbidden = (
        "app.application",
        "app.infrastructure",
        "app.interfaces",
        "fastapi",
        "langgraph",
        "pydantic",
        "openai",
        "sqlite3",
        "acp",
        "asyncio",
        "playwright",
    )
    violations = [
        m for m in modules
        if m in forbidden or m.startswith(forbidden)
    ]
    assert not violations, f"app/domain/{filename} imports forbidden modules: {violations}"


# ---------------------------------------------------------------------------
# 2. No cross-domain Policy imports
# ---------------------------------------------------------------------------

# Each Policy file may import the shared kernel (app.domain.policy) and its
# own domain types, but MUST NOT import another domain's Policy class.
POLICY_FILE_OWN_DOMAIN = {
    "turn_policy.py": {"app.domain.agent"},
    "context_policy.py": set(),  # context_policy has no domain type deps
    "llm_policy.py": {"app.domain.provider"},
    "tool_policy.py": {"app.domain.tool"},
    "memory_policy.py": set(),  # only imports app.domain.policy
    "sandbox_policy.py": set(),
    "gateway_policy.py": {"app.domain.gateway"},
    "schedule_policy.py": {"app.domain.schedule"},
    "budget_policy.py": {"app.domain.budget"},
    "information_flow_policy.py": {"app.domain.information_flow"},
    "host_terminal_policy.py": {"app.domain.host_terminal"},
    "skill_policy.py": {"app.domain.skill"},
    "curator_policy.py": {"app.domain.skill"},
    "task_policy.py": {"app.domain.task"},
    "browser_policy.py": {"app.domain.browser"},
}


@pytest.mark.parametrize(
    "filename",
    [f for f in POLICY_FILES if f != "policy.py"],
)
def test_policy_does_not_import_other_policy(filename: str):
    """A domain Policy MUST NOT import another domain Policy module."""
    path = DOMAIN / filename
    modules = _imported_modules(path)

    # Other policy modules that this file must not import.
    other_policies = POLICY_MODULE_NAMES - {f"app.domain.{filename[:-3]}"}

    violations = [
        m for m in modules
        if m in other_policies or any(m.startswith(p + ".") for p in other_policies)
    ]
    assert not violations, (
        f"app/domain/{filename} imports another domain Policy: {violations}"
    )


@pytest.mark.parametrize(
    "filename",
    [f for f in POLICY_FILES if f != "policy.py"],
)
def test_policy_only_imports_shared_kernel_and_own_domain_types(filename: str):
    """A domain Policy may import app.domain.policy (shared kernel) and its
    own domain types, but no other domain's types that belong to a different
    Policy's subdomain."""
    path = DOMAIN / filename
    modules = _imported_modules(path)
    own = POLICY_FILE_OWN_DOMAIN.get(filename, set())

    # Allowed: stdlib, app.domain.policy, own domain types
    allowed_prefixes = {"app.domain.policy"} | own

    for m in modules:
        if m.startswith("app.domain."):
            # Must be either policy.py shared kernel or own domain types
            is_allowed = any(
                m == prefix or m.startswith(prefix + ".")
                for prefix in allowed_prefixes
            )
            assert is_allowed, (
                f"app/domain/{filename} imports cross-domain type module: {m}"
            )


# ---------------------------------------------------------------------------
# 3. RunPolicySnapshot purity
# ---------------------------------------------------------------------------


def _class_field_types(source: str, class_name: str) -> dict[str, str]:
    """Extract field type annotations from a dataclass using AST."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: dict[str, str] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields[item.target.id] = ast.unparse(item.annotation)
            return fields
    return {}


def test_run_policy_snapshot_has_no_mutable_runtime_state():
    """RunPolicySnapshot must NOT hold RunBudgetAccount, approval pending,
    manager, store, or any mutable runtime state -- only immutable per-run
    facts + typed configs."""
    source = (APPLICATION / "policy_snapshot.py").read_text()
    fields = _class_field_types(source, "RunPolicySnapshot")

    forbidden_type_fragments = (
        "RunBudgetAccount",
        "ApprovalPending",
        "Manager",
        "Store",
        "PolicyAuditService",
        "PolicyAuditSink",
        "BudgetService",
        "InformationFlowService",
        "RuntimeMemoryService",
        "ToolService",
        "SessionService",
    )

    for field_name, type_str in fields.items():
        for fragment in forbidden_type_fragments:
            assert fragment not in type_str, (
                f"RunPolicySnapshot field '{field_name}: {type_str}' "
                f"contains forbidden type fragment '{fragment}'"
            )


def test_run_policy_snapshot_is_frozen_dataclass():
    """RunPolicySnapshot must be a frozen dataclass."""
    source = (APPLICATION / "policy_snapshot.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RunPolicySnapshot":
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                if "frozen=True" in text or "frozen" in text:
                    return
            # Also check @dataclass(frozen=True) form
            # If we get here, check the source text for frozen
            assert "frozen=True" in source.split("class RunPolicySnapshot")[0].split("@dataclass")[-1], (
                "RunPolicySnapshot must be frozen=True"
            )
            return
    pytest.fail("RunPolicySnapshot class not found")


def test_run_policy_snapshot_factory_has_no_settings_reference():
    """RunPolicySnapshotFactory must NOT hold a Settings reference --
    it delegates profile resolution to the injected PolicyProfileProvider."""
    source = (APPLICATION / "policy_snapshot.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RunPolicySnapshotFactory":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    type_str = ast.unparse(item.annotation)
                    assert "Settings" not in type_str, (
                        f"RunPolicySnapshotFactory field '{item.target.id}' "
                        f"holds a Settings reference: {type_str}"
                    )
