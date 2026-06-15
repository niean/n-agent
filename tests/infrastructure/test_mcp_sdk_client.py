import socket

import pytest

from app.domain.mcp import McpSite
from app.infrastructure.mcp.sdk_client import McpClientLimits, McpSdkClient, McpUrlValidationError, validate_mcp_url


@pytest.mark.asyncio
async def test_validate_mcp_url_rejects_credentials_and_private_hosts(monkeypatch):
    with pytest.raises(McpUrlValidationError):
        await validate_mcp_url("https://user:pass@example.com/mcp")

    def fake_private(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_private)
    with pytest.raises(McpUrlValidationError):
        await validate_mcp_url("https://example.com/mcp")
    assert await validate_mcp_url("https://example.com/mcp", allow_private_hosts=True) == "https://example.com/mcp"


@pytest.mark.asyncio
async def test_validate_mcp_url_still_rejects_metadata_when_private_hosts_allowed(monkeypatch):
    def fake_metadata(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_metadata)
    with pytest.raises(McpUrlValidationError):
        await validate_mcp_url("https://example.com/mcp", allow_private_hosts=True)


@pytest.mark.asyncio
async def test_validate_mcp_url_accepts_public_hosts(monkeypatch):
    def fake_public(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_public)
    assert await validate_mcp_url("https://example.com/mcp") == "https://example.com/mcp"


def test_mcp_client_limits_are_configurable():
    client = McpSdkClient(
        McpClientLimits(
            connect_timeout_seconds=1,
            max_tools=2,
            max_schema_bytes=1024,
            max_result_bytes=2048,
            allow_private_hosts=True,
        )
    )

    assert client.limits.max_tools == 2
    assert client.limits.allow_private_hosts is True
