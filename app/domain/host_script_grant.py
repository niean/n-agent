"""Immutable authorization records produced by controlled script writes."""

import hashlib
from typing import Protocol
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta

from app.domain.host_terminal import (
    _require_lower_sha256,
    _require_relative_script_path,
    _require_skill_name,
)
from app.domain.host_terminal_policy import (
    HostSkillScriptRule,
    HostTerminalPolicySnapshot,
)


@dataclass(frozen=True)
class HostScriptGrant:
    skill_name: str
    script_relative_path: str
    sha256: str
    revision: int
    origin: str
    updated_at: str
    state: str

    def __post_init__(self) -> None:
        _require_skill_name(self.skill_name)
        _require_relative_script_path(self.script_relative_path)
        if not self.script_relative_path.startswith(
            "scripts/"
        ) or not self.script_relative_path.endswith(".py"):
            raise ValueError("invalid_script_relative_path")
        _require_lower_sha256(self.sha256, "sha256")
        if type(self.revision) is not int or self.revision <= 0:
            raise ValueError("invalid_revision")
        if self.origin not in ("foreground", "background_review"):
            raise ValueError("invalid_origin")
        if self.state not in ("active", "revoked"):
            raise ValueError("invalid_state")
        try:
            if not isinstance(self.updated_at, str) or "T" not in self.updated_at:
                raise ValueError("invalid_updated_at")
            date = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            if date.utcoffset() != timedelta(0):
                raise ValueError("invalid_updated_at")
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_updated_at") from exc

    @property
    def identity(self) -> tuple[str, str]:
        return self.skill_name, self.script_relative_path

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "HostScriptGrant":
        if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError("invalid_script_grant")
        return cls(**value)


def merge_script_grants(
    base: HostTerminalPolicySnapshot, grants: list[HostScriptGrant]
) -> HostTerminalPolicySnapshot:
    latest = {}
    for grant in grants:
        if not isinstance(grant, HostScriptGrant) or grant.identity in latest:
            raise ValueError("invalid_script_grants")
        latest[grant.identity] = grant
    active = {key: g for key, g in latest.items() if g.state == "active"}
    rules = [
        r
        for r in base.skill_script_rules
        if (r.skill_name, r.script_relative_path) not in active
    ]
    used = {r.rule_id for r in base.rules}
    for identity, grant in sorted(active.items()):
        rule_id = "auto-script:" + hashlib.sha256(repr(identity).encode()).hexdigest()
        while rule_id in used:
            rule_id += ":"
        used.add(rule_id)
        rules.append(
            HostSkillScriptRule(
                rule_id, *identity, grant.sha256, (), allow_any_args=True
            )
        )
    return replace(base, skill_script_rules=tuple(rules))


@dataclass(frozen=True)
class HostScriptIntent:
    skill_name: str
    script_relative_path: str
    sha256: str
    revision: int
    origin: str
    updated_at: str
    state: str
    error: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return self.skill_name, self.script_relative_path

    def grant(self, state: str = "active") -> HostScriptGrant:
        return HostScriptGrant(
            self.skill_name,
            self.script_relative_path,
            self.sha256,
            self.revision,
            self.origin,
            self.updated_at,
            state,
        )


class HostScriptIntentStore(Protocol):
    async def prepare(
        self,
        skill_name: str,
        script_relative_path: str,
        sha256: str,
        origin: str,
        *,
        floor: int = 0,
    ) -> HostScriptIntent: ...
    async def list_current(self) -> list[HostScriptIntent]: ...
    async def transition(
        self, revision: int, expected: str, state: str, error: str | None = None
    ) -> bool: ...


class HostScriptGrantClient(Protocol):
    async def script_grants(
        self, operation: str, grant: HostScriptGrant | None = None
    ) -> list[HostScriptGrant]: ...
