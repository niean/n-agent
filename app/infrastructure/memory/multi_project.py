from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from app.domain.memory_provider import ExternalMemoryProvider
from app.infrastructure.memory.retriever import MemoryRetriever


logger = logging.getLogger(__name__)


# Safe scan patterns for prompt injection
_PROHIBITED_PATTERNS = [
    re.compile(r'ignore\s+previous\s+instructions', re.IGNORECASE),
    re.compile(r'you\s+are\s+now', re.IGNORECASE),
    re.compile(r'do\s+not\s+tell', re.IGNORECASE),
    re.compile(r'do\s+not\s+reveal', re.IGNORECASE),
    re.compile(r'[​-‏﻿]'),  # Zero-width spaces and invisible characters
]

# Entry separator
ENTRY_SEPARATOR = "\n---\n"


class MultiProjectMemory(ExternalMemoryProvider):
    """文件记忆提供者。

    存储结构: {project_root}/{memory_base_path}/{name}/
    - memory.md — 该文件记忆的稳定知识：架构约定、编码规范、常见问题、经验教训
    - user.md — 该文件记忆的用户偏好（沟通风格、工作习惯，可选）

    每个子目录对应一组独立的文件记忆，用户在对话页面勾选启用。
    """

    def __init__(
        self,
        project_root: Path,
        memory_base_path: str = "./locals/external-memory",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
    ):
        self._project_root = project_root
        self._memory_base_path = memory_base_path
        self._memory_char_limit = memory_char_limit
        self._user_char_limit = user_char_limit
        self._base_dir = self._project_root / self._memory_base_path
        self._enabled_projects: set[str] = set()
        self._cache: dict[str, tuple[str, str]] = {}  # project_name -> (memory_content, user_content)

    @property
    def name(self) -> str:
        return "multi-project"

    def is_available(self) -> bool:
        return True

    def list_projects(self) -> list[str]:
        """列出所有可用的文件记忆。排除 builtin，因为它是单独注册的系统记忆。"""
        if not self._base_dir.exists():
            return []
        projects: list[str] = []
        for entry in self._base_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith('.') and entry.name != 'builtin':
                # Check if at least one md file exists
                if (entry / "memory.md").exists() or (entry / "user.md").exists():
                    projects.append(entry.name)
        return sorted(projects)

    def initialize(self, session_id: str, enabled_projects: list[str] | None = None, **kwargs) -> None:
        """Initialize: create base directory if not exists, load enabled projects."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._cache.clear()
        if enabled_projects is not None:
            self._enabled_projects = set(enabled_projects)
        # Preload all enabled projects into cache
        for project_name in self._enabled_projects:
            self._load_project(project_name)

    def set_enabled_projects(self, project_names: list[str]) -> None:
        """Set which projects are enabled for current session."""
        self._enabled_projects = set(project_names)
        # Preload into cache
        for project_name in self._enabled_projects:
            self._load_project(project_name)

    def system_prompt_block(self) -> str:
        """Return combined system prompt from all enabled external memories."""
        blocks: list[str] = []
        for project_name in sorted(self._enabled_projects):
            memory_content, user_content = self._get_cached(project_name)
            if memory_content.strip():
                blocks.append(f"## External Memory: {project_name}\n\n{memory_content.strip()}")
            if user_content.strip():
                blocks.append(f"## User Preferences: {project_name}\n\n{user_content.strip()}")
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    def prefetch(self, query: str, *, session_id: str) -> str:
        """Retrieve relevant entries across enabled projects based on query."""
        if not query or not self._enabled_projects:
            return ""
        retriever = MemoryRetriever(max_results=3, min_score=0.3)
        # Gather (project_name, entry, score) across all enabled projects.
        # Each project contributes its own top-K, then we merge globally.
        cross_project: list[tuple[str, str, float]] = []
        for project_name in sorted(self._enabled_projects):
            memory_content, _ = self._get_cached(project_name)
            entries = self._split_entries(memory_content)
            if not entries:
                continue
            for entry, score in retriever.retrieve(query, entries):
                cross_project.append((project_name, entry, score))
        if not cross_project:
            return ""
        # Global top-K by score
        cross_project.sort(key=lambda triple: triple[2], reverse=True)
        top = cross_project[: retriever.max_results]
        # Group by project, emit single prefix per project
        by_project: dict[str, list[str]] = {}
        order: list[str] = []
        for project_name, entry, _ in top:
            if project_name not in by_project:
                by_project[project_name] = []
                order.append(project_name)
            by_project[project_name].append(entry)
        parts: list[str] = []
        for project_name in order:
            entries_text = "\n---\n".join(by_project[project_name])
            parts.append(f"## Project: {project_name}\n{entries_text}")
        return "\n---\n".join(parts)

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        """No-op for multi-project."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        """Intentional no-op.

        Multi-project memory follows the Hermes holographic pattern: stable
        facts are stored via explicit tool writes (multi_external_memory), not
        auto-synced from each turn. Auto-syncing would also be ambiguous here —
        there is no single "current project" to write to across multiple
        concurrently enabled memories. BuiltinProjectMemory carries the
        per-turn observation persistence for the session; multi-project
        defers all writes to the LLM-driven tool path.
        """
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Expose multi_external_memory tool for LLM to manage memory across external entries."""
        return [
            {
                "name": "multi_external_memory",
                "description": (
                    "Add, replace, or remove stable knowledge in a specific external memory. "
                    "This memory is persistent and can be enabled/disabled per session.\n\n"
                    "**WHEN TO USE:**\n"
                    "- User asks to remember something for a specific external memory\n"
                    "- You discover stable facts about a specific external memory that should persist across sessions\n"
                    "- User corrects your understanding about a specific external memory\n\n"
                    "**Writing is only allowed in interactive primary sessions.**"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Name of the project memory to modify (this is the directory name)",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["add", "replace", "remove"],
                            "description": "Action to perform: add new entry, replace matching entry, remove matching entry",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["memory", "user"],
                            "description": "Target file: memory (project knowledge) or user (user preferences)",
                            "default": "memory",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to add or replace (required for add/replace)",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Substring to match for replace/remove (required for replace/remove)",
                        },
                    },
                    "required": ["project", "action"],
                    "additionalProperties": False,
                },
            }
        ]

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs,
    ) -> str:
        """Handle multi_external_memory tool call."""
        if tool_name != "multi_external_memory":
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})

        agent_context = kwargs.get("agent_context", "unattended")
        if agent_context != "primary":
            return json.dumps({
                "success": False,
                "error": "write permission denied: only interactive primary sessions can modify external memory",
            })

        project = args.get("project")
        if not project or not isinstance(project, str):
            return json.dumps({"success": False, "error": "project name is required"})

        action = args.get("action")
        target = args.get("target", "memory")
        if target not in ["memory", "user"]:
            return json.dumps({"success": False, "error": f"invalid target {target}, must be memory or user"})

        # Validate content based on action
        if action in ["add", "replace"]:
            content = args.get("content")
            if not content or not isinstance(content, str):
                return json.dumps({"success": False, "error": "content is required for add/replace"})
            # Safety scan
            ok, error = self._safe_scan(content)
            if not ok:
                return json.dumps({"success": False, "error": f"security check failed: {error}"})

        if action in ["replace", "remove"]:
            old_text = args.get("old_text")
            if not old_text or not isinstance(old_text, str):
                return json.dumps({"success": False, "error": "old_text is required for replace/remove"})

        project_dir = self._base_dir / project
        project_dir.mkdir(parents=True, exist_ok=True)
        file_path = project_dir / f"{target}.md"

        try:
            if action == "add":
                content = args["content"]
                result = self._add_entry(file_path, content)
            elif action == "replace":
                content = args["content"]
                old_text = args["old_text"]
                result = self._replace_entry(file_path, old_text, content)
            elif action == "remove":
                old_text = args["old_text"]
                result = self._remove_entry(file_path, old_text)
            else:
                return json.dumps({"success": False, "error": f"unknown action {action}"})
        except Exception as exc:
            logger.warning("multi_project_memory tool call failed", exc_info=exc)
            return json.dumps({"success": False, "error": "io error"})

        if result["success"]:
            # Invalidate cache after successful write
            if project in self._cache:
                del self._cache[project]
            self._load_project(project)
            # Check size limit after write
            limit_ok, limit_error = self._check_size_limit(project)
            if not limit_ok:
                result["warning"] = limit_error
            return json.dumps(result)
        else:
            return json.dumps(result)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
    ) -> None:
        """No-op for multi-project."""
        pass

    def on_session_end(self, session_id: str) -> None:
        """No-op for multi-project; interface in place per G5."""
        pass

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """No-op."""
        pass

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str | None:
        """No-op for multi-project; interface in place per G3."""
        return None

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """No-op for multi-project; interface in place per G7."""
        pass

    def shutdown(self) -> None:
        """No-op."""
        pass

    def _load_project(self, project_name: str) -> None:
        """Load project from disk into cache."""
        project_dir = self._base_dir / project_name
        memory_content = self._read_file(project_dir / "memory.md")
        user_content = self._read_file(project_dir / "user.md")
        self._cache[project_name] = (memory_content, user_content)

    def _get_cached(self, project_name: str) -> tuple[str, str]:
        """Get cached content, load if not cached."""
        if project_name not in self._cache:
            self._load_project(project_name)
        return self._cache[project_name]

    def _read_file(self, path: Path) -> str:
        """Read file content."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def _update_file_locked(
        self,
        path: Path,
        update_fn: Callable[[str], tuple[dict[str, Any], str | None]],
    ) -> dict[str, Any]:
        """Run a read-modify-write update while holding an exclusive target-file lock."""
        temp_path = path.with_suffix(".tmp")
        while True:
            with open(path, "a+", encoding="utf-8") as target_file:
                fcntl.flock(target_file, fcntl.LOCK_EX)

                # If another writer replaced the target between open() and flock(),
                # retry on the current target inode so the read is not stale.
                fd_stat = os.fstat(target_file.fileno())
                path_stat = path.stat()
                if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                    # continue exits the with block first, releasing this stale file lock before retrying.
                    continue

                target_file.seek(0)
                current = target_file.read()

                result, new_content = update_fn(current)
                if not result.get("success") or new_content is None:
                    return result

                with open(temp_path, "w", encoding="utf-8") as temp_file:
                    temp_file.write(new_content)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                # Rename (atomic on POSIX) while the target file lock is held.
                os.replace(temp_path, path)
                return result

    def _safe_scan(self, content: str) -> tuple[bool, str]:
        """Scan content for injection patterns and invisible characters."""
        for pattern in _PROHIBITED_PATTERNS:
            match = pattern.search(content)
            if match:
                return False, f"prohibited pattern found: {match.group(0)}"
        return True, ""

    def _check_size_limit(self, project_name: str) -> tuple[bool, str]:
        """Check character limits for this project."""
        memory_content, user_content = self._get_cached(project_name)
        if len(memory_content) > self._memory_char_limit * 2:
            return False, f"{project_name}/memory.md exceeds character limit ({self._memory_char_limit}), consider removing outdated entries"
        if len(user_content) > self._user_char_limit * 2:
            return False, f"{project_name}/user.md exceeds character limit ({self._user_char_limit}), consider removing outdated entries"
        return True, ""

    def _add_entry(self, file_path: Path, content: str) -> dict[str, Any]:
        """Add a new entry."""
        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if current.strip():
                new_content = current.rstrip() + ENTRY_SEPARATOR + content.lstrip()
            else:
                new_content = content
            return {"success": True, "message": f"added entry to {file_path}"}, new_content

        return self._update_file_locked(file_path, update)

    def _replace_entry(self, file_path: Path, old_text: str, new_content: str) -> dict[str, Any]:
        """Replace entry containing old_text."""
        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "old_text not found in target"}, None
            new_content_complete = current.replace(old_text, new_content, 1)
            return {"success": True, "message": f"replaced entry in {file_path}"}, new_content_complete

        return self._update_file_locked(file_path, update)

    def _remove_entry(self, file_path: Path, old_text: str) -> dict[str, Any]:
        """Remove entry containing old_text."""
        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "entry containing old_text not found"}, None

            entries = self._split_entries(current)
            # Remove the first entry that contains old_text
            new_entries: list[str] = []
            removed = False
            for entry in entries:
                if not removed and old_text in entry:
                    removed = True
                    continue
                new_entries.append(entry)

            if not removed:
                return {"success": False, "error": "entry containing old_text not found"}, None

            new_content = ENTRY_SEPARATOR.join(new_entries)
            return {"success": True, "message": f"removed entry from {file_path}"}, new_content

        return self._update_file_locked(file_path, update)

    @staticmethod
    def _split_entries(content: str) -> list[str]:
        """Split content into entries by separator."""
        if not content.strip():
            return []
        entries = content.split(ENTRY_SEPARATOR)
        return [e.strip() for e in entries if e.strip()]
