from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from app.domain.external_memory import ExternalMemoryConfigRegistry
from app.application.external_memory_manager import ExternalMemoryManager


class ExternalMemoryService:
    def __init__(
        self,
        external_memory_manager: ExternalMemoryManager,
        config_registry: ExternalMemoryConfigRegistry,
        settings_default: list[str] | None,
        base_dir: Path,
    ) -> None:
        self._manager = external_memory_manager
        self._config_registry = config_registry
        self._settings_default = settings_default
        self._base_dir = base_dir
        self._initialize()

    def _initialize(self) -> None:
        saved = self._config_registry.get_enabled()
        if saved is not None:
            self._manager.set_global_enabled(list(saved))
        else:
            if self._settings_default is not None:
                self._manager.set_global_enabled(self._settings_default)
            # else: settings_default is None → keep _enabled_providers = None (all enabled)

    def list_providers(self) -> list[dict]:
        providers = self._manager.list_providers()
        # Add description preview for each project
        for p in providers:
            # All providers (including builtin) have memory.md
            content = self.get_external_memory(p["name"], "memory")
            preview = content.strip()
            if len(preview) > 256:
                preview = preview[:256] + "..."
            p["description"] = preview
        return providers

    def save_global_enabled(self, provider_names: list[str]) -> None:
        self._config_registry.set_enabled(provider_names)
        self._manager.set_global_enabled(provider_names)

    def create_project(self, name: str) -> bool:
        """Create a new empty project directory."""
        project_dir = self._base_dir / name
        if project_dir.exists():
            return False
        try:
            project_dir.mkdir(parents=True, exist_ok=False)
            # Create empty memory.md
            (project_dir / "memory.md").write_text("", encoding="utf-8")
            return True
        except OSError:
            return False

    def delete_project(self, name: str) -> bool:
        """Delete an existing project directory."""
        # Double-check name pattern for safety
        if not re.match(r'^[A-Za-z0-9_-]+$', name):
            return False
        project_dir = self._base_dir / name
        if not project_dir.exists() or not project_dir.is_dir():
            return False
        # Security check: must be under base directory
        try:
            project_dir = project_dir.resolve()
            base_dir = self._base_dir.resolve()
            if not str(project_dir).startswith(str(base_dir)):
                return False
        except OSError:
            return False
        try:
            shutil.rmtree(project_dir)
            return True
        except OSError:
            return False

    def _validate_project_name(self, name: str) -> bool:
        """Validate project name pattern."""
        return bool(re.match(r'^[A-Za-z0-9_-]+$', name))

    def _safe_project_dir(self, name: str) -> Path | None:
        """Get safe project directory path."""
        if not self._validate_project_name(name):
            return None
        project_dir = self._base_dir / name
        try:
            project_dir = project_dir.resolve()
            base_dir = self._base_dir.resolve()
            if not str(project_dir).startswith(str(base_dir)):
                return None
            return project_dir
        except OSError:
            return None

    def get_external_memory(self, project_name: str, target: str = "memory") -> str:
        """Get memory content for an external memory entry."""
        if target not in ["memory", "user"]:
            return ""
        project_dir = self._safe_project_dir(project_name)
        if not project_dir or not project_dir.exists():
            return ""
        file_path = project_dir / f"{target}.md"
        try:
            return file_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save_external_memory(self, project_name: str, content: str, target: str = "memory") -> bool:
        """Save memory content for an external memory entry."""
        if target not in ["memory", "user"]:
            return False
        project_dir = self._safe_project_dir(project_name)
        if not project_dir:
            return False
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
            file_path = project_dir / f"{target}.md"
            file_path.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False

    def list_project_entries(self, project_name: str, target: str = "memory") -> list[str]:
        """List entries in an external memory file."""
        content = self.get_external_memory(project_name, target)
        if not content.strip():
            return []
        entries = content.split("\n---\n")
        return [e.strip() for e in entries if e.strip()]

    def add_project_entry(self, project_name: str, content: str, target: str = "memory") -> bool:
        """Add an entry to an external memory file."""
        if target not in ["memory", "user"]:
            return False
        current = self.get_external_memory(project_name, target)
        if current.strip():
            new_content = current.rstrip() + "\n---\n" + content.lstrip()
        else:
            new_content = content
        return self.save_external_memory(project_name, new_content, target)

    def delete_project_entry(self, project_name: str, entry_index: int, target: str = "memory") -> bool:
        """Delete an entry from an external memory file by index."""
        if target not in ["memory", "user"]:
            return False
        entries = self.list_project_entries(project_name, target)
        if entry_index < 0 or entry_index >= len(entries):
            return False
        entries.pop(entry_index)
        new_content = "\n---\n".join(entries)
        return self.save_external_memory(project_name, new_content, target)

    def update_project_entry(self, project_name: str, entry_index: int, new_content: str, target: str = "memory") -> bool:
        """Update an entry in an external memory file by index."""
        if target not in ["memory", "user"]:
            return False
        entries = self.list_project_entries(project_name, target)
        if entry_index < 0 or entry_index >= len(entries):
            return False
        entries[entry_index] = new_content
        new_content = "\n---\n".join(entries)
        return self.save_external_memory(project_name, new_content, target)
