from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_normalizes_workspace_and_sqlite_path(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        agent_iteration_limit=3,
    )

    assert settings.sqlite_path == tmp_path / "sessions.db"
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.agent_iteration_limit == 3


def test_settings_has_kb_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
    )

    assert settings.agent_iteration_limit == 10
    assert settings.kb_enabled is False
    assert settings.kb_base_url == ""
    assert settings.kb_default_top_k == 5
    assert settings.kb_default_min_score == 0.5
    assert settings.kb_timeout_seconds == 10


def test_settings_has_web_fetch_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.web_fetch_enabled is True
    assert settings.web_fetch_timeout_seconds == 10
    assert settings.web_fetch_max_bytes == 131072
    assert settings.web_fetch_allow_private_urls is False


def test_settings_web_fetch_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_WEB_FETCH_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_MAX_BYTES", "4096")
    monkeypatch.setenv("N_AGENT_WEB_FETCH_ALLOW_PRIVATE_URLS", "true")

    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.web_fetch_enabled is False
    assert settings.web_fetch_timeout_seconds == 3
    assert settings.web_fetch_max_bytes == 4096
    assert settings.web_fetch_allow_private_urls is True


def test_settings_has_scheduler_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.scheduler_enabled is True
    assert settings.scheduler_tick_seconds == 30
    assert settings.scheduler_max_due_per_tick == 5
    assert settings.scheduler_missed_grace_seconds == 300
    assert settings.scheduler_lease_seconds == 900
    assert settings.scheduler_timezone == "Asia/Shanghai"


@pytest.mark.parametrize("timezone", ["Not/AZone", ""])
def test_settings_validates_scheduler_timezone(tmp_path: Path, timezone: str):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            scheduler_timezone=timezone,
            _env_file=None,
        )


@pytest.mark.parametrize("kwargs", [
    {"scheduler_tick_seconds": 0},
    {"scheduler_max_due_per_tick": 0},
    {"scheduler_missed_grace_seconds": -1},
    {"scheduler_lease_seconds": 10},
])
def test_settings_validates_scheduler_bounds(tmp_path: Path, kwargs: dict):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            **kwargs,
        )



def test_settings_has_gateway_and_feishu_defaults(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )

    assert settings.gateway_enabled is True
    assert settings.feishu_enabled is False
    assert settings.feishu_app_id == ""
    assert settings.feishu_app_secret == ""
    assert settings.feishu_tenant_key == ""
    assert settings.feishu_allowed_open_ids == []
    assert settings.feishu_allowed_chat_ids == []


def test_settings_parses_feishu_allowlists(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        feishu_allowed_open_ids="ou_1, ou_2",
        feishu_allowed_chat_ids="oc_1,oc_2",
    )

    assert settings.feishu_allowed_open_ids == ["ou_1", "ou_2"]
    assert settings.feishu_allowed_chat_ids == ["oc_1", "oc_2"]


@pytest.mark.parametrize("top_k", [0, 51])
def test_settings_validates_kb_default_top_k_range(tmp_path: Path, top_k: int):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_default_top_k=top_k,
        )


@pytest.mark.parametrize("min_score", [-0.1, 1.1])
def test_settings_validates_kb_default_min_score_range(tmp_path: Path, min_score: float):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_default_min_score=min_score,
        )


@pytest.mark.parametrize("timeout_seconds", [0, -1])
def test_settings_validates_kb_timeout_positive(tmp_path: Path, timeout_seconds: float):
    with pytest.raises(ValidationError):
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_timeout_seconds=timeout_seconds,
        )


def test_create_app_with_enabled_kb_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "default.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    from app.main import create_app

    app = create_app(
        Settings(
            provider_base_url="https://example.test/v1",
            provider_api_key="test-key",
            provider_model="test-model",
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            kb_enabled=True,
            kb_base_url="http://kb.test",
        )
    )

    assert app.title == "N-Agent"


