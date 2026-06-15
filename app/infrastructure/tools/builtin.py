from __future__ import annotations

import ast
import operator
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolExecutor, ToolResult, ToolResultStatus


BUILTIN_TOOL_NAMES = frozenset({"get_current_time", "calculator", "list_directory", "read_text_file"})


class BuiltinToolExecutor:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

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
        raise ValueError(f"unknown tool: {request.name}")

    def _safe_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        candidate = path if path.is_absolute() else self.workspace_root / path
        resolved = candidate.resolve()
        if resolved != self.workspace_root and not resolved.is_relative_to(self.workspace_root):
            raise PermissionError("path outside workspace")
        return resolved


def build_builtin_tool_executor(workspace_root: Path) -> ToolExecutor:
    return BuiltinToolExecutor(workspace_root)


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
