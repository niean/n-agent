"""ConfigBundleService -- export/import of the 10 configuration sections.

Shared in-file fakes (T3 + T4). Every fake implements only the methods listed
in the plan's port fact sheet, with signatures copied verbatim from the real
Domain ports, so a signature drift in app/domain breaks these tests instead of
silently passing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.application.config_bundle_service import ConfigBundleService
from app.application.schedule_service import ScheduleService
from app.application.task_config_service import TaskConfigService
from app.config import Settings
from app.domain.config_bundle import (
    BUNDLE_SCHEMA_VERSION,
    SECTION_NAMES,
    ConfigBundleValidationError,
    ImportMode,
    ImportOutcome,
)
from app.domain.external_memory_provider import (
    DuplicateExternalMemoryProviderError,
    ExternalMemoryProviderConfig,
    ExternalMemoryProviderNotFoundError,
    ExternalMemoryProviderSecret,
    ExternalMemoryProviderType,
    ExternalMemoryProviderValidationError,
)
from app.domain.gateway import GatewayHomeTarget
from app.domain.knowledge import (
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseType,
    KnowledgeProbeStatus,
)
from app.domain.mcp import (
    McpProbeStatus,
    McpSite,
    McpSiteValidationError,
    McpTransportType,
)
from app.domain.platform import Platform
from app.domain.plugin import Plugin, PluginKind, PluginNotFoundError, PluginSource
from app.domain.provider import (
    DuplicateProviderError,
    ProviderConfig,
    ProviderNotFoundError,
)
from app.domain.schedule import (
    DeliveryTarget,
    ScheduleExpression,
    ScheduleTimezone,
    ScheduledExecutionPolicy,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskExecutionStatus,
    ScheduledTaskStatus,
    PromptSafetyResult,
)
from app.domain.skill import Skill, SkillFrontmatter, SkillNotFoundError, SkillReadiness, SkillSource
from app.domain.task_config import (
    StoredTaskConfig,
    TaskConfigConflictError,
    TaskConfigOverrides,
)

SEED_SECRET = "sk-live"

_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
class _FakeProviderRegistry:
    """In-memory ProviderRegistry. Keeps the idx_providers_active invariant:
    set_active clears is_active on every other row."""

    def __init__(self) -> None:
        self.rows: dict[str, ProviderConfig] = {}
        self.secrets: dict[str, str] = {}

    def seed(self, config: ProviderConfig, api_key: str = "") -> ProviderConfig:
        self.rows[config.id] = config
        if api_key:
            self.secrets[config.id] = api_key
        return config

    async def list_providers(self) -> list[ProviderConfig]:
        return list(self.rows.values())

    async def get_provider(self, provider_id: str) -> ProviderConfig | None:
        return self.rows.get(provider_id)

    async def create_provider(self, config: ProviderConfig, api_key: str) -> ProviderConfig:
        if any(row.name == config.name for row in self.rows.values()):
            raise DuplicateProviderError(config.name)
        if config.is_active and await self.get_active() is not None:
            # Mirrors the real idx_providers_active unique index.
            raise DuplicateProviderError("another provider is already active")
        stored = replace(config, api_key_present=bool(api_key))
        self.rows[config.id] = stored
        if api_key:
            self.secrets[config.id] = api_key
        return stored

    async def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        supports_vision: bool | None = None,
    ) -> ProviderConfig:
        existing = self.rows.get(provider_id)
        if existing is None:
            raise ProviderNotFoundError(provider_id)
        if clear_api_key:
            self.secrets.pop(provider_id, None)
        elif api_key is not None:
            if api_key:
                self.secrets[provider_id] = api_key
            else:
                self.secrets.pop(provider_id, None)
        changes: dict[str, Any] = {}
        for field_name, value in (
            ("name", name),
            ("base_url", base_url),
            ("model", model),
            ("provider_type", provider_type),
            ("extra_headers", extra_headers),
            ("supports_vision", supports_vision),
        ):
            if value is not None:
                changes[field_name] = value
        changes["api_key_present"] = provider_id in self.secrets
        updated = replace(existing, **changes)
        self.rows[provider_id] = updated
        return updated

    async def delete_provider(self, provider_id: str) -> None:
        self.rows.pop(provider_id, None)
        self.secrets.pop(provider_id, None)

    async def set_active(self, provider_id: str) -> ProviderConfig:
        if provider_id not in self.rows:
            raise ProviderNotFoundError(provider_id)
        for row_id, row in list(self.rows.items()):
            self.rows[row_id] = replace(row, is_active=row_id == provider_id)
        return self.rows[provider_id]

    async def get_active(self) -> ProviderConfig | None:
        for row in self.rows.values():
            if row.is_active:
                return row
        return None

    async def get_secret(self, provider_id: str) -> str | None:
        return self.secrets.get(provider_id)


# --------------------------------------------------------------------------
# knowledge_bases
# --------------------------------------------------------------------------
class _FakeKnowledgeRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, KnowledgeBase] = {}
        self.secrets: dict[str, str] = {}

    def seed(self, base: KnowledgeBase, api_key: str = "") -> KnowledgeBase:
        self.rows[base.id] = base
        if api_key:
            self.secrets[base.id] = api_key
        return base

    async def list_bases(self) -> list[KnowledgeBase]:
        return list(self.rows.values())

    async def get_base(self, kb_id: str) -> KnowledgeBase | None:
        return self.rows.get(kb_id)

    async def create_base(self, base: KnowledgeBase, api_key: str | None = None) -> KnowledgeBase:
        stored = replace(base, api_key_present=bool(api_key))
        self.rows[base.id] = stored
        if api_key:
            self.secrets[base.id] = api_key
        return stored

    async def update_base(
        self,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        base_type: KnowledgeBaseType | None = None,
        base_url: str | None = None,
        dataset_id: str | None = None,
        enabled: bool | None = None,
        default_top_k: int | None = None,
        default_min_score: float | None = None,
        clear_default_top_k: bool = False,
        clear_default_min_score: bool = False,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> KnowledgeBase:
        existing = self.rows.get(kb_id)
        if existing is None:
            raise KnowledgeBaseNotFoundError(kb_id)
        if clear_api_key:
            self.secrets.pop(kb_id, None)
        elif api_key is not None:
            if api_key:
                self.secrets[kb_id] = api_key
            else:
                self.secrets.pop(kb_id, None)
        changes: dict[str, Any] = {}
        for field_name, value in (
            ("name", name),
            ("description", description),
            ("base_type", base_type),
            ("base_url", base_url),
            ("dataset_id", dataset_id),
            ("enabled", enabled),
            ("default_top_k", default_top_k),
            ("default_min_score", default_min_score),
        ):
            if value is not None:
                changes[field_name] = value
        if clear_default_top_k:
            changes["default_top_k"] = None
        if clear_default_min_score:
            changes["default_min_score"] = None
        changes["api_key_present"] = kb_id in self.secrets
        updated = replace(existing, **changes)
        self.rows[kb_id] = updated
        return updated

    async def get_secret(self, kb_id: str) -> str | None:
        return self.secrets.get(kb_id)


# --------------------------------------------------------------------------
# mcp_sites
# --------------------------------------------------------------------------
class _FakeMcpRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, McpSite] = {}
        self.fail_on_name: str | None = None

    def seed(self, site: McpSite) -> McpSite:
        self.rows[site.id] = site
        return site

    async def list_sites(self) -> list[McpSite]:
        return list(self.rows.values())

    async def get_site_by_name(self, name: str) -> McpSite | None:
        for site in self.rows.values():
            if site.name == name:
                return site
        return None

    async def create_site(self, site: McpSite) -> McpSite:
        self._guard(site.name)
        if any(row.name == site.name for row in self.rows.values()):
            raise McpSiteValidationError(f"duplicate site name {site.name}")
        self.rows[site.id] = site
        return site

    async def update_site(self, site: McpSite) -> McpSite:
        self._guard(site.name)
        if site.id not in self.rows:
            raise McpSiteValidationError(f"unknown site {site.id}")
        self.rows[site.id] = site
        return site

    def _guard(self, name: str) -> None:
        if self.fail_on_name is not None and name == self.fail_on_name:
            raise McpSiteValidationError("simulated registry failure")


# --------------------------------------------------------------------------
# external_memory_providers (synchronous port)
# --------------------------------------------------------------------------
class _FakeExternalProviderRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, ExternalMemoryProviderConfig] = {}
        self.secrets: dict[str, str] = {}

    def seed(self, config: ExternalMemoryProviderConfig, api_key: str = "") -> ExternalMemoryProviderConfig:
        self.rows[config.id] = config
        if api_key:
            self.secrets[config.id] = api_key
        return config

    def list_providers(self) -> list[ExternalMemoryProviderConfig]:
        return list(self.rows.values())

    def get_provider(self, id: str) -> ExternalMemoryProviderConfig | None:
        return self.rows.get(id)

    def create_provider(
        self,
        *,
        id: str,
        name: str,
        provider_type: ExternalMemoryProviderType,
        base_url: str,
        api_key: str | None,
        enabled: bool,
        extra_config: dict[str, Any],
    ) -> ExternalMemoryProviderConfig:
        if any(row.name == name for row in self.rows.values()):
            raise DuplicateExternalMemoryProviderError(name)
        if enabled:
            self._assert_no_other_enabled(id)
        config = ExternalMemoryProviderConfig(
            id=id,
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            api_key_present=bool(api_key),
            enabled=enabled,
            extra_config=dict(extra_config or {}),
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
        )
        self.rows[id] = config
        if api_key:
            self.secrets[id] = api_key
        return config

    def update_provider(
        self,
        id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
        enabled: bool | None = None,
        extra_config: dict[str, Any] | None = None,
        provider_type: ExternalMemoryProviderType | None = None,
    ) -> ExternalMemoryProviderConfig:
        existing = self.rows.get(id)
        if existing is None:
            raise ExternalMemoryProviderNotFoundError(id)
        if enabled:
            self._assert_no_other_enabled(id)
        if clear_api_key or api_key == "":
            self.secrets.pop(id, None)
        elif api_key is not None:
            self.secrets[id] = api_key
        changes: dict[str, Any] = {}
        for field_name, value in (
            ("name", name),
            ("base_url", base_url),
            ("enabled", enabled),
            ("extra_config", extra_config),
            ("provider_type", provider_type),
        ):
            if value is not None:
                changes[field_name] = value
        changes["api_key_present"] = id in self.secrets
        updated = replace(existing, **changes)
        self.rows[id] = updated
        return updated

    def _assert_no_other_enabled(self, exclude_id: str) -> None:
        for row_id, row in self.rows.items():
            if row_id != exclude_id and row.enabled:
                raise ExternalMemoryProviderValidationError(
                    "at most one external-query provider can be enabled"
                )

    def get_secret(self, id: str) -> ExternalMemoryProviderSecret | None:
        if id not in self.rows:
            return None
        return ExternalMemoryProviderSecret(id=id, api_key=self.secrets.get(id))


# --------------------------------------------------------------------------
# external_memory_global_config (synchronous port)
# --------------------------------------------------------------------------
class _FakeExternalMemoryConfig:
    def __init__(self) -> None:
        self._enabled: set[str] | None = None
        self.set_enabled_calls: list[list[str]] = []

    def get_enabled(self) -> set[str] | None:
        return self._enabled

    def set_enabled(self, provider_names: list[str]) -> None:
        self.set_enabled_calls.append(list(provider_names))
        self._enabled = set(provider_names)


# --------------------------------------------------------------------------
# plugins
# --------------------------------------------------------------------------
class _FakePluginRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, Plugin] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        self.update_config_calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def seed(self, plugin: Plugin, secrets: dict[str, str] | None = None) -> Plugin:
        self.rows[plugin.key] = plugin
        self.secrets[plugin.key] = dict(secrets or {})
        return plugin

    async def list_plugins(self, include_disabled: bool = True) -> list[Plugin]:
        return list(self.rows.values())

    async def get_plugin(self, key: str) -> Plugin | None:
        return self.rows.get(key)

    async def set_enabled(self, key: str, enabled: bool) -> Plugin:
        existing = self.rows.get(key)
        if existing is None:
            raise PluginNotFoundError(key)
        updated = replace(existing, enabled=enabled)
        self.rows[key] = updated
        return updated

    async def update_config(
        self, key: str, config: dict[str, Any], secret_updates: dict[str, str] | None = None
    ) -> Plugin:
        existing = self.rows.get(key)
        if existing is None:
            raise PluginNotFoundError(key)
        self.update_config_calls.append((key, dict(config), dict(secret_updates) if secret_updates is not None else None))
        updated = replace(existing, config=dict(config))
        self.rows[key] = updated
        if secret_updates:
            # Mirrors the SQLite adapter: an explicit "" is upserted as an empty value.
            self.secrets.setdefault(key, {}).update(secret_updates)
        return updated

    async def get_secret_config(self, key: str) -> dict[str, str]:
        return dict(self.secrets.get(key, {}))


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------
class _FakeSkillRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, Skill] = {}
        self.upsert_calls: list[Skill] = []

    def seed(self, skill: Skill) -> Skill:
        self.rows[skill.name] = skill
        return skill

    async def list_skills(self, include_disabled: bool = True) -> list[Skill]:
        return list(self.rows.values())

    async def get_skill(self, name: str) -> Skill | None:
        return self.rows.get(name)

    async def set_enabled(self, name: str, enabled: bool) -> Skill:
        existing = self.rows.get(name)
        if existing is None:
            raise SkillNotFoundError(name)
        updated = replace(existing, enabled=enabled)
        self.rows[name] = updated
        return updated

    async def set_chat_selectable(self, name: str, value: bool) -> Skill:
        existing = self.rows.get(name)
        if existing is None:
            raise SkillNotFoundError(name)
        updated = replace(existing, chat_selectable=value)
        self.rows[name] = updated
        return updated

    async def upsert_skill(self, skill: Skill) -> Skill:
        self.upsert_calls.append(skill)
        return skill


# --------------------------------------------------------------------------
# scheduled_tasks: real ScheduleService over fake ports
# --------------------------------------------------------------------------
class _FakeScheduleRegistry:
    """Mirrors SQLiteScheduleRegistry.list(), which filters out DELETED rows."""

    def __init__(self) -> None:
        self.rows: dict[str, ScheduledTask] = {}

    def seed(self, task: ScheduledTask) -> ScheduledTask:
        self.rows[task.id] = task
        return task

    async def create(self, task: ScheduledTask) -> ScheduledTask:
        self.rows[task.id] = task
        return task

    async def list(self) -> list[ScheduledTask]:
        return [t for t in self.rows.values() if t.status is not ScheduledTaskStatus.DELETED]

    async def get(self, task_id: str) -> ScheduledTask | None:
        task = self.rows.get(task_id)
        if task is None or task.status is ScheduledTaskStatus.DELETED:
            return None
        return task

    async def update(self, task: ScheduledTask) -> ScheduledTask:
        self.rows[task.id] = task
        return task

    async def update_status(
        self, task_id: str, status: ScheduledTaskStatus, enabled: bool
    ) -> ScheduledTask:
        task = self.rows[task_id]
        updated = ScheduledTask(**{**task.__dict__, "status": status, "enabled": enabled})
        self.rows[task_id] = updated
        return updated


class _FakeCalculator:
    def validate(self, expression: ScheduleExpression, timezone_value: ScheduleTimezone) -> None:
        if len(expression.value.split()) != 5:
            raise ValueError(f"invalid cron expression: {expression.value}")

    def next_after(
        self,
        expression: ScheduleExpression,
        base_time: datetime,
        timezone_value: ScheduleTimezone,
    ) -> datetime:
        return base_time + timedelta(hours=1)


class _FakeScanner:
    def scan(self, prompt: str) -> PromptSafetyResult:
        return PromptSafetyResult(allowed=True)


class _FakeSessionService:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def create_session(self, session_id: str, source: str = "dashboard") -> str:
        self.created.append(session_id)
        return session_id


# --------------------------------------------------------------------------
# gateway_home_targets
# --------------------------------------------------------------------------
class _FakeGatewayRegistry:
    def __init__(self) -> None:
        self.targets: dict[Platform, GatewayHomeTarget] = {}

    def seed(self, target: GatewayHomeTarget) -> GatewayHomeTarget:
        self.targets[target.platform] = target
        return target

    async def get_home_target(self, platform: Platform) -> GatewayHomeTarget | None:
        return self.targets.get(platform)

    async def set_home_target(self, target: GatewayHomeTarget) -> GatewayHomeTarget:
        self.targets[target.platform] = target
        return target


# --------------------------------------------------------------------------
# task_config
# --------------------------------------------------------------------------
class _FakeTaskConfigStore:
    def __init__(self) -> None:
        self.row: StoredTaskConfig | None = None
        self.raise_conflict_once = False

    async def get(self) -> StoredTaskConfig | None:
        return self.row

    async def save(
        self, overrides: TaskConfigOverrides, expected_version: int, updated_by: str
    ) -> StoredTaskConfig:
        if self.raise_conflict_once:
            self.raise_conflict_once = False
            raise TaskConfigConflictError("simulated concurrent write")
        current = self.row.version if self.row is not None else 0
        if expected_version != current:
            raise TaskConfigConflictError(
                f"expected version {expected_version}, store is at {current}"
            )
        self.row = StoredTaskConfig(
            overrides=overrides,
            version=current + 1,
            updated_at=_NOW.isoformat(),
            updated_by=updated_by,
        )
        return self.row


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
        # Isolated env base for the task_config section: the defaults
        # (heartbeat=300, dispatch=30) would reject any imported lease below
        # 300s, which has nothing to do with what these tests exercise.
        task_heartbeat_timeout_seconds=60,
        task_dispatch_interval_seconds=10,
    )


def _build_registries(tmp_path: Path) -> SimpleNamespace:
    """Empty registries. Field names match ConfigBundleService.__init__."""
    schedule_registry = _FakeScheduleRegistry()
    task_config_store = _FakeTaskConfigStore()
    return SimpleNamespace(
        provider_registry=_FakeProviderRegistry(),
        knowledge_registry=_FakeKnowledgeRegistry(),
        mcp_registry=_FakeMcpRegistry(),
        external_memory_provider_registry=_FakeExternalProviderRegistry(),
        external_memory_config=_FakeExternalMemoryConfig(),
        plugin_registry=_FakePluginRegistry(),
        skill_registry=_FakeSkillRegistry(),
        schedule_service=ScheduleService(
            registry=schedule_registry,
            calculator=_FakeCalculator(),
            scanner=_FakeScanner(),
            session_service=_FakeSessionService(),
        ),
        gateway_registry=_FakeGatewayRegistry(),
        task_config_store=task_config_store,
        task_config_service=TaskConfigService(_settings(tmp_path), task_config_store),
    )


def _skill(name: str, *, enabled: bool, chat_selectable: bool) -> Skill:
    return Skill(
        id=f"skl-{name}",
        name=name,
        relative_path=f"{name}/SKILL.md",
        description="",
        platforms=[],
        frontmatter=SkillFrontmatter(
            name=name,
            description="",
            version="1.0.0",
            platforms=[],
            tags=[],
            related_skills=[],
            author="",
            license="",
            setup_help=None,
            required_env_vars=[],
            raw={},
        ),
        enabled=enabled,
        readiness=SkillReadiness.AVAILABLE,
        last_scan_status="ok",
        last_scan_error=None,
        last_seen_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        source=SkillSource.USER,
        chat_selectable=chat_selectable,
    )


def _scheduled_task(
    name: str, *, status: ScheduledTaskStatus, enabled: bool
) -> ScheduledTask:
    return ScheduledTask(
        id=f"sched-{name}",
        name=name,
        prompt="每天汇报",
        schedule=ScheduleExpression("0 9 * * *"),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id=f"schedule-{name}",
        delivery_target=DeliveryTarget.origin(
            {"receive_id": "ou_x", "receive_id_type": "open_id"}
        ),
        next_run_at=_NOW + timedelta(hours=9),
        enabled=enabled,
        status=status,
        origin={"platform": "feishu"},
        execution_policy=ScheduledExecutionPolicy(
            mode=ScheduledExecutionPolicyMode.UNATTENDED,
            tool_exposure_policy="safe_only",
            allow_confirm_tools=False,
            allowed_tools=("web_search",),
        ),
        lease_until=None,
        lease_owner=None,
        claim_id=None,
        last_run_at=_NOW,
        last_status=None,
        unread_count=2,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed(registries: SimpleNamespace) -> None:
    registries.provider_registry.seed(
        ProviderConfig(
            id="prv-1",
            name="ark",
            provider_type="openai",
            base_url="https://ark.example.com/v1",
            model="ark-code-latest",
            api_key_present=True,
            is_active=True,
            extra_headers={"X-Auth": SEED_SECRET},
            created_at=_NOW,
            updated_at=_NOW,
            supports_vision=True,
        ),
        api_key=SEED_SECRET,
    )
    registries.knowledge_registry.seed(
        KnowledgeBase(
            id="team-kb",
            name="团队知识库",
            description="内部资料",
            base_type=KnowledgeBaseType.N_KB,
            base_url="https://kb.example.com",
            dataset_id="ds-1",
            api_key_present=True,
            enabled=True,
            default_top_k=5,
            default_min_score=0.3,
            last_probe_status=KnowledgeProbeStatus.SUCCESS,
            last_probe_error=None,
            last_probed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        api_key=SEED_SECRET,
    )
    registries.mcp_registry.seed(
        McpSite(
            name="local-fs",
            url="",
            id="mcp-1",
            transport_type=McpTransportType.STDIO,
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"],
            env={"FS_TOKEN": SEED_SECRET},
            enabled=True,
            last_probe_status=McpProbeStatus.SUCCESS,
            last_probe_error=None,
            last_probed_at=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    registries.external_memory_provider_registry.seed(
        ExternalMemoryProviderConfig(
            id="emp-1",
            name="mem0-main",
            provider_type=ExternalMemoryProviderType.MEM0,
            base_url="https://mem0.example.com",
            api_key_present=True,
            enabled=True,
            extra_config={
                "org": "acme",
                "nested": [{"webhook_secret": SEED_SECRET}],
            },
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
        ),
        api_key=SEED_SECRET,
    )
    registries.external_memory_config.set_enabled(["builtin", "mem0-main"])
    registries.external_memory_config.set_enabled_calls.clear()
    registries.plugin_registry.seed(
        Plugin(
            id="plg-1",
            key="opa-client",
            name="OPA Client",
            source=PluginSource.USER,
            enabled=True,
            version="1.0.0",
            description="",
            author="",
            kind=PluginKind.STANDALONE,
            source_path="/Users/someone/plugins/opa-client",
            config={
                "endpoint": "https://opa.example.com",
                "credentials": {"api_token": SEED_SECRET},
            },
            secret_refs={"token": "ref"},
            capabilities={},
            manifest={"config_schema": {"type": "object", "required": ["endpoint"]}},
            last_scan_status="ok",
            last_scan_error=None,
            last_scanned_at=_NOW,
        ),
        secrets={"token": SEED_SECRET},
    )
    registries.skill_registry.seed(_skill("web-research", enabled=True, chat_selectable=True))
    registries.skill_registry.seed(_skill("legacy-skill", enabled=False, chat_selectable=False))
    registries.schedule_service.registry.seed(
        _scheduled_task("每日简报", status=ScheduledTaskStatus.ACTIVE, enabled=True)
    )
    registries.schedule_service.registry.seed(
        _scheduled_task("已删除任务", status=ScheduledTaskStatus.DELETED, enabled=False)
    )
    registries.gateway_registry.seed(
        GatewayHomeTarget(
            platform=Platform.FEISHU,
            receive_id="oc_home",
            receive_id_type="chat_id",
            thread_id="",
            display_name="主群",
            updated_at=_NOW,
        )
    )
    registries.task_config_store.row = StoredTaskConfig(
        overrides=TaskConfigOverrides(task_lease_seconds=1200),
        version=1,
        updated_at=_NOW.isoformat(),
        updated_by="human",
    )


@pytest.fixture
def registries(tmp_path: Path) -> SimpleNamespace:
    """Seeded registries. Field names match ConfigBundleService.__init__."""
    built = _build_registries(tmp_path)
    _seed(built)
    return built


@pytest.fixture
def svc(registries: SimpleNamespace) -> ConfigBundleService:
    return ConfigBundleService(**vars(registries))


@pytest.fixture
def svc_without_rows(tmp_path: Path) -> ConfigBundleService:
    return ConfigBundleService(**vars(_build_registries(tmp_path / "empty")))


@pytest.fixture
async def bundle(svc: ConfigBundleService):
    return await svc.export_bundle(redact_secrets=False)


class _ImportTarget:
    """An empty import target: a fresh set of registries plus the service on
    top of them, with the seeding helpers the T4 cases need.

    registries.task_config_store is the very instance wrapped by
    registries.task_config_service, so a test can read the stored row directly.
    """

    def __init__(self, registries: SimpleNamespace) -> None:
        self.registries = registries
        self.svc = ConfigBundleService(**vars(registries))

    async def seed_active_provider(self, name: str) -> ProviderConfig:
        return self.registries.provider_registry.seed(
            ProviderConfig(
                id=f"prv-{name}",
                name=name,
                provider_type="openai",
                base_url="https://target.example.com/v1",
                model="target-model",
                api_key_present=False,
                is_active=True,
                extra_headers=None,
                created_at=_NOW,
                updated_at=_NOW,
                supports_vision=False,
            )
        )

    def seed_external_provider(self, name: str, enabled: bool) -> ExternalMemoryProviderConfig:
        # Synchronous Domain port -- deliberately not a coroutine.
        return self.registries.external_memory_provider_registry.seed(
            ExternalMemoryProviderConfig(
                id=f"emp-{name}",
                name=name,
                provider_type=ExternalMemoryProviderType.MEM0,
                base_url="https://target-memory.example.com",
                api_key_present=False,
                enabled=enabled,
                extra_config={},
                created_at=_NOW.isoformat(),
                updated_at=_NOW.isoformat(),
            )
        )

    async def seed_skill(self, name: str, enabled: bool, chat_selectable: bool) -> Skill:
        return self.registries.skill_registry.seed(
            _skill(name, enabled=enabled, chat_selectable=chat_selectable)
        )

    async def seed_plugin(self, key: str, version: str) -> Plugin:
        return self.registries.plugin_registry.seed(
            Plugin(
                id=f"plg-{key}",
                key=key,
                name="OPA Client",
                source=PluginSource.USER,
                enabled=True,
                version=version,
                description="",
                author="",
                kind=PluginKind.STANDALONE,
                source_path=f"/opt/plugins/{key}",
                config={},
                secret_refs={},
                capabilities={},
                manifest={},
                last_scan_status="ok",
                last_scan_error=None,
                last_scanned_at=_NOW,
            )
        )

    async def mark_executed(self, task_id: str, last_status: str, unread_count: int) -> ScheduledTask:
        registry = self.registries.schedule_service.registry
        task = registry.rows[task_id]
        updated = ScheduledTask(
            **{
                **task.__dict__,
                "last_status": ScheduledTaskExecutionStatus(last_status),
                "last_run_at": _NOW,
                "unread_count": unread_count,
            }
        )
        registry.rows[task_id] = updated
        return updated

    async def hold_lease(self, task_id: str, seconds: int) -> ScheduledTask:
        registry = self.registries.schedule_service.registry
        task = registry.rows[task_id]
        updated = ScheduledTask(
            **{
                **task.__dict__,
                "lease_until": datetime.now(timezone.utc) + timedelta(seconds=seconds),
                "lease_owner": "runner-1",
                "claim_id": "claim-1",
            }
        )
        registry.rows[task_id] = updated
        return updated


@pytest.fixture
def target(tmp_path: Path) -> _ImportTarget:
    return _ImportTarget(_build_registries(tmp_path / "target"))


# --------------------------------------------------------------------------
# T3: export
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_covers_all_ten_sections(svc):
    bundle = await svc.export_bundle(redact_secrets=False)
    assert bundle.schema_version == BUNDLE_SCHEMA_VERSION
    assert set(bundle.sections) == set(SECTION_NAMES)


@pytest.mark.asyncio
async def test_export_carries_plaintext_secret_when_not_redacted(svc):
    bundle = await svc.export_bundle(redact_secrets=False)
    assert bundle.sections["providers"][0]["api_key"] == "sk-live"


@pytest.mark.asyncio
async def test_export_redacts_every_secret_carrier(svc):
    bundle = await svc.export_bundle(redact_secrets=True)
    assert bundle.redacted is True
    assert bundle.sections["providers"][0]["api_key"] is None
    assert bundle.sections["providers"][0]["extra_headers"] is None
    assert bundle.sections["knowledge_bases"][0]["api_key"] is None
    assert bundle.sections["external_memory_providers"][0]["api_key"] is None
    assert bundle.sections["mcp_sites"][0]["env"] is None
    assert bundle.sections["plugins"][0]["plugin_secrets"] is None
    assert "sk-live" not in json.dumps(bundle.sections)


@pytest.mark.asyncio
async def test_export_redacts_nested_secrets_in_plugin_config_and_extra_config(svc):
    bundle = await svc.export_bundle(redact_secrets=True)
    plugin = bundle.sections["plugins"][0]
    assert plugin["config_json"]["credentials"]["api_token"] is None
    assert plugin["config_json"]["endpoint"] == "https://opa.example.com"
    provider = bundle.sections["external_memory_providers"][0]
    assert provider["extra_config"]["nested"][0]["webhook_secret"] is None
    assert provider["extra_config"]["org"] == "acme"


@pytest.mark.asyncio
async def test_export_strips_runtime_and_machine_fields(svc):
    task = (await svc.export_bundle(redact_secrets=False)).sections["scheduled_tasks"][0]
    for stripped in ("id", "session_id", "next_run_at", "lease_until", "claim_id",
                     "last_run_at", "last_status", "unread_count"):
        assert stripped not in task
    site = (await svc.export_bundle(redact_secrets=False)).sections["mcp_sites"][0]
    assert "last_probe_status" not in site and "id" not in site


@pytest.mark.asyncio
async def test_export_skips_deleted_scheduled_tasks(svc):
    names = [t["name"] for t in (await svc.export_bundle(redact_secrets=False)).sections["scheduled_tasks"]]
    assert "已删除任务" not in names


@pytest.mark.asyncio
async def test_export_singletons_use_null_when_no_row(svc_without_rows):
    bundle = await svc_without_rows.export_bundle(redact_secrets=False)
    assert bundle.sections["external_memory_global_config"] is None
    assert bundle.sections["task_config"] is None


@pytest.mark.asyncio
async def test_export_empty_collection_is_empty_list_not_null(svc_without_rows):
    assert svc_without_rows and (await svc_without_rows.export_bundle(redact_secrets=False)).sections["providers"] == []


@pytest.mark.asyncio
async def test_export_gateway_home_targets_iterates_platform_enum(svc):
    platforms = [t["platform"] for t in (await svc.export_bundle(redact_secrets=False)).sections["gateway_home_targets"]]
    assert platforms == ["feishu"]


@pytest.mark.asyncio
async def test_export_task_config_payload_is_overrides_object(svc):
    section = (await svc.export_bundle(redact_secrets=False)).sections["task_config"]
    assert section == {"overrides": {"task_lease_seconds": 1200}}


@pytest.mark.asyncio
async def test_export_external_memory_global_config_is_flat_name_set(svc):
    section = (await svc.export_bundle(redact_secrets=False)).sections["external_memory_global_config"]
    assert section == {"enabled_providers": ["builtin", "mem0-main"]}


@pytest.mark.asyncio
async def test_export_skills_only_carry_two_flags(svc):
    skills = (await svc.export_bundle(redact_secrets=False)).sections["skills"]
    assert {s["name"] for s in skills} == {"web-research", "legacy-skill"}
    for skill in skills:
        assert set(skill) == {"name", "enabled", "chat_selectable"}


@pytest.mark.asyncio
async def test_export_scheduled_task_carries_policy_and_delivery_context(svc):
    task = (await svc.export_bundle(redact_secrets=False)).sections["scheduled_tasks"][0]
    assert task["cron_expression"] == "0 9 * * *"
    assert task["timezone"] == "Asia/Shanghai"
    assert task["delivery_target"] == "origin"
    assert task["delivery_context"] == {"receive_id": "ou_x", "receive_id_type": "open_id"}
    assert task["status"] == "active"
    assert task["execution_policy"] == {
        "mode": "unattended",
        "tool_exposure_policy": "safe_only",
        "allow_confirm_tools": False,
        "allowed_tools": ["web_search"],
    }


@pytest.mark.asyncio
async def test_export_sections_are_json_serializable(svc):
    bundle = await svc.export_bundle(redact_secrets=False)
    json.dumps(bundle.sections)


# --------------------------------------------------------------------------
# T4: import
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_creates_then_second_run_skips_everything(target, bundle):
    first = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    second = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert first.counts[ImportOutcome.CREATED] > 0
    assert second.counts[ImportOutcome.CREATED] == 0
    assert second.counts.get(ImportOutcome.UPDATED, 0) == 0


@pytest.mark.asyncio
async def test_overwrite_updates_base_url_model_and_api_key(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    bundle.sections["providers"][0]["base_url"] = "https://new.example.com"
    bundle.sections["providers"][0]["model"] = "new-model"
    bundle.sections["providers"][0]["api_key"] = "sk-new"
    report = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    row = (await target.registries.provider_registry.list_providers())[0]
    assert row.base_url == "https://new.example.com" and row.model == "new-model"
    assert await target.registries.provider_registry.get_secret(row.id) == "sk-new"
    assert report.counts[ImportOutcome.UPDATED] >= 1


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(target, bundle):
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=True)
    assert await target.registries.provider_registry.list_providers() == []
    assert target.registries.provider_registry.secrets == {}
    assert report.items  # 仍产出差异预览


@pytest.mark.asyncio
async def test_redacted_secret_keeps_target_value_on_update(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    pid = (await target.registries.provider_registry.list_providers())[0].id
    bundle.redacted = True
    bundle.sections["providers"][0]["api_key"] = None      # None = 已剥离
    bundle.sections["providers"][0]["base_url"] = "https://changed"
    report = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    assert await target.registries.provider_registry.get_secret(pid) == SEED_SECRET
    item = next(i for i in report.items if i.section == "providers")
    assert item.outcome is ImportOutcome.DEGRADED
    assert any("redacted" in r for r in item.reasons)


@pytest.mark.asyncio
async def test_redacted_secret_on_create_lands_without_secret_and_degraded(target, bundle):
    bundle.redacted = True
    bundle.sections["providers"][0]["api_key"] = None
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    pid = (await target.registries.provider_registry.list_providers())[0].id
    assert await target.registries.provider_registry.get_secret(pid) in (None, "")
    assert next(i for i in report.items if i.section == "providers").outcome is ImportOutcome.DEGRADED


@pytest.mark.asyncio
async def test_empty_string_secret_clears_target_value(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    pid = (await target.registries.provider_registry.list_providers())[0].id
    bundle.sections["providers"][0]["api_key"] = ""        # "" = 明确清空
    await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    assert await target.registries.provider_registry.get_secret(pid) is None


@pytest.mark.asyncio
async def test_active_provider_is_not_stolen_when_target_already_has_one(target, bundle):
    await target.seed_active_provider(name="目标原 active")
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert (await target.registries.provider_registry.get_active()).name == "目标原 active"
    assert any(
        i.outcome is ImportOutcome.DEGRADED and any("active" in r for r in i.reasons)
        for i in report.items if i.section == "providers"
    )


@pytest.mark.asyncio
async def test_bundle_with_two_active_providers_is_rejected_before_any_write(target, bundle):
    bundle.sections["providers"].append({**bundle.sections["providers"][0], "name": "second", "is_active": True})
    with pytest.raises(ConfigBundleValidationError):
        await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert await target.registries.provider_registry.list_providers() == []


@pytest.mark.asyncio
async def test_bundle_with_duplicate_natural_key_is_rejected_before_any_write(target, bundle):
    bundle.sections["mcp_sites"].append(dict(bundle.sections["mcp_sites"][0]))
    with pytest.raises(ConfigBundleValidationError):
        await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert target.registries.mcp_registry.rows == {}


@pytest.mark.asyncio
async def test_unsupported_schema_version_is_rejected_before_any_write(target, bundle):
    bundle.schema_version = BUNDLE_SCHEMA_VERSION + 1
    with pytest.raises(ConfigBundleValidationError):
        await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert await target.registries.provider_registry.list_providers() == []


@pytest.mark.asyncio
async def test_external_memory_lands_disabled_when_target_slot_taken(target, bundle):
    target.seed_external_provider(name="existing-provider", enabled=True)
    target.registries.external_memory_config.set_enabled(["existing-provider"])
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert target.registries.external_memory_config.get_enabled() == {"existing-provider"}
    imported = [p for p in target.registries.external_memory_provider_registry.list_providers()]
    assert all(p.enabled is False for p in imported if p.name != "existing-provider")
    assert any(i.section == "external_memory_global_config" and i.outcome is ImportOutcome.DEGRADED
               for i in report.items)


@pytest.mark.asyncio
async def test_external_memory_none_and_empty_set_are_distinguished(target, bundle):
    bundle.sections["external_memory_global_config"] = None      # 源端从未配置
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert target.registries.external_memory_config.get_enabled() is None  # 不得写成 set()


@pytest.mark.asyncio
async def test_schedules_default_to_disabled_and_paused(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    task = (await target.registries.schedule_service.registry.list())[0]
    assert task.enabled is False and task.status is ScheduledTaskStatus.PAUSED
    assert task.session_id.startswith("schedule-")  # 新建空 session，不复制来源 session_id


@pytest.mark.asyncio
async def test_with_schedules_enables_only_source_enabled_tasks(target, bundle):
    bundle.sections["scheduled_tasks"] = [
        {**bundle.sections["scheduled_tasks"][0], "name": "on", "enabled": True, "status": "active"},
        {**bundle.sections["scheduled_tasks"][0], "name": "off", "enabled": False, "status": "paused"},
    ]
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=True, dry_run=False)
    by_name = {t.name: t for t in await target.registries.schedule_service.registry.list()}
    assert by_name["on"].enabled is True and by_name["off"].enabled is False


@pytest.mark.asyncio
async def test_invalid_schedule_is_failed_and_never_enabled(target, bundle):
    bundle.sections["scheduled_tasks"][0]["cron_expression"] = "not-a-cron"
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=True, dry_run=False)
    assert await target.registries.schedule_service.registry.list() == []
    assert any(i.section == "scheduled_tasks" and i.outcome is ImportOutcome.FAILED for i in report.items)


@pytest.mark.asyncio
async def test_execution_policy_and_delivery_context_round_trip(target, bundle):
    policy = {"mode": "unattended", "tool_exposure_policy": "safe_only",
              "allow_confirm_tools": False, "allowed_tools": ["a"]}
    bundle.sections["scheduled_tasks"][0]["execution_policy"] = policy
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    exported = await target.svc.export_bundle(redact_secrets=False)
    restored = exported.sections["scheduled_tasks"][0]
    assert restored["execution_policy"] == policy
    assert restored["delivery_context"] == bundle.sections["scheduled_tasks"][0]["delivery_context"]


@pytest.mark.asyncio
async def test_unknown_execution_policy_mode_is_rejected_before_any_write(target, bundle):
    bundle.sections["scheduled_tasks"][0]["execution_policy"]["mode"] = "custom"
    with pytest.raises(ConfigBundleValidationError):
        await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert await target.registries.provider_registry.list_providers() == []
    assert await target.registries.schedule_service.registry.list() == []


@pytest.mark.asyncio
async def test_skills_only_apply_flags_and_missing_skill_is_degraded(target, bundle):
    await target.seed_skill(name="present", enabled=True, chat_selectable=True)
    bundle.sections["skills"] = [
        {"name": "present", "enabled": False, "chat_selectable": False},
        {"name": "absent", "enabled": False, "chat_selectable": False},
    ]
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False, pre_existing={"skills": [], "plugins": []})
    present = await target.registries.skill_registry.get_skill("present")
    assert present.enabled is False and present.chat_selectable is False
    assert target.registries.skill_registry.upsert_calls == []   # 绝不新建 skill
    assert next(i for i in report.items if i.natural_key == "absent").outcome is ImportOutcome.DEGRADED


@pytest.mark.asyncio
async def test_plugin_version_mismatch_is_degraded_without_writing_config(target, bundle):
    await target.seed_plugin(key="opa-client", version="2.0.0")
    bundle.sections["plugins"] = [
        {"key": "opa-client", "name": "OPA Client", "kind": "standalone", "version": "1.0.0", "enabled": True,
         "config_json": {"endpoint": "https://a"}, "plugin_secrets": {"token": "t"}},
    ]
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False, pre_existing={"skills": [], "plugins": []})
    assert target.registries.plugin_registry.update_config_calls == []
    assert next(i for i in report.items if i.section == "plugins").outcome is ImportOutcome.DEGRADED


@pytest.mark.asyncio
async def test_task_config_uses_target_version_cas_and_reports_conflict(target, bundle):
    await target.registries.task_config_store.save(
        TaskConfigOverrides(task_lease_seconds=120), expected_version=0, updated_by="human")
    bundle.sections["task_config"] = {"overrides": {"task_lease_seconds": 180}}
    report = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    stored = await target.registries.task_config_store.get()
    assert stored.updated_by == "config-import" and stored.version == 2
    bundle.sections["task_config"] = {"overrides": {"task_lease_seconds": 240}}
    target.registries.task_config_store.raise_conflict_once = True
    report2 = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    assert next(i for i in report2.items if i.section == "task_config").outcome is ImportOutcome.FAILED
    assert (await target.registries.task_config_store.get()).overrides.task_lease_seconds == 180
    assert report2.exit_code == 1
    assert report.exit_code == 0


@pytest.mark.asyncio
async def test_task_config_merge_keeps_target_row_untouched(target, bundle):
    await target.registries.task_config_store.save(
        TaskConfigOverrides(task_lease_seconds=120), expected_version=0, updated_by="human")
    bundle.sections["task_config"] = {"overrides": {"task_lease_seconds": 180}}
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    stored = await target.registries.task_config_store.get()
    assert stored.updated_by == "human" and stored.version == 1
    item = next(i for i in report.items if i.section == "task_config")
    assert item.outcome is ImportOutcome.SKIPPED and item.action == "none"


@pytest.mark.asyncio
async def test_gateway_home_target_is_upserted_per_platform(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert (await target.registries.gateway_registry.get_home_target(Platform.FEISHU)) is not None


@pytest.mark.asyncio
async def test_single_record_failure_does_not_block_other_records(target, bundle):
    target.registries.mcp_registry.fail_on_name = bundle.sections["mcp_sites"][0]["name"]
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert report.exit_code == 1
    assert report.counts[ImportOutcome.FAILED] == 1
    assert report.counts[ImportOutcome.CREATED] > 0          # 其它段照常落地
    assert (await target.registries.provider_registry.list_providers()) != []


@pytest.mark.asyncio
async def test_overwrite_schedule_preserves_id_session_and_history(target, bundle):
    """AC20：overwrite 更新 prompt/cron 等，但保留目标 id、session_id 与运行历史。"""
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    before = (await target.registries.schedule_service.registry.list())[0]
    await target.mark_executed(before.id, last_status="succeeded", unread_count=3)
    bundle.sections["scheduled_tasks"][0]["prompt"] = "改过的提示词"
    await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    after = (await target.registries.schedule_service.registry.list())[0]
    assert after.id == before.id and after.session_id == before.session_id
    assert after.prompt == "改过的提示词"
    assert after.last_status.value == "succeeded" and after.unread_count == 3


@pytest.mark.asyncio
async def test_overwrite_schedule_prompt_only_edit_keeps_next_run_at(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    before = (await target.registries.schedule_service.registry.list())[0]
    bundle.sections["scheduled_tasks"][0]["prompt"] = "只改提示词"
    await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    after = (await target.registries.schedule_service.registry.list())[0]
    assert after.next_run_at == before.next_run_at


@pytest.mark.asyncio
async def test_created_schedule_session_is_registered_and_empty(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    task = (await target.registries.schedule_service.registry.list())[0]
    assert target.registries.schedule_service.session_service.created == [task.session_id]


@pytest.mark.asyncio
async def test_duplicate_target_task_name_fails_instead_of_picking_one(target, bundle):
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    existing = (await target.registries.schedule_service.registry.list())[0]
    clone = ScheduledTask(**{**existing.__dict__, "id": "sched-clone"})
    target.registries.schedule_service.registry.seed(clone)
    bundle.sections["scheduled_tasks"][0]["prompt"] = "不该生效"
    report = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    item = next(i for i in report.items if i.section == "scheduled_tasks")
    assert item.outcome is ImportOutcome.FAILED and item.action == "none"
    assert all(t.prompt != "不该生效" for t in await target.registries.schedule_service.registry.list())


@pytest.mark.asyncio
async def test_overwrite_refuses_task_holding_valid_lease(target, bundle):
    """AC20：持有未过期 lease 的任务正在执行，不得被覆盖。"""
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    task = (await target.registries.schedule_service.registry.list())[0]
    await target.hold_lease(task.id, seconds=300)
    bundle.sections["scheduled_tasks"][0]["prompt"] = "不该生效"
    report = await target.svc.import_bundle(bundle, mode=ImportMode.OVERWRITE, with_schedules=False, dry_run=False)
    assert (await target.registries.schedule_service.registry.list())[0].prompt != "不该生效"
    item = next(i for i in report.items if i.section == "scheduled_tasks")
    assert item.outcome is ImportOutcome.FAILED and item.action == "none"


@pytest.mark.asyncio
async def test_degraded_without_write_reports_action_none(target, bundle):
    """T7 S9 的重启判定只看 action：幂等复跑时即便仍有 degraded，也必须全是 action=none。"""
    bundle.redacted = True
    bundle.sections["providers"][0]["api_key"] = None
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    second = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert second.counts[ImportOutcome.DEGRADED] > 0          # degraded 幂等重现
    assert all(i.action == "none" for i in second.items)      # 但没有任何实际写入


@pytest.mark.asyncio
async def test_skill_absent_from_pre_existing_is_treated_as_new_and_flags_applied(target, bundle):
    """目标机首次扫描出的 skill 不在 pre_existing 中，merge 下仍须恢复标志。"""
    await target.seed_skill(name="scanned", enabled=True, chat_selectable=True)
    bundle.sections["skills"] = [{"name": "scanned", "enabled": False, "chat_selectable": False}]
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False,
                                   dry_run=False, pre_existing={"skills": [], "plugins": []})
    s = await target.registries.skill_registry.get_skill("scanned")
    assert s.enabled is False and s.chat_selectable is False


@pytest.mark.asyncio
async def test_skill_present_in_pre_existing_is_skipped_under_merge(target, bundle):
    await target.seed_skill(name="mine", enabled=True, chat_selectable=True)
    bundle.sections["skills"] = [{"name": "mine", "enabled": False, "chat_selectable": False}]
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False,
                                   dry_run=False, pre_existing={"skills": ["mine"], "plugins": []})
    s = await target.registries.skill_registry.get_skill("mine")
    assert s.enabled is True                                  # 目标原有配置不被 merge 覆盖


@pytest.mark.asyncio
async def test_skill_pre_existing_defaults_to_current_registry_when_not_supplied(target, bundle):
    await target.seed_skill(name="mine", enabled=True, chat_selectable=True)
    bundle.sections["skills"] = [{"name": "mine", "enabled": False, "chat_selectable": False}]
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    s = await target.registries.skill_registry.get_skill("mine")
    assert s.enabled is True


@pytest.mark.asyncio
async def test_builtin_stays_in_enabled_set_when_an_external_name_is_unresolvable(target, bundle):
    """AC21：external_memory_global_config 是一个平铺的 enabled 名字集合，
    "builtin" 只是其中一个名字（app/application/external_memory_manager.py:343 _is_enabled）。
    包里某个外部 provider 在目标机解析不到时，只能剔掉那个名字并记 degraded，
    绝不能整段放弃或把 builtin 一起抹掉——那会静默关掉目标机的内置记忆。
    注意该 registry 是同步端口（app/domain/external_memory.py），不要 await。"""
    bundle.sections["external_memory_global_config"] = {
        "enabled_providers": ["builtin", "src-only-ext"]}          # src-only-ext 目标机不存在
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    enabled = target.registries.external_memory_config.get_enabled()   # 同步
    assert "builtin" in enabled
    assert "src-only-ext" not in enabled
    item = next(i for i in report.items if i.section == "external_memory_global_config")
    assert item.outcome is ImportOutcome.DEGRADED and item.action == "updated"


@pytest.mark.asyncio
async def test_external_memory_empty_list_disables_everything(target, bundle):
    bundle.sections["external_memory_global_config"] = {"enabled_providers": []}
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert target.registries.external_memory_config.get_enabled() == set()


@pytest.mark.asyncio
async def test_task_config_is_validated_against_target_env_before_save(target, bundle):
    """AC21：包内 overrides 必须与目标机 env base 合并后过 validate_task_config
    （由 TaskConfigService.replace_overrides 内部完成，非 ConfigBundleService 自己拼），非法值 -> failed 不落库。"""
    bundle.sections["task_config"] = {"overrides": {"task_lease_seconds": -1}}
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    assert await target.registries.task_config_store.get() is None
    item = next(i for i in report.items if i.section == "task_config")
    assert item.outcome is ImportOutcome.FAILED and item.action == "none"


@pytest.mark.asyncio
async def test_task_config_empty_overrides_object_is_written(target, bundle):
    bundle.sections["task_config"] = {"overrides": {}}
    await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    stored = await target.registries.task_config_store.get()
    assert stored is not None and stored.overrides.to_dict() == {}


@pytest.mark.asyncio
async def test_report_never_contains_secret_values(target, bundle):
    report = await target.svc.import_bundle(bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False)
    serialized = json.dumps(report.to_dict())
    assert SEED_SECRET not in serialized
    assert "sk-" not in serialized


# --------------------------------------------------------------------------
# Section-level failure containment (Phase 5 scan, error-handling dimension)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_failure_between_the_row_guards_is_contained_not_escaped(target, bundle):
    """A failure outside the per-row guards must not escape import_bundle.

    import_bundle promises that after validation "one bad record degrades or
    fails only itself". The per-row try blocks keep that promise for the
    writes, but the work around them -- reading the target's current state to
    resolve a conflict -- sits outside every guard. When that raises, the
    exception travels all the way to the CLI, which turns any exception into
    exit 2: "bundle format error, target untouched". Providers and knowledge
    bases are committed by then, so that code tells the operator the exact
    opposite of the truth and invites a retry against a target they believe
    is clean.
    """
    def boom():
        raise RuntimeError("target registry is unavailable")

    target.registries.external_memory_provider_registry.list_providers = boom

    report = await target.svc.import_bundle(
        bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False
    )

    # 1 is the honest code: "some records failed, the rest were applied".
    assert report.exit_code == 1
    failed = [i for i in report.items if i.outcome is ImportOutcome.FAILED]
    # Two sections, not one: the global config section resolves its enabled
    # names against the same registry, so one broken collaborator legitimately
    # fails both. What matters is that both are contained.
    assert [i.section for i in failed] == [
        "external_memory_providers",
        "external_memory_global_config",
    ]
    assert failed[0].reasons == ["write failed: RuntimeError"]
    # Non-vacuity: the sections on both sides of the broken one still ran, so
    # this is containment rather than an aborted import wearing a report.
    written = {i.section for i in report.items if i.action != "none"}
    assert "providers" in written  # ordered before the failure
    assert "gateway_home_targets" in written  # ordered after it


@pytest.mark.asyncio
async def test_a_contained_failure_logs_its_location_but_never_the_message(
    target, bundle, caplog
):
    """Debt D057: a contained failure must be diagnosable without leaking values.

    The report only ever carries the exception type, which tells an operator
    that something broke but not where. The scan asked for logger.exception;
    that renders the exception message, and the messages on these paths can
    contain a value taken from the row being written -- an api_key on a
    provider, a secret_value on a plugin. So the log carries the type and the
    code location and nothing else. This test pins both halves: the location
    is there, the message is not.
    """
    secret = "sk-live-must-never-reach-a-log-file"

    def boom():
        raise RuntimeError(f"target registry is unavailable: {secret}")

    target.registries.external_memory_provider_registry.list_providers = boom

    logger_name = "app.application.config_bundle_service"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        report = await target.svc.import_bundle(
            bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False
        )

    assert report.exit_code == 1
    records = [r for r in caplog.records if r.name == logger_name]
    assert records, "a contained failure that leaves no trace at all is D057 again"
    rendered = "\n".join(r.getMessage() for r in records)

    # Diagnosable: which section, which type, which line raised.
    assert "external_memory_providers" in rendered
    assert "RuntimeError" in rendered
    assert f"{Path(__file__).name}:" in rendered

    # Redacted: neither the value nor the message that carried it.
    assert secret not in rendered
    assert "target registry is unavailable" not in rendered
    # exc_info would smuggle the message back in through the traceback.
    assert all(r.exc_info is None for r in records)


@pytest.mark.asyncio
async def test_a_failing_singleton_section_is_contained_too(target, bundle):
    """The singleton sections reach the item list by a different call shape
    (append of one item, not concatenation of a list), so containment has to
    be proven for them separately."""
    def boom(*args, **kwargs):
        raise RuntimeError("global config row is unreadable")

    target.svc._import_external_global_config = boom

    report = await target.svc.import_bundle(
        bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False
    )

    assert report.exit_code == 1
    failed = [i for i in report.items if i.outcome is ImportOutcome.FAILED]
    assert [i.section for i in failed] == ["external_memory_global_config"]
    written = {i.section for i in report.items if i.action != "none"}
    assert "providers" in written and "gateway_home_targets" in written


@pytest.mark.asyncio
async def test_a_structurally_invalid_bundle_still_raises_before_any_write(target, bundle):
    """Containment must not swallow validation. Nothing is written yet at that
    point, so the CLI's exit 2 -- "target untouched" -- is accurate there and
    has to keep working."""
    bundle.sections.pop("skills")
    with pytest.raises(ConfigBundleValidationError):
        await target.svc.import_bundle(
            bundle, mode=ImportMode.MERGE, with_schedules=False, dry_run=False
        )
    assert target.registries.provider_registry.rows == {}