def test_skill_subsystem_defaults(monkeypatch: pytest.MonkeyPatch):
    for key in (
        "N_AGENT_SKILLS_ROOT",
        "N_AGENT_SKILLS_INLINE_SHELL_ENABLED",
        "N_AGENT_SKILLS_INLINE_SHELL_TIMEOUT",
        "N_AGENT_SKILLS_MAX_VIEW_BYTES",
        "N_AGENT_SKILLS_MAX_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert str(s.skills_root) == "/workspace/skills"
    assert s.skills_inline_shell_enabled is False
    assert s.skills_inline_shell_timeout == 10
    assert s.skills_max_view_bytes == 131072
    assert s.skills_max_count == 200


def test_skill_subsystem_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_SKILLS_ROOT", "/tmp/skills")
    monkeypatch.setenv("N_AGENT_SKILLS_INLINE_SHELL_ENABLED", "true")
    monkeypatch.setenv("N_AGENT_SKILLS_INLINE_SHELL_TIMEOUT", "30")
    s = Settings(_env_file=None)
    assert str(s.skills_root) == "/tmp/skills"
    assert s.skills_inline_shell_enabled is True
    assert s.skills_inline_shell_timeout == 30


def test_acp_workspace_settings(tmp_path: Path):
    settings = Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="test-key",
        provider_model="test-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        acp_host_workspace_root=str(tmp_path / "host"),
        acp_container_workspace_root="/workspace",
        _env_file=None,
    )

    assert settings.acp_host_workspace_root == tmp_path / "host"
    assert settings.acp_container_workspace_root == Path("/workspace")


def test_context_compression_defaults():
    settings = Settings(_env_file=None)
    assert settings.context_compression_enabled is True
    assert settings.context_length == 32000
    assert settings.context_compression_threshold == 0.50
    assert settings.context_compression_target_ratio == 0.20
    assert settings.context_compression_tail_budget_enabled is False
    assert settings.context_compression_protect_first_n == 3
    assert settings.context_compression_protect_last_n == 10
    assert settings.context_compression_cooldown_seconds == 300


def test_context_compression_env_mapping(monkeypatch):
    monkeypatch.setenv("N_AGENT_CONTEXT_LENGTH", "64000")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_THRESHOLD", "0.6")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_TARGET_RATIO", "0.15")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_TAIL_BUDGET_ENABLED", "true")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_PROTECT_FIRST_N", "5")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_PROTECT_LAST_N", "30")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("N_AGENT_CONTEXT_COMPRESSION_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.context_length == 64000
    assert settings.context_compression_threshold == 0.6
    assert settings.context_compression_target_ratio == 0.15
    assert settings.context_compression_tail_budget_enabled is True
    assert settings.context_compression_protect_first_n == 5
    assert settings.context_compression_protect_last_n == 30
    assert settings.context_compression_cooldown_seconds == 600
    assert settings.context_compression_enabled is False


def test_context_compression_target_ratio_must_be_less_than_threshold():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            context_compression_threshold=0.3,
            context_compression_target_ratio=0.3,
        )


def test_context_compression_target_ratio_equal_threshold_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            context_compression_threshold=0.5,
            context_compression_target_ratio=0.5,
        )


def test_context_length_min_value():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_length=100)  # < 1024


def test_skill_evolution_settings_defaults(monkeypatch):
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.skills_creation_nudge_interval == 10
    assert s.skills_background_review_max_iterations == 16
    assert s.skills_background_review_timeout_seconds == 120
    assert s.skills_write_approval is False
    assert s.skills_guard_agent_created is True
    assert s.skills_backup_enabled is True
    assert s.skills_backup_keep == 10
    assert s.skills_archive_not_delete is True
    assert s.skills_background_review_enabled is True
    assert s.skills_background_review_max_concurrent == 1


def test_plugin_override_and_hook_defaults():
    s = Settings(_env_file=None)
    assert s.plugins_override_allowlist == []
    assert s.plugin_hook_timeout_seconds == 5.0


def test_plugin_override_allowlist_parses_csv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_PLUGINS_OVERRIDE_ALLOWLIST", " a, b,a,, ")
    s = Settings(_env_file=None)
    # trim + drop-empty + stable dedupe (preserve first-occurrence order)
    assert s.plugins_override_allowlist == ["a", "b"]


