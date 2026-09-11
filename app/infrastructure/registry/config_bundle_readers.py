"""Config-bundle port set -- SQLite adapters assembled without app bootstrap.

`n-agent config export` and `config import --dry-run` must open an existing
deployment database and read the ten configuration tables. They must NOT run
the regular application bootstrap: no schema creation on a fresh path, no
provider/KB seeding, no skill/plugin scanning, no background tasks.

Two modes:
  read_only=False -- the write path used by a real import. It reuses the
    existing schema through the normal adapters (initialize=True is harmless
    and idempotent: every DDL statement is CREATE ... IF NOT EXISTS) but skips
    every seeder and scanner, because those belong to the running service.
  read_only=True  -- every adapter opens the file with the SQLite URI
    `mode=ro`. A missing database raises instead of being created, no parent
    directory is made, no DDL runs, and any write raises OperationalError.

This module stays in Infrastructure: it wires adapters only. The Application
services that sit on top (ScheduleService, TaskConfigService) are wired by the
composition root in app/main.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_external_memory_config import (
    SQLiteExternalMemoryConfig,
)
from app.infrastructure.registry.sqlite_external_memory_provider_registry import (
    SQLiteExternalMemoryProviderRegistry,
)
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry
from app.infrastructure.registry.sqlite_knowledge_registry import SQLiteKnowledgeBaseRegistry
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry
from app.infrastructure.registry.sqlite_plugin_registry import SQLitePluginRegistry
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.infrastructure.registry.sqlite_schedule_registry import SQLiteScheduledTaskRegistry
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.infrastructure.registry.sqlite_task_config_store import SqliteTaskConfigStore
from app.infrastructure.schedule.croniter_calculator import CroniterScheduleCalculator
from app.infrastructure.sqlite_support import open_sqlite


@dataclass(frozen=True)
class ConfigBundleRegistries:
    """Every SQLite port the config bundle touches, plus the calculator and
    memory store the Application layer needs to finish the wiring."""

    provider_registry: SQLiteProviderRegistry
    knowledge_registry: SQLiteKnowledgeBaseRegistry
    mcp_registry: SQLiteMcpSiteRegistry
    external_memory_provider_registry: SQLiteExternalMemoryProviderRegistry
    external_memory_config: SQLiteExternalMemoryConfig
    plugin_registry: SQLitePluginRegistry
    skill_registry: SQLiteSkillRegistry
    schedule_registry: SQLiteScheduledTaskRegistry
    gateway_registry: SQLiteGatewaySessionRegistry
    task_config_store: SqliteTaskConfigStore
    memory_store: SQLiteMemoryStore
    schedule_calculator: CroniterScheduleCalculator
    read_only: bool


def build_config_bundle_registries(
    sqlite_path: str | Path, *, read_only: bool
) -> ConfigBundleRegistries:
    """Assemble the ten configuration ports over ``sqlite_path``.

    With ``read_only`` the database must already exist: the probe connection
    below raises ``sqlite3.OperationalError`` on a missing file rather than
    silently creating one, which is the whole point of the read-only preview.
    """
    path = Path(sqlite_path)
    if read_only:
        open_sqlite(path, read_only=True).close()

    calculator = CroniterScheduleCalculator()
    initialize = not read_only
    return ConfigBundleRegistries(
        provider_registry=SQLiteProviderRegistry(path, initialize=initialize, read_only=read_only),
        knowledge_registry=SQLiteKnowledgeBaseRegistry(path, initialize=initialize, read_only=read_only),
        mcp_registry=SQLiteMcpSiteRegistry(path, initialize=initialize, read_only=read_only),
        external_memory_provider_registry=_external_provider_registry(path, read_only),
        external_memory_config=_external_memory_config(path, read_only),
        plugin_registry=SQLitePluginRegistry(path, initialize=initialize, read_only=read_only),
        skill_registry=SQLiteSkillRegistry(path, initialize=initialize, read_only=read_only),
        schedule_registry=SQLiteScheduledTaskRegistry(
            path, calculator, initialize=initialize, read_only=read_only
        ),
        gateway_registry=SQLiteGatewaySessionRegistry(path, initialize=initialize, read_only=read_only),
        task_config_store=SqliteTaskConfigStore(str(path), initialize=initialize, read_only=read_only),
        memory_store=SQLiteMemoryStore(path, initialize=initialize, read_only=read_only),
        schedule_calculator=calculator,
        read_only=read_only,
    )


def _external_provider_registry(
    path: Path, read_only: bool
) -> SQLiteExternalMemoryProviderRegistry:
    """These two adapters create their table via an explicit create_tables()
    call rather than in __init__, so the read-only branch simply skips it."""
    registry = SQLiteExternalMemoryProviderRegistry(path, read_only=read_only)
    if not read_only:
        registry.create_tables()
    return registry


def _external_memory_config(path: Path, read_only: bool) -> SQLiteExternalMemoryConfig:
    config = SQLiteExternalMemoryConfig(path, read_only=read_only)
    if not read_only:
        config.create_tables()
    return config
