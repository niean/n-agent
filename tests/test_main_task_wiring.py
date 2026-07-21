"""T18 + T8: main.py task subsystem wiring tests.

Covers:
  - T18 S5-S9: ApplicationServices exposes task_service/task_run_service/task_runner
    + TaskRunner.run_service bound (late-bind set_run_service called)
    + ToolService registers task tools as managed (visible in definitions;
      execution gated by permitted_managed_tools + trusted_metadata.task)
    + lifespan starts/stops TaskRunner when task_enabled=true (and skips
      otherwise)
    + Settings task fields + cross-field validation (covered in test_config.py)
  - T8: five user_task tools (create/list/approve/reject/revise) wired under
    a single UserTaskToolExecutor + single source key `user_task`; task_enabled
    gates the entire subsystem; init failure is fail-fast (not fail-soft);
    duplicate-name assertion covers all five names across routes and definitions.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.task_run_service import TaskRunService
from app.application.task_runner import TaskRunner
from app.application.task_service import TaskService
from app.application.task_tools import (
    TASK_TOOL_NAMES,
    USER_TASK_APPROVAL_TOOL_NAMES,
    USER_TASK_TOOL_NAMES,
)
from app.config import Settings
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType
from app.main import build_application_services, create_app


# 五个用户侧工具名合并集合（create/list + approve/reject/revise）
ALL_USER_TASK_TOOL_NAMES: frozenset[str] = (
    USER_TASK_TOOL_NAMES | USER_TASK_APPROVAL_TOOL_NAMES
)


def _settings(
    tmp_path: Path,
    *,
    task_enabled: bool = True,
    feishu_enabled: bool = False,
    scheduler_enabled: bool = False,
) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=scheduler_enabled,
        feishu_enabled=feishu_enabled,
        task_enabled=task_enabled,
    )


# ---------------------------------------------------------------------------
# T18 S5-S6: ApplicationServices exposes task services
# ---------------------------------------------------------------------------


def test_application_services_expose_task_fields(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert isinstance(services.task_service, TaskService)
    assert isinstance(services.task_run_service, TaskRunService)
    assert isinstance(services.task_runner, TaskRunner)


def test_task_runner_run_service_late_bound(tmp_path: Path):
    """TaskRunner.set_run_service must be called (circular dep resolution)."""
    services = build_application_services(_settings(tmp_path))
    assert services.task_runner._run_service is services.task_run_service


def test_task_service_dispatch_run_service_injected(tmp_path: Path):
    """TaskService.set_run_service must be called (delegation circular dep)."""
    services = build_application_services(_settings(tmp_path))
    assert services.task_service._run_service is services.task_run_service


def test_task_service_attachments_root_resolved(tmp_path: Path):
    """task_attachments_root relative path resolved to workspace_root."""
    services = build_application_services(_settings(tmp_path))
    expected = (tmp_path / "locals" / "task-attachments").resolve()
    assert services.task_service.attachments_root == expected


# ---------------------------------------------------------------------------
# T18 S5: ToolService registers task tools (managed, gated)
# ---------------------------------------------------------------------------


def test_task_tools_registered_as_managed(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    names = {d.name for d in services.tool_service.list_definitions()}
    assert TASK_TOOL_NAMES <= names
    # All task tools managed=True (pattern twelve gating)
    for d in services.tool_service.list_definitions():
        if d.name in TASK_TOOL_NAMES:
            assert d.managed is True, f"{d.name} managed"


def test_task_management_executor_routes_bound(tmp_path: Path):
    """All 6 task tool names routed to TaskManagementToolExecutor in CompositeToolExecutor."""
    from app.infrastructure.tools.task_management import TaskManagementToolExecutor

    services = build_application_services(_settings(tmp_path))
    routes = services.tool_service.executor.routes
    # Find a TaskManagementToolExecutor instance in routes
    task_executors = {
        routes[name] for name in TASK_TOOL_NAMES if name in routes
    }
    assert len(task_executors) == 1, "all 6 task tools must share one executor"
    assert isinstance(next(iter(task_executors)), TaskManagementToolExecutor)


def test_task_tools_unique_registration_no_duplicates(tmp_path: Path):
    """启动时发现重名即失败 -- 单一权威来源（静态注册）。"""
    services = build_application_services(_settings(tmp_path))
    names = [d.name for d in services.tool_service.list_definitions()]
    # No duplicates overall
    assert len(names) == len(set(names)), "duplicate tool names"


# ---------------------------------------------------------------------------
# T18 S7-S9: lifespan starts/stops TaskRunner
# ---------------------------------------------------------------------------


def test_lifespan_starts_task_runner_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """task_enabled=true -> lifespan start TaskRunner; on shutdown -> stop.

    The TestClient context triggers the FastAPI lifespan. If start/stop raise
    unhandled exceptions, the context would fail. TaskRunner.start/stop
    idempotency is verified separately.
    """
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "true")
    # Boots without exception; lifespan runs task_runner.start()/stop()
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_lifespan_skips_task_runner_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """task_enabled=false -> lifespan does NOT start TaskRunner."""
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "false")
    with TestClient(create_app()) as client:
        # App still boots
        assert client.get("/health").status_code in (200, 503)


def test_task_runner_start_stop_idempotent(tmp_path: Path):
    """TaskRunner.start/stop are idempotent per spec."""
    services = build_application_services(_settings(tmp_path))

    async def _run():
        await services.task_runner.start()
        await services.task_runner.start()  # idempotent
        await services.task_runner.stop()
        await services.task_runner.stop()  # idempotent

    asyncio.run(_run())
    assert services.task_runner._started is False


# ---------------------------------------------------------------------------
# T18 S7: feishu_client optional (None if not configured)
# ---------------------------------------------------------------------------


def test_task_outbound_delivery_handles_no_feishu(tmp_path: Path):
    """feishu_client=None 时 TaskOutboundDelivery 仍可装配."""
    services = build_application_services(_settings(tmp_path, feishu_enabled=False))
    # Notifier wired (may be the TaskOutboundDelivery with feishu_client=None)
    assert services.task_run_service.notifier is not None


def test_task_outbound_delivery_with_feishu(tmp_path: Path):
    """feishu_enabled 时 TaskOutboundDelivery 持有 feishu_client."""
    services = build_application_services(_settings(tmp_path, feishu_enabled=True))
    # feishu_client should be non-None on the outbound delivery
    notifier = services.task_run_service.notifier
    assert notifier is not None
    # We don't expose feishu_client directly; verify it via the delivery object
    assert hasattr(notifier, "feishu_client")
    assert notifier.feishu_client is not None


# ---------------------------------------------------------------------------
# T18 S9: schema failure -> health unhealthy + dispatcher not started
# (Verified indirectly: schema runs at construction; failure raises)
# ---------------------------------------------------------------------------


def test_task_registry_schema_initialized_at_startup(tmp_path: Path):
    """SQLiteTaskRegistry 在装配时完成 schema 初始化."""
    services = build_application_services(_settings(tmp_path))
    # Verify by listing tables
    reg = services.task_service.registry
    # SQLiteTaskRegistry has _list_tables()
    tables = reg._list_tables()
    for t in [
        "tasks", "task_runs", "task_comments",
        "task_events", "task_attachments", "task_notify_subs",
    ]:
        assert t in tables, f"missing table {t}"


# ---------------------------------------------------------------------------
# T18 S7: TaskRunner interval/shutdown_grace wired from Settings
# ---------------------------------------------------------------------------


def test_task_runner_interval_from_settings(tmp_path: Path):
    s = _settings(tmp_path)
    # Override via model_copy to confirm wiring reads settings
    s = s.model_copy(update={"task_dispatch_interval_seconds": 7})
    services = build_application_services(s)
    assert services.task_runner.interval_seconds == 7


def test_task_runner_shutdown_grace_from_settings(tmp_path: Path):
    s = _settings(tmp_path).model_copy(update={"task_shutdown_grace_seconds": 15})
    services = build_application_services(s)
    assert services.task_runner.shutdown_grace_seconds == 15


def test_task_run_service_lease_and_concurrency_from_settings(tmp_path: Path):
    s = _settings(tmp_path).model_copy(update={
        "task_lease_seconds": 600,
        "task_max_concurrency": 2,
        "task_max_runtime_seconds": 1800,
        "task_heartbeat_timeout_seconds": 120,
    })
    services = build_application_services(s)
    assert services.task_run_service.lease_seconds == 600
    assert services.task_run_service.max_concurrency == 2
    assert services.task_run_service.max_runtime_seconds == 1800
    assert services.task_run_service.heartbeat_timeout_seconds == 120


# ---------------------------------------------------------------------------
# 用户侧任务工具装配（自然语言委派 create_task / list_tasks + 审批 approve_task /
# reject_task / revise_task）
# spec: spec-260720-chat-natural-language-task.md, spec-260721-chat-nl-approval.md
# ---------------------------------------------------------------------------


def test_user_task_tools_registered_when_task_subsystem_available(tmp_path: Path):
    """task 子系统可用时，五个用户侧工具（create/list/approve/reject/revise）注册为动态定义。"""
    services = build_application_services(_settings(tmp_path))
    names = {d.name for d in services.tool_service.list_definitions()}
    assert ALL_USER_TASK_TOOL_NAMES <= names, (
        f"missing user_task tools: {ALL_USER_TASK_TOOL_NAMES - names}"
    )
    # 无重名
    all_names = [d.name for d in services.tool_service.list_definitions()]
    assert len(all_names) == len(set(all_names)), "duplicate tool names"


def test_user_task_tools_routed_to_single_user_executor(tmp_path: Path):
    """五个用户侧工具路由到同一个 UserTaskToolExecutor。"""
    from app.infrastructure.tools.user_task_management import UserTaskToolExecutor

    services = build_application_services(_settings(tmp_path))
    routes = services.tool_service.executor.routes
    user_executors = {
        routes[name] for name in ALL_USER_TASK_TOOL_NAMES if name in routes
    }
    assert len(user_executors) == 1, (
        "create_task/list_tasks/approve_task/reject_task/revise_task must share one executor"
    )
    assert isinstance(next(iter(user_executors)), UserTaskToolExecutor)


def test_user_task_tools_do_not_disrupt_worker_task_routes(tmp_path: Path):
    """用户侧工具注册后，6 个 worker managed task 工具路由保持不变。"""
    from app.infrastructure.tools.task_management import TaskManagementToolExecutor

    services = build_application_services(_settings(tmp_path))
    routes = services.tool_service.executor.routes
    worker_executors = {routes[name] for name in TASK_TOOL_NAMES if name in routes}
    assert len(worker_executors) == 1
    assert isinstance(next(iter(worker_executors)), TaskManagementToolExecutor)


def test_user_task_tools_absent_when_task_disabled(tmp_path: Path):
    """task_enabled=false 时，五个用户侧工具的 definitions 与 routes 全部不存在。"""
    services = build_application_services(_settings(tmp_path, task_enabled=False))
    names = {d.name for d in services.tool_service.list_definitions()}
    assert ALL_USER_TASK_TOOL_NAMES.isdisjoint(names), (
        f"user_task tools leaked when disabled: {ALL_USER_TASK_TOOL_NAMES & names}"
    )
    routes = services.tool_service.executor.routes
    assert ALL_USER_TASK_TOOL_NAMES.isdisjoint(set(routes.keys())), (
        "user_task routes leaked when disabled"
    )


def test_task_services_none_when_task_disabled(tmp_path: Path):
    """task_enabled=false 时，task_service/task_run_service/task_runner 均为 None。"""
    services = build_application_services(_settings(tmp_path, task_enabled=False))
    assert services.task_service is None
    assert services.task_run_service is None
    assert services.task_runner is None


def test_user_task_definitions_merged_under_single_source_key(tmp_path: Path):
    """五个用户侧工具合并到唯一 source key `user_task`，不传 override_static_names。"""
    services = build_application_services(_settings(tmp_path))
    # `user_task` source key 包含且仅包含五个用户侧工具
    user_task_defs = services.tool_service.dynamic_definitions.get("user_task", {})
    user_task_names = set(user_task_defs.keys())
    assert user_task_names == ALL_USER_TASK_TOOL_NAMES, (
        f"user_task source key mismatch: {user_task_names}"
    )
    # 不应存在 override_static_names 抑制（五工具名都不在 suppressed 集合）
    suppressed = services.tool_service._suppressed_static_names.get("user_task", set())
    assert suppressed == set(), f"unexpected suppressed static names: {suppressed}"


def test_user_task_tools_listed_in_chat_tools_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """task_enabled=true 时 /chat/tools 列出五个用户侧工具。"""
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "true")
    with TestClient(create_app()) as client:
        tools = client.get("/chat/tools").json()
        names = {t["name"] for t in tools}
        assert ALL_USER_TASK_TOOL_NAMES <= names, (
            f"missing user_task tools in /chat/tools: {ALL_USER_TASK_TOOL_NAMES - names}"
        )


def test_user_task_tools_absent_from_chat_tools_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """task_enabled=false 时 /chat/tools 不包含五个用户侧工具。"""
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "false")
    with TestClient(create_app()) as client:
        tools = client.get("/chat/tools").json()
        names = {t["name"] for t in tools}
        assert ALL_USER_TASK_TOOL_NAMES.isdisjoint(names), (
            f"user_task tools leaked in /chat/tools when disabled: "
            f"{ALL_USER_TASK_TOOL_NAMES & names}"
        )


# ---------------------------------------------------------------------------
# T8: fail-fast semantics -- task_enabled=true 且初始化异常 -> 启动失败
# (spec: 初始化异常必须让启动失败，不得伪装成"子系统不可用"静默降级)
# ---------------------------------------------------------------------------


def test_build_raises_when_registry_init_fails_while_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """task_enabled=true 且 SQLiteTaskRegistry 初始化异常时 build_application_services 抛出。

    不得伪装成 subsystem disabled 静默降级（spec 要求 fail-fast）。
    """
    import app.main as main_module

    class _BoomRegistry:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("simulated schema failure")

    monkeypatch.setattr(main_module, "SQLiteTaskRegistry", _BoomRegistry)
    with pytest.raises(RuntimeError, match="simulated schema failure"):
        build_application_services(_settings(tmp_path))


def test_build_does_not_raise_when_disabled_even_if_registry_would_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """task_enabled=false 时跳过 task 子系统初始化，SQLiteTaskRegistry 不被调用。"""
    import app.main as main_module

    class _BoomRegistry:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SQLiteTaskRegistry must not be called when task_enabled=False")

    monkeypatch.setattr(main_module, "SQLiteTaskRegistry", _BoomRegistry)
    # 不抛异常，build 正常返回
    services = build_application_services(_settings(tmp_path, task_enabled=False))
    assert services.task_service is None
    assert services.task_run_service is None
    assert services.task_runner is None


# ---------------------------------------------------------------------------
# T8: 重名断言扩展 -- 审批名称预先存在于 routes 或 definitions 时启动抛 RuntimeError
# ---------------------------------------------------------------------------


def test_startup_raises_on_approval_route_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """approve_task 预先存在于 routes 时启动抛 RuntimeError（route 冲突）。"""
    import app.main as main_module
    from app.application.task_tools import USER_TASK_TOOL_APPROVE

    # 把 approve_task 加入 BUILTIN_TOOL_NAMES 使其预先注册到 routes
    # (routes = {name: builtin_executor for name in BUILTIN_TOOL_NAMES})
    monkeypatch.setattr(
        main_module,
        "BUILTIN_TOOL_NAMES",
        main_module.BUILTIN_TOOL_NAMES | {USER_TASK_TOOL_APPROVE},
    )
    with pytest.raises(RuntimeError, match="duplicate tool name"):
        build_application_services(_settings(tmp_path))


def test_startup_raises_on_approval_definition_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """reject_task 预先存在于 ToolService definitions 时启动抛 RuntimeError（definition 冲突）。"""
    import app.main as main_module
    from app.application.task_tools import USER_TASK_TOOL_REJECT

    # 构造一个与 reject_task 同名的 BUILTIN 定义，使其进入静态 tool_definitions
    conflicting_def = ToolDefinition(
        name=USER_TASK_TOOL_REJECT,
        description="conflicting pre-existing definition",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.BUILTIN,
        toolset="system",
    )
    original = main_module.builtin_tool_definitions

    def _patched(*args, **kwargs):
        return original(*args, **kwargs) + [conflicting_def]

    monkeypatch.setattr(main_module, "builtin_tool_definitions", _patched)
    with pytest.raises(RuntimeError, match="duplicate tool name"):
        build_application_services(_settings(tmp_path))


# ---------------------------------------------------------------------------
# T4: _task_lifecycle_writer 三参合同（card 透传到 SessionService）
# ---------------------------------------------------------------------------


def test_task_lifecycle_writer_passes_card_to_session_service(tmp_path: Path):
    """main.py _task_lifecycle_writer 把第三参数 card 原样传给 SessionService."""
    from app.domain.session import ConversationSession

    services = build_application_services(_settings(tmp_path))
    session_svc = services.session_service
    writer = services.task_run_service.lifecycle_writer
    assert writer is not None

    async def _run():
        await session_svc.memory_store.create_session(
            ConversationSession(id="dashboard-s1")
        )
        card = {
            "schema_version": 1,
            "kind": "task_lifecycle",
            "task_id": "t1",
            "status": "waiting_approval",
            "title": "完成报告",
            "summary": "改用 PDF",
            "available_actions": ["approve", "reject", "revise", "cancel"],
        }
        await writer("dashboard-s1", "等待批准: t1 - 完成报告", card)
        msgs = await session_svc.memory_store.list_messages("dashboard-s1")
        assert len(msgs) == 1
        assert msgs[0].name == "ui.task_lifecycle"
        assert msgs[0].card is not None
        assert msgs[0].card["task_id"] == "t1"
        assert msgs[0].card["status"] == "waiting_approval"
        assert msgs[0].card["available_actions"] == [
            "approve", "reject", "revise", "cancel",
        ]

    asyncio.run(_run())


def test_task_lifecycle_writer_defaults_card_none(tmp_path: Path):
    """main.py _task_lifecycle_writer 默认 card=None（两参调用兼容）。"""
    from app.domain.session import ConversationSession

    services = build_application_services(_settings(tmp_path))
    session_svc = services.session_service
    writer = services.task_run_service.lifecycle_writer

    async def _run():
        await session_svc.memory_store.create_session(
            ConversationSession(id="dashboard-s1")
        )
        # 两参调用：card 默认 None
        await writer("dashboard-s1", "开始运行: t1 - 完成报告")
        msgs = await session_svc.memory_store.list_messages("dashboard-s1")
        assert len(msgs) == 1
        assert msgs[0].card is None
        assert "开始运行" in msgs[0].content

    asyncio.run(_run())


def test_task_lifecycle_writer_swallows_session_not_found(tmp_path: Path):
    """会话不存在时 SessionNotFoundError 静默跳过（不复活会话）。"""
    services = build_application_services(_settings(tmp_path))
    writer = services.task_run_service.lifecycle_writer

    async def _run():
        # 不创建会话 -> SessionNotFoundError 被吞，不抛
        await writer("nonexistent-session", "content", None)
        await writer(
            "nonexistent-session", "content",
            {"schema_version": 1, "kind": "task_lifecycle"},
        )

    asyncio.run(_run())  # 不抛即通过
