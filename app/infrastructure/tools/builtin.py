from __future__ import annotations

import ast
import ipaddress
import json
import operator
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolExecutor, ToolResult, ToolResultStatus


BUILTIN_TOOL_NAMES = frozenset({"get_current_time", "calculator", "list_directory", "read_text_file", "web_fetch"})


_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (ipaddress.ip_network("169.254.0.0/16"),)
_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class BuiltinToolExecutor:
    def __init__(
        self,
        workspace_root: Path,
        web_fetch_timeout_seconds: float = 10,
        web_fetch_max_bytes: int = 131072,
        web_fetch_allow_private_urls: bool = False,
    ):
        self.workspace_root = workspace_root.resolve()
        self.web_fetch_timeout_seconds = web_fetch_timeout_seconds
        self.web_fetch_max_bytes = web_fetch_max_bytes
        self.web_fetch_allow_private_urls = web_fetch_allow_private_urls

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        start = time.monotonic()
        try:
            content = self._execute(request)
            status = ToolResultStatus.SUCCESS
        except PermissionError as exc:
            content = {"error": str(exc)}
            status = ToolResultStatus.PERMISSION_DENIED
        except Exception as exc:
            content = {"error": str(exc)}
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=content,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _execute(self, request: ToolCallRequest) -> Any:
        if request.name == "get_current_time":
            return {"now": datetime.now(timezone.utc).isoformat()}
        if request.name == "calculator":
            return {"result": safe_eval(str(request.arguments.get("expression", "")))}
        if request.name == "list_directory":
            path = self._safe_path(str(request.arguments.get("path", ".")))
            if not path.is_dir():
                raise ValueError("path is not a directory")
            return {"entries": sorted(child.name for child in path.iterdir())}
        if request.name == "read_text_file":
            path = self._safe_path(str(request.arguments.get("path", "")))
            if not path.is_file():
                raise ValueError("path is not a file")
            return {"content": path.read_text(encoding="utf-8")}
        if request.name == "web_fetch":
            return self._web_fetch(
                str(request.arguments.get("url", "")),
                str(request.arguments.get("format") or "text"),
            )
        raise ValueError(f"unknown tool: {request.name}")

    def _safe_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        candidate = path if path.is_absolute() else self.workspace_root / path
        resolved = candidate.resolve()
        if resolved != self.workspace_root and not resolved.is_relative_to(self.workspace_root):
            raise PermissionError("path outside workspace")
        return resolved

    def _web_fetch(self, url: str, output_format: str) -> dict[str, Any]:
        if output_format not in {"text", "json"}:
            raise ValueError("format must be text or json")
        safe_url = _validate_web_fetch_url(url, self.web_fetch_allow_private_urls)
        request = urllib.request.Request(safe_url, headers={"User-Agent": "N-Agent web_fetch"}, method="GET")
        opener = urllib.request.build_opener(_SafeRedirectHandler(self.web_fetch_allow_private_urls))
        try:
            with opener.open(request, timeout=self.web_fetch_timeout_seconds) as response:
                final_url = response.geturl()
                _validate_web_fetch_url(final_url, self.web_fetch_allow_private_urls)
                data = _read_limited(response, self.web_fetch_max_bytes)
                content_type = response.headers.get("content-type", "")
                text = data.decode(_charset_from_content_type(content_type), errors="replace")
                result: dict[str, Any] = {
                    "url": safe_url,
                    "final_url": final_url,
                    "status_code": response.status,
                    "content_type": content_type,
                }
                if output_format == "json":
                    result["json"] = json.loads(text)
                else:
                    result["text"] = text
                return result
        except urllib.error.HTTPError as exc:
            raise ValueError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"request failed: {exc.reason}") from exc


def build_builtin_tool_executor(
    workspace_root: Path,
    web_fetch_timeout_seconds: float = 10,
    web_fetch_max_bytes: int = 131072,
    web_fetch_allow_private_urls: bool = False,
) -> ToolExecutor:
    return BuiltinToolExecutor(
        workspace_root,
        web_fetch_timeout_seconds=web_fetch_timeout_seconds,
        web_fetch_max_bytes=web_fetch_max_bytes,
        web_fetch_allow_private_urls=web_fetch_allow_private_urls,
    )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_private_urls: bool):
        self.allow_private_urls = allow_private_urls

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_web_fetch_url(newurl, self.allow_private_urls)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_web_fetch_url(url: str, allow_private_urls: bool) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in {"http", "https"}:
        raise PermissionError("only http and https URLs are allowed")
    if not hostname:
        raise PermissionError("URL hostname is required")
    if hostname in _BLOCKED_HOSTNAMES:
        raise PermissionError("URL targets an internal metadata hostname")
    _validate_hostname_addresses(hostname, allow_private_urls)
    return urllib.parse.urlunparse(parsed)


def _validate_hostname_addresses(hostname: str, allow_private_urls: bool) -> None:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else _resolve_hostname(hostname)
    for address in addresses:
        if address in _ALWAYS_BLOCKED_IPS or any(address in network for network in _ALWAYS_BLOCKED_NETWORKS):
            raise PermissionError("URL targets a cloud metadata address")
        if not allow_private_urls and _is_blocked_ip(address, allow_benchmark=literal is None):
            raise PermissionError("URL targets a private or internal network address")


def _resolve_hostname(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise PermissionError("URL hostname could not be resolved") from exc
    addresses = []
    for _family, _, _, _, sockaddr in addr_info:
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise PermissionError("URL hostname could not be resolved")
    return addresses


def _is_blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_benchmark: bool = False) -> bool:
    if allow_benchmark and address in _BENCHMARK_NETWORK:
        return False
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        return True
    if address.is_multicast or address.is_unspecified:
        return True
    return address in _CGNAT_NETWORK


def _read_limited(response: Any, max_bytes: int) -> bytes:
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response exceeded maximum size")
    return data


def _charset_from_content_type(content_type: str) -> str:
    for part in content_type.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.lower() == "charset" and value:
            return value.strip()
    return "utf-8"


def safe_eval(expression: str) -> int | float:
    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def evaluate(node: ast.AST) -> int | float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.left), evaluate(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.operand))
        raise ValueError("unsupported expression")

    return evaluate(ast.parse(expression, mode="eval"))