def test_plugin_override_allowlist_stores_exact_strings_no_glob():
    # exact match only: '*' and 'foo/*' are stored verbatim, no prefix/glob expansion
    s = Settings(
        _env_file=None,
        plugins_override_allowlist="*,foo/*,bar",
    )
    assert s.plugins_override_allowlist == ["*", "foo/*", "bar"]


def test_plugin_override_allowlist_preserves_first_occurrence_order():
    s = Settings(
        _env_file=None,
        plugins_override_allowlist="b,a,b,c,a",
    )
    assert s.plugins_override_allowlist == ["b", "a", "c"]


@pytest.mark.parametrize("timeout_seconds", [0, -1, -0.1, 60.1, 61])
def test_plugin_hook_timeout_seconds_rejects_invalid(timeout_seconds: float):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            plugin_hook_timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("timeout_seconds", [0.1, 1.0, 5.0, 30.0, 60.0])
def test_plugin_hook_timeout_seconds_accepts_boundary(timeout_seconds: float):
    s = Settings(_env_file=None, plugin_hook_timeout_seconds=timeout_seconds)
    assert s.plugin_hook_timeout_seconds == timeout_seconds


def test_plugin_hook_timeout_seconds_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_PLUGIN_HOOK_TIMEOUT_SECONDS", "12.5")
    s = Settings(_env_file=None)
    assert s.plugin_hook_timeout_seconds == 12.5


# ---------------------------------------------------------------------------
# Task subsystem (T18 S1)
# ---------------------------------------------------------------------------


def test_settings_task_defaults(tmp_path: Path):
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )
    assert s.task_enabled is True
    assert s.task_dispatch_interval_seconds == 30
    assert s.task_lease_seconds == 900
    assert s.task_heartbeat_timeout_seconds == 300
    assert s.task_max_runtime_seconds == 3600
    assert s.task_failure_limit == 3
    assert s.task_max_concurrency == 4
    assert s.task_shutdown_grace_seconds == 30
    assert s.task_planning_max_children == 20
    assert s.task_goal_max_turns == 10
    assert s.task_attachment_max_bytes == 20 * 1024 * 1024
    assert s.task_attachment_task_max_bytes == 100 * 1024 * 1024
    # attachments_root default
    assert s.task_attachments_root == Path("locals/task-attachments")


def test_settings_task_max_runtime_can_exceed_lease(tmp_path: Path):
    """lease 由 heartbeat 续租，max_runtime 可大于初始 lease，禁止加入 max_runtime < lease 的错误约束。"""
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        task_max_runtime_seconds=3600,
        task_lease_seconds=900,
        task_heartbeat_timeout_seconds=300,
        task_dispatch_interval_seconds=30,
    )
    assert s.task_max_runtime_seconds > s.task_lease_seconds


def test_settings_task_cross_field_heartbeat_lt_lease(tmp_path: Path):
    # heartbeat_timeout_seconds < lease_seconds 必须满足
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            task_heartbeat_timeout_seconds=1000,
            task_lease_seconds=900,
        )


def test_settings_task_cross_field_dispatch_lt_lease(tmp_path: Path):
    # dispatch_interval_seconds < lease_seconds 必须满足
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            task_dispatch_interval_seconds=1000,
            task_lease_seconds=900,
        )


def test_settings_task_attachment_total_ge_single(tmp_path: Path):
    # attachment_task_max_bytes >= attachment_max_bytes
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            task_attachment_max_bytes=100,
            task_attachment_task_max_bytes=50,
        )


@pytest.mark.parametrize("kwargs", [
    {"task_lease_seconds": 0},
    {"task_heartbeat_timeout_seconds": 0},
    {"task_dispatch_interval_seconds": 0},
    {"task_max_runtime_seconds": 0},
    {"task_failure_limit": 0},
    {"task_max_concurrency": 0},
    {"task_planning_max_children": 0},
    {"task_goal_max_turns": 0},
    {"task_attachment_max_bytes": 0},
    {"task_shutdown_grace_seconds": 0},
])
def test_settings_task_validates_positive_bounds(tmp_path: Path, kwargs: dict):
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            **kwargs,
        )


