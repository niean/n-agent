from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from app.domain.memory_provider import ExternalMemoryProvider


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


class BuiltinProjectMemory(ExternalMemoryProvider):
    """内置外置记忆文本文件存储。

    存储位置: {project_root}/{memory_path}/
    - memory.md — 外置知识：架构约定、编码规范、常见问题、经验教训
    - user.md — 用户偏好（这个外置记忆下的沟通风格、工作习惯，可选）
    """

    def __init__(
        self,
        project_root: Path,
        memory_path: str = "./locals/external-memory",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
    ):
        self._project_root = project_root
        self._memory_path = memory_path
        self._memory_char_limit = memory_char_limit
        self._user_char_limit = user_char_limit
        self._memory_dir = self._project_root / self._memory_path
        resolved_memory_dir = self._memory_dir.resolve()
        resolved_project_root = self._project_root.resolve()
        if not str(resolved_memory_dir).startswith(str(resolved_project_root) + os.path.sep):
            raise ValueError("memory_path must stay within project_root")
        self._memory_dir = resolved_memory_dir
        self._memory_file = self._memory_dir / "memory.md"
        self._user_file = self._memory_dir / "user.md"
        self._memory_content: str = ""
        self._user_content: str = ""

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize: create directory if not exists, load snapshot."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        if self._memory_file.exists():
            self._memory_content = self._read_file(self._memory_file)
        if self._user_file.exists():
            self._user_content = self._read_file(self._user_file)

    def system_prompt_block(self) -> str:
        """Return frozen snapshot of stable external memory for system prompt."""
        blocks: list[str] = []
        if self._memory_content.strip():
            blocks.append("## External Stable Memory\n\n" + self._memory_content.strip())
        if self._user_content.strip():
            blocks.append("## User Preferences (this external memory)\n\n" + self._user_content.strip())
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    def prefetch(self, query: str, *, session_id: str) -> str:
        """Builtin uses frozen snapshot in system prompt, no dynamic prefetch."""
        return ""

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        """No-op for builtin."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        """Builtin doesn't sync full turns, only explicit tool writes."""
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Expose external_memory tool for LLM to manage memory."""
        return [
            {
                "name": "external_memory",
                "description": (
                    "Add, replace, or remove stable external knowledge in persistent memory. "
                    "This memory is available across all sessions. "
                    "\n\n**WHEN TO USE (do this proactively, don't wait to be asked):**\n"
                    "- User corrects you or says \"remember this\" / \"don't do that again\"\n"
                    "- User shares conventions, preferences, or workflow habits\n"
                    "- You discover something about the environment (OS, installed tools, structure)\n"
                    "- You learn a convention, API quirk, or recurring solution\n"
                    "- You identify a stable fact that will be useful again in *future sessions*\n"
                    "\n**Writing is only allowed in interactive (primary) sessions. "
                    "Unattended/cron jobs will be rejected.**"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
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
                    "required": ["action"],
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
        """Handle external_memory tool call."""
        if tool_name != "external_memory":
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})

        agent_context = kwargs.get("agent_context", "unattended")
        if agent_context != "primary":
            return json.dumps({
                "success": False,
                "error": "write permission denied: only interactive primary sessions can modify external memory",
            })

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

        try:
            if action == "add":
                content = args["content"]
                result = self._add_entry(target, content)
            elif action == "replace":
                content = args["content"]
                old_text = args["old_text"]
                result = self._replace_entry(target, old_text, content)
            elif action == "remove":
                old_text = args["old_text"]
                result = self._remove_entry(target, old_text)
            else:
                return json.dumps({"success": False, "error": f"unknown action {action}"})
        except Exception as exc:
            logger.warning("project_memory tool call failed", exc_info=exc)
            return json.dumps({"success": False, "error": "io error"})

        if result["success"]:
            # Reload snapshot after successful write
            self._reload_snapshot()
            # Check size limit after write
            limit_ok, limit_error = self._check_size_limit()
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
        """No-op for builtin."""
        pass

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """No-op for builtin."""
        pass

    def shutdown(self) -> None:
        """No-op for builtin."""
        pass

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

    def _check_size_limit(self) -> tuple[bool, str]:
        """Check character limits."""
        if len(self._memory_content) > self._memory_char_limit * 2:
            return False, f"memory.md exceeds character limit ({self._memory_char_limit}), consider removing outdated entries"
        if len(self._user_content) > self._user_char_limit * 2:
            return False, f"user.md exceeds character limit ({self._user_char_limit}), consider removing outdated entries"
        return True, ""

    def _reload_snapshot(self) -> None:
        """Reload snapshot after write."""
        self._memory_content = self._read_file(self._memory_file)
        self._user_content = self._read_file(self._user_file)

    def _add_entry(self, target: str, content: str) -> dict[str, Any]:
        """Add a new entry."""
        file_path = self._memory_file if target == "memory" else self._user_file

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if current.strip():
                new_content = current.rstrip() + ENTRY_SEPARATOR + content.lstrip()
            else:
                new_content = content
            return {"success": True, "message": f"added entry to {target}"}, new_content

        return self._update_file_locked(file_path, update)

    def _replace_entry(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Replace entry containing old_text."""
        file_path = self._memory_file if target == "memory" else self._user_file

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "old_text not found in target"}, None
            new_content_complete = current.replace(old_text, new_content, 1)
            return {"success": True, "message": f"replaced entry in {target}"}, new_content_complete

        return self._update_file_locked(file_path, update)

    def _remove_entry(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove entry containing old_text."""
        file_path = self._memory_file if target == "memory" else self._user_file

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "old_text not found in target"}, None

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
            return {"success": True, "message": f"removed entry from {target}"}, new_content

        return self._update_file_locked(file_path, update)

    @staticmethod
    def _split_entries(content: str) -> list[str]:
        """Split content into entries by separator."""
        if not content.strip():
            return []
        entries = content.split(ENTRY_SEPARATOR)
        return [e.strip() for e in entries if e.strip()]
