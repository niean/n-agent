from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Protocol


class SkillReadiness(str, Enum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    SETUP_NEEDED = "setup_needed"
    SCAN_ERROR = "scan_error"


class SkillSource(str, Enum):
    SEED = "seed"
    AGENT = "agent"
    USER = "user"


class SkillWriteOrigin(str, Enum):
    FOREGROUND = "foreground"
    BACKGROUND_REVIEW = "background_review"


class SkillWriteAction(str, Enum):
    CREATE = "create"
    PATCH = "patch"
    EDIT = "edit"
    DELETE = "delete"
    WRITE_FILE = "write_file"
    REMOVE_FILE = "remove_file"


class SkillLifecycleState(str, Enum):
    """Curator 周期维护的生命周期状态。

    与 skill_usage 表的 state 列（TEXT，默认 'active'）一一对应。枚举集中
    校验合法值，避免裸字符串散落。
    """

    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    version: str
    platforms: list[str]
    tags: list[str]
    related_skills: list[str]
    author: str
    license: str
    setup_help: str | None
    required_env_vars: list[str]
    raw: dict[str, Any]
    metadata: dict[str, str] = field(default_factory=dict)
    compatibility: str = ""
    allowed_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    relative_path: str
    description: str
    platforms: list[str]
    frontmatter: SkillFrontmatter
    enabled: bool
    readiness: SkillReadiness
    last_scan_status: str | None
    last_scan_error: str | None
    last_seen_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    source: SkillSource = SkillSource.USER
    chat_selectable: bool = True


class SkillRegistry(Protocol):
    async def list_skills(self, include_disabled: bool = True) -> list[Skill]: ...
    async def get_skill(self, name: str) -> Skill | None: ...
    async def upsert_skill(self, skill: Skill) -> Skill: ...
    async def delete_skill(self, name: str) -> bool: ...
    async def set_enabled(self, name: str, enabled: bool) -> Skill: ...
    async def set_chat_selectable(self, name: str, value: bool) -> Skill: ...
    async def replace_all_skills(self, skills: Iterable[Skill]) -> list[Skill]: ...


class SkillNotFoundError(Exception):
    pass


class SkillValidationError(Exception):
    pass


class SkillScanError(Exception):
    pass


@dataclass(frozen=True)
class SkillManageRequest:
    action: SkillWriteAction
    name: str
    origin: SkillWriteOrigin
    content: str = ""
    old_string: str = ""
    new_string: str = ""
    file_path: str = ""
    file_content: str = ""
    category: str = ""
    approved_replay: bool = False
    absorbed_into: str = ""


@dataclass(frozen=True)
class SkillUsage:
    created_by: str
    use_count: int
    view_count: int
    patch_count: int
    created_at: datetime | None
    last_used_at: datetime | None
    last_viewed: datetime | None
    last_patched_at: datetime | None
    state: str
    pinned: bool
    archived_at: datetime | None


@dataclass(frozen=True)
class SkillPendingWrite:
    pending_id: str
    action: SkillWriteAction
    skill_name: str
    origin: SkillWriteOrigin
    summary: str
    diff: str
    payload: dict[str, Any]
    state: str  # pending/approved_in_progress/approved/rejected/failed
    error: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class SkillManageResult:
    success: bool
    staged: bool
    pending_id: str | None
    skill_name: str
    action: SkillWriteAction
    summary: str
    diff: str | None
    error: str | None


# ---------------------------------------------------------------------------
# Curator 周期维护值对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratorState:
    """Curator 自身的调度状态（持久化于 curator_state 表）。"""

    last_run_at: str | None = None
    last_run_duration_seconds: float | None = None
    last_run_summary: str | None = None
    last_report_path: str | None = None
    paused: bool = False
    run_count: int = 0


@dataclass(frozen=True)
class CuratorConfig:
    """Curator 运行配置，从 Settings 派生。字段语义对齐 HermesAgent。"""

    enabled: bool = True
    interval_hours: int = 168
    min_idle_hours: float = 2.0
    stale_after_days: int = 30
    archive_after_days: int = 90
    prune_seeds: bool = False
    consolidate: bool = False
    consolidate_max_iterations: int = 64


@dataclass(frozen=True)
class CuratorTransitions:
    """apply_automatic_transitions 的返回计数（纯函数，无 LLM）。"""

    checked: int = 0
    marked_stale: int = 0
    archived: int = 0
    reactivated: int = 0
    seeded: int = 0


@dataclass(frozen=True)
class CuratorRunResult:
    """run_curator_review 的返回。"""

    started_at: str
    auto_transitions: CuratorTransitions
    summary_so_far: str


@dataclass(frozen=True)
class CuratorSkillReport:
    """单个 curator-managed skill 的快照行，供状态机遍历与报告 diff。

    source 为从 registry 读取的 SkillSource 字符串值（'seed'/'agent'/'user'）。
    _persisted 标记是否存在真实 usage 记录（False 表示首次见到的 backfill）。
    """

    name: str
    source: str
    state: str
    pinned: bool
    use_count: int
    view_count: int
    patch_count: int
    created_at: datetime | None
    last_used_at: datetime | None
    last_viewed: datetime | None
    last_patched_at: datetime | None
    last_activity_at: str | None
    activity_count: int
    _persisted: bool


class SkillUsageRegistry(Protocol):
    async def get(self, name: str) -> SkillUsage | None: ...
    async def upsert(self, name: str, usage: SkillUsage) -> SkillUsage: ...
    async def increment_use(self, name: str) -> None: ...
    async def increment_view(self, name: str) -> None: ...
    async def increment_patch(self, name: str) -> None: ...
    async def set_state(self, name: str, state: str) -> None: ...
    async def set_pinned(self, name: str, pinned: bool) -> None: ...
    async def list_curator_managed(
        self, prune_seeds: bool, protected_names: set[str]
    ) -> list[CuratorSkillReport]: ...
    async def seed_record_if_missing(self, name: str) -> None: ...
    async def archive_skill(self, name: str) -> None: ...
    async def restore_skill(self, name: str) -> None: ...


class SkillPendingStore(Protocol):
    async def stage(self, write: SkillPendingWrite) -> str: ...
    async def list(self) -> list[SkillPendingWrite]: ...
    async def get(self, pending_id: str) -> SkillPendingWrite | None: ...
    async def approve_take(self, pending_id: str) -> SkillPendingWrite | None: ...
    async def reject(self, pending_id: str) -> bool: ...
    async def clear(self, pending_id: str) -> None: ...


class SkillBackupStore(Protocol):
    async def snapshot(self) -> str: ...
    async def list(self) -> list[str]: ...
    async def rollback(self, snapshot_id: str) -> bool: ...


class CuratorStateStore(Protocol):
    """Curator 调度状态持久化端口。"""

    async def load(self) -> CuratorState: ...
    async def save(self, state: CuratorState) -> None: ...
    async def set_paused(self, paused: bool) -> None: ...


class SkillWriteError(Exception):
    pass


class CuratorError(Exception):
    """Curator 编排错误基类。"""


class CuratorStateError(CuratorError):
    """curator_state 读写错误。"""


class SkillPatchConflictError(SkillWriteError):
    pass


class SkillPinError(Exception):
    pass


class SkillBackupError(Exception):
    pass
