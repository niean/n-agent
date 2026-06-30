"""Sandbox callback tools — invoked by sandboxed code via UDS RPC.

Each tool runs in the parent (trusted) process and operates on
`SandboxCallbackContext.workspace_root` (read-only boundary enforced by
Docker `-v /workspace:ro`; Local trusts the dev host).

Security:
- read_file/search_files: refuse paths escaping workspace_root (resolve + is_relative_to)
- write_file/patch: refuse paths escaping workspace_root
- web_extract/web_search: delegate to providers, no local fs writes
- terminal: NOT exposed by default (capability out of scope for first cut;
  enabled flag defaults False and tool is not registered unless explicitly configured)

Tools are registered disabled by default; `enabled` is set by the registry
based on `Settings.sandbox_callback_tools` and feature flags.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.sandbox import SandboxCallbackContext, SandboxCallbackTool, SearchResult


_MAX_READ_BYTES = 200_000
_MAX_LIST_ENTRIES = 500
_MAX_PATCH_BYTES = 200_000


def _resolve_under_workspace(workspace_root: Path, target: str) -> Path:
    """Resolve `target` under `workspace_root`; refuse escapes.

    Absolute paths are anchored at workspace_root (so `/etc/passwd` becomes
    `<workspace>/etc/passwd`, which doesn't exist — fail-closed). Relative
    paths are resolved against workspace_root. Symlinks are NOT followed
    past the workspace boundary (resolved path must stay under workspace).
    """
    workspace_root = workspace_root.resolve()
    candidate = (workspace_root / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace_root: {target}") from exc
    return candidate


@dataclass
class _BaseCallbackTool:
    name: str
    enabled: bool = False


class ReadFileTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "read_file"
    enabled = False

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        path_arg = str(arguments.get("path", ""))
        if not path_arg:
            return {"status": "error", "error": "path required"}
        try:
            resolved = _resolve_under_workspace(context.workspace_root, path_arg)
        except PermissionError as exc:
            return {"status": "error", "error": str(exc)}
        if not resolved.exists() or not resolved.is_file():
            return {"status": "error", "error": f"file not found: {path_arg}"}
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            return {"status": "error", "error": f"read failed: {exc}"}
        if len(data) > _MAX_READ_BYTES:
            data = data[:_MAX_READ_BYTES]
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            return {"status": "error", "error": f"decode failed: {exc}"}
        return {"status": "ok", "content": text, "bytes": len(data), "path": str(resolved)}


class WriteFileTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "write_file"
    enabled = False

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        path_arg = str(arguments.get("path", ""))
        content = str(arguments.get("content", ""))
        if not path_arg:
            return {"status": "error", "error": "path required"}
        try:
            resolved = _resolve_under_workspace(context.workspace_root, path_arg)
        except PermissionError as exc:
            return {"status": "error", "error": str(exc)}
        data = content.encode("utf-8")
        if len(data) > _MAX_PATCH_BYTES:
            return {"status": "error", "error": "content too large"}
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
        except OSError as exc:
            return {"status": "error", "error": f"write failed: {exc}"}
        return {"status": "ok", "bytes": len(data), "path": str(resolved)}


class SearchFilesTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "search_files"
    enabled = False

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            return {"status": "error", "error": "pattern required"}
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"status": "error", "error": f"invalid regex: {exc}"}
        root = context.workspace_root.resolve()
        matches: list[str] = []
        try:
            for entry in root.rglob("*"):
                if entry.is_file() and regex.search(entry.name):
                    matches.append(str(entry.relative_to(root)))
                    if len(matches) >= _MAX_LIST_ENTRIES:
                        break
        except OSError as exc:
            return {"status": "error", "error": f"search failed: {exc}"}
        return {"status": "ok", "matches": matches, "count": len(matches)}


class PatchTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "patch"
    enabled = False

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        path_arg = str(arguments.get("path", ""))
        old = str(arguments.get("old", ""))
        new = str(arguments.get("new", ""))
        if not path_arg:
            return {"status": "error", "error": "path required"}
        try:
            resolved = _resolve_under_workspace(context.workspace_root, path_arg)
        except PermissionError as exc:
            return {"status": "error", "error": str(exc)}
        if not resolved.exists() or not resolved.is_file():
            return {"status": "error", "error": f"file not found: {path_arg}"}
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"status": "error", "error": f"read failed: {exc}"}
        count = text.count(old)
        if count == 0:
            return {"status": "ok", "replacements": 0, "note": "no matches"}
        new_text = text.replace(old, new)
        if len(new_text.encode("utf-8")) > _MAX_PATCH_BYTES:
            return {"status": "error", "error": "result too large"}
        try:
            resolved.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            return {"status": "error", "error": f"write failed: {exc}"}
        return {"status": "ok", "replacements": count}


class WebExtractTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "web_extract"
    enabled = False

    def __init__(self, fetcher: "WebFetcher | None" = None) -> None:
        self._fetcher = fetcher

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        url = str(arguments.get("url", ""))
        if not url:
            return {"status": "error", "error": "url required"}
        if not (url.startswith("http://") or url.startswith("https://")):
            return {"status": "error", "error": "only http(s) URLs allowed"}
        if self._fetcher is None:
            return {"status": "error", "error": "web_extract not configured"}
        return await self._fetcher.fetch(url)


class WebSearchTool(_BaseCallbackTool, SandboxCallbackTool):
    name = "web_search"
    enabled = False

    def __init__(self, provider: "SearchProviderLike | None" = None) -> None:
        self._provider = provider

    async def call(self, arguments: dict, context: SandboxCallbackContext) -> dict:
        query = str(arguments.get("query", ""))
        top_k = int(arguments.get("top_k", 5))
        if not query:
            return {"status": "error", "error": "query required"}
        if self._provider is None or not self._provider.is_available():
            return {"status": "error", "error": "search provider unavailable"}
        try:
            results = await self._provider.search(query, top_k)
        except Exception as exc:
            return {"status": "error", "error": f"search failed: {exc}"}
        return {
            "status": "ok",
            "results": [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results],
        }


# Protocol-like shims for type hints (avoid runtime import cycles)
class WebFetcher:  # pragma: no cover — protocol
    async def fetch(self, url: str) -> dict: ...


class SearchProviderLike:  # pragma: no cover — protocol
    def is_available(self) -> bool: ...
    async def search(self, query: str, top_k: int) -> list[SearchResult]: ...