def test_settings_task_env_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_DISPATCH_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("N_AGENT_TASK_MAX_CONCURRENCY", "2")
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )
    assert s.task_enabled is False
    assert s.task_dispatch_interval_seconds == 5
    assert s.task_max_concurrency == 2


def test_settings_task_attachments_root_rejects_parent_traversal(tmp_path: Path):
    # 附件根路径不得包含 ".." 穿越
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            task_attachments_root="../../etc",
        )


# ---------------------------------------------------------------------------
# Browser subsystem (T10)
# ---------------------------------------------------------------------------


def test_settings_browser_defaults(tmp_path: Path):
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )
    assert s.browser_enabled is False
    assert s.browser_default_backend == "container"
    assert s.browser_container_endpoint == ""
    assert s.browser_action_timeout == 30
    assert s.browser_navigation_timeout == 30
    assert s.browser_max_observe_chars == 4000
    assert s.browser_max_observe_elements == 80
    assert s.browser_max_screenshot_bytes == 1048576
    assert s.browser_max_screenshot_pixels == 10_000_000
    assert s.browser_screenshot_ttl_seconds == 86400
    assert s.browser_per_session_screenshot_quota == 20
    assert s.browser_poll_interval_seconds == 2
    assert s.browser_global_session_limit == 4
    assert s.browser_host_bridge_url == ""
    assert s.browser_host_bridge_token_path is None
    assert s.browser_host_grant_ttl_seconds == 300
    assert s.browser_takeover_ttl_seconds == 60
    assert s.browser_trusted_dev is False


def test_settings_browser_env_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("N_AGENT_BROWSER_ENABLED", "true")
    monkeypatch.setenv("N_AGENT_BROWSER_DEFAULT_BACKEND", "host_cdp")
    monkeypatch.setenv("N_AGENT_BROWSER_ACTION_TIMEOUT", "60")
    monkeypatch.setenv("N_AGENT_BROWSER_POLL_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("N_AGENT_BROWSER_GLOBAL_SESSION_LIMIT", "8")
    monkeypatch.setenv("N_AGENT_BROWSER_TRUSTED_DEV", "true")
    monkeypatch.setenv("N_AGENT_BROWSER_HOST_BRIDGE_URL", "http://127.0.0.1:8766")
    token_path = tmp_path / "browser_token"
    token_path.write_bytes(b"a" * 32 + b"\n")
    token_path.chmod(0o600)
    monkeypatch.setenv("N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH", str(token_path))
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
    )
    assert s.browser_enabled is True
    assert s.browser_default_backend == "host_cdp"
    assert s.browser_action_timeout == 60
    assert s.browser_poll_interval_seconds == 5
    assert s.browser_global_session_limit == 8
    assert s.browser_trusted_dev is True
    assert s.browser_host_bridge_url == "http://127.0.0.1:8766"
    assert s.browser_host_bridge_token_path == token_path


@pytest.mark.parametrize("backend", ["host_cdp", "container"])
def test_settings_browser_default_backend_accepts_valid(tmp_path: Path, backend: str):
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        browser_default_backend=backend,
    )
    assert s.browser_default_backend == backend


@pytest.mark.parametrize("backend", ["local", "docker", "", "invalid"])
def test_settings_browser_default_backend_rejects_invalid(tmp_path: Path, backend: str):
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_default_backend=backend,
        )


@pytest.mark.parametrize("poll", [0, 6, 10, -1])
def test_settings_browser_poll_interval_rejects_out_of_bounds(tmp_path: Path, poll: int):
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_poll_interval_seconds=poll,
        )


@pytest.mark.parametrize("poll", [1, 2, 3, 4, 5])
def test_settings_browser_poll_interval_accepts_bounds(tmp_path: Path, poll: int):
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        browser_poll_interval_seconds=poll,
    )
    assert s.browser_poll_interval_seconds == poll


