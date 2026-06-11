import pytest

from app.domain.tool import ToolCallRequest, ToolResultStatus
from app.infrastructure.tools.builtin import build_builtin_tool_executor, safe_eval


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
