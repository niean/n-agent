from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from app.application.tool_service import ToolService
from app.domain.mcp import (
    McpProbeError,
    McpProbeResult,
    McpProbeStatus,
    McpRemoteTool,
    McpSite,
    McpSiteNotFoundError,
    McpSiteRegistry,
    McpSiteValidationError,
    McpTool,
    McpTransportType,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


class McpClient(Protocol):
    async def probe_tools(self, site: McpSite) -> McpProbeResult: ...
    async def call_tool(self, site: McpSite, remote_name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class McpSiteInput:
    name: str
    url: str
    transport_type: McpTransportType = McpTransportType.STREAMABLE_HTTP
    enabled: bool = True


class McpService:
    def __init__(self, registry: McpSiteRegistry, client: McpClient, tool_service: ToolService | None = None):
        self.registry = registry
        self.client = client
        self.tool_service = tool_service

    async def list_sites(self) -> list[McpSite]:
        return await self.registry.list_sites()

    async def get_site(self, site_id: str) -> McpSite:
        site = await self.registry.get_site(site_id)
        if site is None:
            raise McpSiteNotFoundError(site_id)
        return site

    async def probe_site(self, payload: McpSiteInput) -> McpProbeResult:
        site = _site_from_input(payload)
        try:
            return await self.client.probe_tools(site)
        except Exception as exc:
            raise McpProbeError(_safe_error(exc)) from exc

    async def create_site_with_probe(self, payload: McpSiteInput, tool_include: list[str] | None = None) -> McpSite:
        site = _site_from_input(payload, probe_status=McpProbeStatus.SUCCESS)
        self._validate_site(site)
        try:
            probe = await self.client.probe_tools(site)
        except Exception as exc:
            raise McpProbeError(_safe_error(exc)) from exc
        saved = await self.registry.create_site(site)
        tools = await self._build_tools(saved.id, saved.name, probe.tools, tool_include)
        await self.registry.replace_site_tools(saved.id, tools)
        await self.registry.update_probe_status(saved.id, McpProbeStatus.SUCCESS)
        await self.refresh_registered_tool_surface()
        refreshed = await self.registry.get_site(saved.id)
        return refreshed or saved

    async def update_site(self, site_id: str, payload: McpSiteInput) -> McpSite:
        current = await self.get_site(site_id)
        updated = McpSite(
            id=current.id,
            name=payload.name.strip(),
            transport_type=payload.transport_type,
            url=payload.url.strip(),
            enabled=payload.enabled,
            last_probe_status=current.last_probe_status,
            last_probe_error=current.last_probe_error,
            last_probed_at=current.last_probed_at,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        self._validate_site(updated)
        saved = await self.registry.update_site(updated)
        await self.refresh_registered_tool_surface()
        return saved

    async def delete_site(self, site_id: str) -> None:
        if not await self.registry.delete_site(site_id):
            raise McpSiteNotFoundError(site_id)
        await self.refresh_registered_tool_surface()

    async def refresh_site_tools(self, site_id: str) -> list[McpTool]:
        site = await self.get_site(site_id)
        if not site.enabled:
            raise McpSiteValidationError("site disabled")
        try:
            probe = await self.client.probe_tools(site)
        except Exception as exc:
            await self.registry.update_probe_status(site_id, McpProbeStatus.FAILED, _safe_error(exc))
            raise McpProbeError(_safe_error(exc)) from exc
        tools = await self._build_tools(site.id, site.name, probe.tools, None)
        saved = await self.registry.replace_site_tools(site.id, tools)
        await self.registry.update_probe_status(site.id, McpProbeStatus.SUCCESS)
        await self.refresh_registered_tool_surface()
        return saved

    async def list_site_tools(self, site_id: str) -> list[McpTool]:
        await self.get_site(site_id)
        return await self.registry.list_tools(site_id)

    async def set_tool_enabled(self, site_id: str, tool_id: str, enabled: bool) -> McpTool:
        await self.get_site(site_id)
        tool = await self.registry.update_tool_enabled(site_id, tool_id, enabled)
        await self.refresh_registered_tool_surface()
        return tool

    async def refresh_registered_tool_surface(self) -> None:
        if self.tool_service is None:
            return
        self.tool_service.set_dynamic_definitions("mcp", await self.list_mcp_tool_definitions())

    async def list_mcp_tool_definitions(self) -> list[ToolDefinition]:
        sites = {site.id: site for site in await self.registry.list_sites() if site.enabled}
        definitions: list[ToolDefinition] = []
        for tool in await self.registry.list_tools(None):
            site = sites.get(tool.site_id)
            if site is None or not tool.enabled:
                continue
            definitions.append(
                ToolDefinition(
                    name=tool.local_name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    risk_level=RiskLevel.SAFE,
                    source_type=ToolSourceType.MCP,
                    toolset=f"mcp:{site.name}",
                )
            )
        return definitions

    async def resolve_tool(self, local_name: str) -> tuple[McpSite, McpTool]:
        for tool in await self.registry.list_tools(None):
            if tool.local_name == local_name:
                site = await self.registry.get_site(tool.site_id)
                if site is None:
                    break
                if not site.enabled or not tool.enabled:
                    raise McpSiteValidationError("tool disabled")
                return site, tool
        raise McpSiteNotFoundError(local_name)

    async def call_tool(self, local_name: str, arguments: dict[str, Any]) -> Any:
        site, tool = await self.resolve_tool(local_name)
        return await self.client.call_tool(site, tool.remote_name, arguments)

    def _validate_site(self, site: McpSite) -> None:
        if not site.name.strip():
            raise McpSiteValidationError("name required")
        if not site.url.strip():
            raise McpSiteValidationError("url required")
        if not site.url.startswith(("http://", "https://")):
            raise McpSiteValidationError("invalid url")

    async def _build_tools(
        self,
        site_id: str,
        site_name: str,
        remote_tools: list[McpRemoteTool],
        include: list[str] | None,
    ) -> list[McpTool]:
        include_set = set(include or [])
        selected = [tool for tool in remote_tools if not include_set or tool.name in include_set]
        used = {definition.name for definition in self.tool_service.list_definitions()} if self.tool_service else set()
        used -= {tool.local_name for tool in await self.registry.list_tools(site_id)}
        result: list[McpTool] = []
        for remote in selected:
            schema = remote.input_schema if isinstance(remote.input_schema, dict) else {"type": "object", "properties": {}}
            local_name = _unique_tool_name(f"mcp_{_slug(site_name)}_{_slug(remote.name)}", used)
            used.add(local_name)
            result.append(
                McpTool(
                    site_id=site_id,
                    remote_name=remote.name,
                    local_name=local_name,
                    description=remote.description,
                    input_schema=schema,
                )
            )
        return result


class McpManagementToolExecutor(ToolExecutor):
    def __init__(self, service: McpService):
        self.service = service

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        start = time.monotonic()
        try:
            content = await self._execute(request)
            return _tool_result(request, ToolResultStatus.SUCCESS, content, start)
        except McpSiteValidationError as exc:
            return _tool_result(request, ToolResultStatus.ERROR, {"error": str(exc)}, start)
        except McpSiteNotFoundError as exc:
            return _tool_result(request, ToolResultStatus.ERROR, {"error": str(exc)}, start)
        except McpProbeError as exc:
            return _tool_result(request, ToolResultStatus.ERROR, {"error": str(exc)}, start)

    async def _execute(self, request: ToolCallRequest) -> dict[str, Any]:
        args = request.arguments
        if request.name == "mcp_site_list":
            sites = await self.service.list_sites()
            return {"sites": [_site_to_dict(site) for site in sites]}
        if request.name == "mcp_site_probe":
            result = await self.service.probe_site(_input_from_args(args))
            return {"tools": [_remote_tool_to_dict(tool) for tool in result.tools]}
        if request.name == "mcp_site_add":
            site = await self.service.create_site_with_probe(
                _input_from_args(args),
                list(args.get("tool_include") or []),
            )
            return {"site": _site_to_dict(site)}
        if request.name == "mcp_site_refresh":
            site_id = str(args.get("site_id") or "")
            if not site_id and args.get("name"):
                site = await self.service.registry.get_site_by_name(str(args["name"]))
                if site is None:
                    raise McpSiteNotFoundError(str(args["name"]))
                site_id = site.id
            tools = await self.service.refresh_site_tools(site_id)
            return {"tools": [_tool_to_dict(tool) for tool in tools]}
        raise McpSiteNotFoundError(request.name)


class McpToolExecutor(ToolExecutor):
    def __init__(self, service: McpService):
        self.service = service

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        start = time.monotonic()
        try:
            content = await self.service.call_tool(request.name, request.arguments)
            return _tool_result(request, ToolResultStatus.SUCCESS, content, start)
        except Exception as exc:
            return _tool_result(request, ToolResultStatus.ERROR, {"error": _safe_error(exc)}, start)


def mcp_management_tool_definitions() -> list[ToolDefinition]:
    base_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "url": {"type": "string"},
            "transport_type": {"type": "string", "enum": ["streamable_http", "sse"]},
        },
        "required": ["name", "url"],
        "additionalProperties": False,
    }
    add_schema = json.loads(json.dumps(base_schema))
    add_schema["properties"]["enabled"] = {"type": "boolean"}
    add_schema["properties"]["tool_include"] = {"type": "array", "items": {"type": "string"}}
    refresh_schema = {
        "type": "object",
        "properties": {"site_id": {"type": "string"}, "name": {"type": "string"}},
        "additionalProperties": False,
    }
    return [
        ToolDefinition("mcp_site_probe", "Probe an MCP site and list remote tools.", base_schema, RiskLevel.CONFIRM, source_type=ToolSourceType.BUILTIN, toolset="mcp-management"),
        ToolDefinition("mcp_site_add", "Add an MCP site after probing its tools.", add_schema, RiskLevel.CONFIRM, source_type=ToolSourceType.BUILTIN, toolset="mcp-management"),
        ToolDefinition("mcp_site_refresh", "Refresh tools for an existing MCP site.", refresh_schema, RiskLevel.CONFIRM, source_type=ToolSourceType.BUILTIN, toolset="mcp-management"),
        ToolDefinition("mcp_site_list", "List configured MCP sites.", {"type": "object", "properties": {}, "additionalProperties": False}, RiskLevel.SAFE, source_type=ToolSourceType.BUILTIN, toolset="mcp-management"),
    ]


def _site_from_input(payload: McpSiteInput, probe_status: McpProbeStatus = McpProbeStatus.NEVER) -> McpSite:
    return McpSite(
        id=str(uuid4()),
        name=payload.name.strip(),
        transport_type=payload.transport_type,
        url=payload.url.strip(),
        enabled=payload.enabled,
        last_probe_status=probe_status,
    )


def _input_from_args(args: dict[str, Any]) -> McpSiteInput:
    return McpSiteInput(
        name=str(args.get("name", "")),
        url=str(args.get("url", "")),
        transport_type=McpTransportType(str(args.get("transport_type") or McpTransportType.STREAMABLE_HTTP.value)),
        enabled=bool(args.get("enabled", True)),
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "tool"


def _unique_tool_name(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    suffix = uuid4().hex[:8]
    return f"{base}_{suffix}"


def _safe_error(exc: Exception) -> str:
    children = getattr(exc, "exceptions", None)
    if children:
        return _safe_error(children[0])
    return str(exc).splitlines()[0][:300]


def _tool_result(request: ToolCallRequest, status: ToolResultStatus, content: Any, start: float) -> ToolResult:
    return ToolResult(request.id, request.name, status, content, int((time.monotonic() - start) * 1000))


def _site_to_dict(site: McpSite) -> dict[str, Any]:
    return {
        "id": site.id,
        "name": site.name,
        "transport_type": site.transport_type.value,
        "url": site.url,
        "enabled": site.enabled,
        "last_probe_status": site.last_probe_status.value,
        "last_probe_error": site.last_probe_error,
        "last_probed_at": site.last_probed_at.isoformat() if site.last_probed_at else None,
        "created_at": site.created_at.isoformat(),
        "updated_at": site.updated_at.isoformat(),
    }


def _tool_to_dict(tool: McpTool) -> dict[str, Any]:
    return {
        "id": tool.id,
        "site_id": tool.site_id,
        "remote_name": tool.remote_name,
        "local_name": tool.local_name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "enabled": tool.enabled,
        "last_seen_at": tool.last_seen_at.isoformat(),
    }


def _remote_tool_to_dict(tool: McpRemoteTool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