@pytest.mark.parametrize("kwargs", [
    {"browser_action_timeout": 0},
    {"browser_navigation_timeout": 0},
    {"browser_max_observe_chars": 0},
    {"browser_max_observe_elements": 0},
    {"browser_max_screenshot_bytes": 100},
    {"browser_max_screenshot_pixels": 0},
    {"browser_screenshot_ttl_seconds": 0},
    {"browser_per_session_screenshot_quota": 0},
    {"browser_global_session_limit": 0},
    {"browser_host_grant_ttl_seconds": 0},
    {"browser_takeover_ttl_seconds": 0},
])
def test_settings_browser_validates_positive_bounds(tmp_path: Path, kwargs: dict):
    with pytest.raises(ValidationError):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            **kwargs,
        )


def test_settings_browser_host_bridge_token_path_expands(tmp_path: Path):
    token_path = tmp_path / "token"
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        browser_host_bridge_token_path=str(token_path),
    )
    assert s.browser_host_bridge_token_path == token_path


# ---------------------------------------------------------------------------
# T13: Browser cross-field validation
# ---------------------------------------------------------------------------


def test_browser_host_cdp_requires_bridge_url(tmp_path: Path):
    """When browser_enabled + default_backend=host_cdp, bridge URL is required."""
    token_path = tmp_path / "token"
    token_path.write_bytes(b"a" * 32 + b"\n")
    token_path.chmod(0o600)
    with pytest.raises(ValidationError, match="browser_host_bridge_url is required"):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_enabled=True,
            browser_default_backend="host_cdp",
            browser_host_bridge_token_path=str(token_path),
            browser_trusted_dev=True,
        )


def test_browser_host_cdp_requires_token_path(tmp_path: Path):
    """When browser_enabled + default_backend=host_cdp, token path is required."""
    with pytest.raises(ValidationError, match="browser_host_bridge_token_path is required"):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_enabled=True,
            browser_default_backend="host_cdp",
            browser_host_bridge_url="http://127.0.0.1:8766",
            browser_trusted_dev=True,
        )


def test_browser_host_cdp_requires_trusted_dev(tmp_path: Path):
    """When browser_enabled + default_backend=host_cdp, trusted_dev must be True."""
    token_path = tmp_path / "token"
    token_path.write_bytes(b"a" * 32 + b"\n")
    token_path.chmod(0o600)
    with pytest.raises(ValidationError, match="browser_trusted_dev must be True"):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_enabled=True,
            browser_default_backend="host_cdp",
            browser_host_bridge_url="http://127.0.0.1:8766",
            browser_host_bridge_token_path=str(token_path),
            browser_trusted_dev=False,
        )


def test_browser_container_requires_endpoint(tmp_path: Path):
    """When browser_enabled + default_backend=container, container endpoint is required."""
    with pytest.raises(ValidationError, match="browser_container_endpoint is required"):
        Settings(
            sqlite_path=str(tmp_path / "sessions.db"),
            workspace_root=str(tmp_path),
            _env_file=None,
            browser_enabled=True,
            browser_default_backend="container",
            browser_container_endpoint="",
        )


def test_browser_host_cdp_valid_when_all_config_present(tmp_path: Path):
    """When browser_enabled + default_backend=host_cdp and all required config
    is present, Settings construction succeeds."""
    token_path = tmp_path / "token"
    token_path.write_bytes(b"a" * 32 + b"\n")
    token_path.chmod(0o600)
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        browser_enabled=True,
        browser_default_backend="host_cdp",
        browser_host_bridge_url="http://127.0.0.1:8766",
        browser_host_bridge_token_path=str(token_path),
        browser_trusted_dev=True,
    )
    assert s.browser_default_backend == "host_cdp"
    assert s.browser_host_bridge_url == "http://127.0.0.1:8766"


def test_browser_validation_skipped_when_disabled(tmp_path: Path):
    """When browser_enabled=False, no cross-field validation is applied
    (allows constructing Settings without backend config)."""
    s = Settings(
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        _env_file=None,
        browser_enabled=False,
        browser_default_backend="container",
        browser_container_endpoint="",
    )
    assert s.browser_enabled is False
    assert s.browser_container_endpoint == ""
