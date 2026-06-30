import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.domain.tool import ToolCallRequest, ToolResultStatus
from app.infrastructure.tools import builtin as builtin_tools
from app.infrastructure.tools.builtin import build_builtin_tool_executor, safe_eval


class _WebFetchHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/json":
            body = json.dumps({"weather": "sunny"}).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/redirect-internal":
            self.send_response(302)
            self.send_header("location", "http://169.254.169.254/latest/meta-data")
            self.end_headers()
            return
        body = b"hello web"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebFetchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_builtin_safe_tools(tmp_path):
    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    executor = build_builtin_tool_executor(tmp_path)

    now = await executor.execute(ToolCallRequest(id="1", name="get_current_time"))
    calc = await executor.execute(ToolCallRequest(id="2", name="calculator", arguments={"expression": "1 + 2 * 3"}))
    listing = await executor.execute(ToolCallRequest(id="3", name="list_directory", arguments={"path": "."}))
    read = await executor.execute(ToolCallRequest(id="4", name="read_text_file", arguments={"path": "file.txt"}))

    assert now.status == ToolResultStatus.SUCCESS
    assert "T" in now.content["now"]
    assert calc.content["result"] == 7
    assert "file.txt" in listing.content["entries"]
    assert read.content["content"] == "hello"


@pytest.mark.asyncio
async def test_web_fetch_reads_public_text_when_private_urls_allowed(tmp_path, http_server):
    executor = build_builtin_tool_executor(tmp_path, web_fetch_allow_private_urls=True)

    result = await executor.execute(ToolCallRequest(id="1", name="web_fetch", arguments={"url": http_server + "/text"}))

    assert result.status == ToolResultStatus.SUCCESS
    assert result.content["status_code"] == 200
    assert result.content["text"] == "hello web"
    assert result.content["content_type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_web_fetch_parses_json_when_requested(tmp_path, http_server):
    executor = build_builtin_tool_executor(tmp_path, web_fetch_allow_private_urls=True)

    result = await executor.execute(
        ToolCallRequest(id="1", name="web_fetch", arguments={"url": http_server + "/json", "format": "json"})
    )

    assert result.status == ToolResultStatus.SUCCESS
    assert result.content["json"] == {"weather": "sunny"}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://10.0.0.1",
    "http://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/computeMetadata/v1",
    "file:///tmp/demo.txt",
])
async def test_web_fetch_rejects_internal_and_non_http_urls(tmp_path, url: str):
    executor = build_builtin_tool_executor(tmp_path)

    result = await executor.execute(ToolCallRequest(id="1", name="web_fetch", arguments={"url": url}))

    assert result.status == ToolResultStatus.PERMISSION_DENIED


def test_web_fetch_allows_hostname_resolving_to_benchmark_network(monkeypatch: pytest.MonkeyPatch):
    def fake_resolve(hostname: str):
        assert hostname == "wttr.in"
        return [builtin_tools.ipaddress.ip_address("198.18.0.217")]

    monkeypatch.setattr(builtin_tools, "_resolve_hostname", fake_resolve)

    assert builtin_tools._validate_web_fetch_url("https://wttr.in/北京?lang=zh", False) == "https://wttr.in/北京?lang=zh"


@pytest.mark.asyncio
async def test_web_fetch_rejects_direct_benchmark_ip(tmp_path):
    executor = build_builtin_tool_executor(tmp_path)

    result = await executor.execute(ToolCallRequest(id="1", name="web_fetch", arguments={"url": "http://198.18.0.217"}))

    assert result.status == ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_web_fetch_rejects_redirect_to_internal_url(tmp_path, http_server):
    executor = build_builtin_tool_executor(tmp_path, web_fetch_allow_private_urls=True)

    result = await executor.execute(
        ToolCallRequest(id="1", name="web_fetch", arguments={"url": http_server + "/redirect-internal"})
    )

    assert result.status == ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_web_fetch_rejects_invalid_format(tmp_path, http_server):
    executor = build_builtin_tool_executor(tmp_path, web_fetch_allow_private_urls=True)

    result = await executor.execute(
        ToolCallRequest(id="1", name="web_fetch", arguments={"url": http_server, "format": "xml"})
    )

    assert result.status == ToolResultStatus.ERROR
    assert "format" in result.content["error"]


@pytest.mark.asyncio
async def test_web_fetch_limits_response_size(tmp_path, http_server):
    executor = build_builtin_tool_executor(tmp_path, web_fetch_max_bytes=4, web_fetch_allow_private_urls=True)

    result = await executor.execute(ToolCallRequest(id="1", name="web_fetch", arguments={"url": http_server}))

    assert result.status == ToolResultStatus.ERROR
    assert "maximum size" in result.content["error"]


def test_calculator_rejects_unsafe_expressions():
    with pytest.raises(ValueError):
        safe_eval("__import__('os').system('pwd')")

    with pytest.raises(ValueError):
        safe_eval("(1).__class__")


@pytest.mark.asyncio
async def test_file_tools_reject_workspace_escape(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    executor = build_builtin_tool_executor(tmp_path)

    traversal = await executor.execute(ToolCallRequest(id="1", name="read_text_file", arguments={"path": "../outside.txt"}))
    symlink = await executor.execute(ToolCallRequest(id="2", name="read_text_file", arguments={"path": "link.txt"}))

    assert traversal.status == ToolResultStatus.PERMISSION_DENIED
    assert symlink.status == ToolResultStatus.PERMISSION_DENIED
