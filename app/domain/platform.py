from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class Platform(str, Enum):
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WECOM = "wecom"


class PlatformKind(str, Enum):
    IM = "im"


@dataclass(frozen=True)
class PlatformDescriptor:
    platform: Platform
    display_name: str
    kind: PlatformKind
    config_summary: dict[str, Any] = field(default_factory=dict)


class PlatformLifecycle(Protocol):
    def is_connected(self) -> bool: ...

    def fatal_error(self) -> tuple[str, str] | None: ...


class PlatformRegistry(Protocol):
    def list(self) -> list[PlatformDescriptor]: ...

    def get(self, platform: Platform) -> PlatformDescriptor | None: ...

    def get_lifecycle(self, platform: Platform) -> PlatformLifecycle | None: ...
