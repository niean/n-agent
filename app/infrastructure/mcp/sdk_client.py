from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.domain.mcp import McpProbeResult, McpRemoteTool, McpSite, McpTransportType


@dataclass(frozen=True)
class McpClientLimits:
    connect_timeout_seconds: float = 10
    max_tools: int = 50
    max_schema_bytes: int = 65536
    max_result_bytes: int = 262144
    allow_private_hosts: bool = False


class McpUrlValidationError(ValueError):
    pass


class McpSdkClient:
    def __init__(self, limits: McpClientLimits | None = None):
        self.limits = limits or McpClientLimits()

    async def probe_tools(self, site: McpSite) -> McpProbeResult:
        await validate_mcp_url(site.url, allow_private_hosts=self.limits.allow_private_hosts)
        async with asyncio.timeout(self.limits.connect_timeout_seconds):
            if site.transport_type is McpTransportType.SSE:
                tools = await self._probe_sse(site)
            else:
                tools = await self._probe_streamable_http(site)
        return McpProbeResult(tools=tools)

    async def call_tool(self, site: McpSite, remote_name: str, arguments: dict[str, Any]) -> Any:
        await validate_mcp_url(site.url, allow_private_hosts=self.limits.allow_private_hosts)
        async with asyncio.timeout(self.limits.connect_timeout_seconds):
            if site.transport_type is McpTransportType.SSE:
                result = await self._call_sse(site, remote_name, arguments)
            else:
                result = await self._call_streamable_http(site, remote_name, arguments)
        content = _to_jsonable(result)
        if _json_size(content) > self.limits.max_result_bytes:
            return {"error": "mcp result too large"}
        return content

    async def _probe_streamable_http(self, site: McpSite) -> list[McpRemoteTool]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(_connection_url(site.url), timeout=self.limits.connect_timeout_seconds) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.list_tools()
        return self._map_tools(response)

    async def _probe_sse(self, site: McpSite) -> list[McpRemoteTool]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(_connection_url(site.url), timeout=self.limits.connect_timeout_seconds) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.list_tools()
        return self._map_tools(response)

    async def _call_streamable_http(self, site: McpSite, remote_name: str, arguments: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(_connection_url(site.url), timeout=self.limits.connect_timeout_seconds) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(remote_name, arguments)

    async def _call_sse(self, site: McpSite, remote_name: str, arguments: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(_connection_url(site.url), timeout=self.limits.connect_timeout_seconds) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(remote_name, arguments)

    def _map_tools(self, response: Any) -> list[McpRemoteTool]:
        raw_tools = list(getattr(response, "tools", response) or [])[: self.limits.max_tools]
        tools: list[McpRemoteTool] = []
        for item in raw_tools:
            name = str(getattr(item, "name", ""))
            if not name:
                continue
            description = str(getattr(item, "description", "") or "")
            schema = getattr(item, "inputSchema", None) or getattr(item, "input_schema", None) or {"type": "object", "properties": {}}
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            if _json_size(schema) > self.limits.max_schema_bytes:
                continue
            tools.append(McpRemoteTool(name=name, description=description, input_schema=schema))
        return tools


async def validate_mcp_url(url: str, allow_private_hosts: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise McpUrlValidationError("invalid scheme")
    if parsed.username or parsed.password:
        raise McpUrlValidationError("credentials not allowed")
    if not parsed.hostname:
        raise McpUrlValidationError("host required")
    await _resolve_public_ips(parsed.hostname, allow_private_hosts=allow_private_hosts)
    return url


async def _resolve_public_ips(hostname: str, allow_private_hosts: bool = False) -> list[str]:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise McpUrlValidationError("host resolution failed") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise McpUrlValidationError("host resolution failed")
    for raw_ip in ips:
        ip = ipaddress.ip_address(raw_ip)
        if raw_ip in {"169.254.169.254", "100.100.100.200"}:
            raise McpUrlValidationError("host not allowed")
        if ip.is_multicast or ip.is_unspecified:
            raise McpUrlValidationError("host not allowed")
        if not allow_private_hosts and (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise McpUrlValidationError("host not allowed")
    return ips


def _connection_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise McpUrlValidationError("credentials not allowed")
    return urlunparse(parsed)


def _json_size(value: Any) -> int:
    return len(json.dumps(_to_jsonable(value), ensure_ascii=False).encode("utf-8"))


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value))
    return str(value)
