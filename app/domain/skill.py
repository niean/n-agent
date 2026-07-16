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


class SkillRegistry(Protocol):
    async def list_skills(self, include_disabled: bool = True) -> list[Skill]: ...
    async def get_skill(self, name: str) -> Skill | None: ...
    async def upsert_skill(self, skill: Skill) -> Skill: ...
    async def delete_skill(self, name: str) -> bool: ...
    async def set_enabled(self, name: str, enabled: bool) -> Skill: ...
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


class SkillUsageRegistry(Protocol):
    async def get(self, name: str) -> SkillUsage | None: ...
    async def upsert(self, name: str, usage: SkillUsage) -> SkillUsage: ...
    async def increment_use(self, name: str) -> None: ...
    async def increment_view(self, name: str) -> None: ...
    async def increment_patch(self, name: str) -> None: ...
    async def set_state(self, name: str, state: str) -> None: ...
    async def set_pinned(self, name: str, pinned: bool) -> None: ...


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


class SkillWriteError(Exception):
    pass


class SkillPatchConflictError(SkillWriteError):
    pass


class SkillPinError(Exception):
    pass


class SkillBackupError(Exception):
    pass
