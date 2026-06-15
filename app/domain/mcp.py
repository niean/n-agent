from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


class McpTransportType(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"
    STDIO = "stdio"


class McpProbeStatus(str, Enum):
    NEVER = "never"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class McpSite:
    name: str
    url: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    transport_type: McpTransportType = McpTransportType.STREAMABLE_HTTP
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    last_probe_status: McpProbeStatus = McpProbeStatus.NEVER
    last_probe_error: str | None = None
    last_probed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class McpTool:
    site_id: str
    remote_name: str
    local_name: str
    description: str
    input_schema: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    enabled: bool = True
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class McpRemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpProbeResult:
    tools: list[McpRemoteTool]


class McpSiteNotFoundError(Exception):
    pass


class McpSiteValidationError(Exception):
    pass


class McpProbeError(Exception):
    pass


class McpSiteRegistry(Protocol):
    async def list_sites(self) -> list[McpSite]: ...
    async def get_site(self, site_id: str) -> McpSite | None: ...
    async def get_site_by_name(self, name: str) -> McpSite | None: ...
    async def create_site(self, site: McpSite) -> McpSite: ...
    async def update_site(self, site: McpSite) -> McpSite: ...
    async def delete_site(self, site_id: str) -> bool: ...
    async def update_probe_status(self, site_id: str, status: McpProbeStatus, error: str | None = None) -> None: ...
    async def list_tools(self, site_id: str | None = None) -> list[McpTool]: ...
    async def replace_site_tools(self, site_id: str, tools: list[McpTool]) -> list[McpTool]: ...
    async def update_tool_enabled(self, site_id: str, tool_id: str, enabled: bool) -> McpTool: ...
