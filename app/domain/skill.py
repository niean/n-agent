from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Protocol


class SkillReadiness(str, Enum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    SETUP_NEEDED = "setup_needed"
    SCAN_ERROR = "scan_error"


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
