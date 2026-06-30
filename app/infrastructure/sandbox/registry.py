from __future__ import annotations

from app.domain.sandbox import SandboxCallbackTool, SandboxCallbackToolRegistry


class InMemorySandboxCallbackToolRegistry(SandboxCallbackToolRegistry):
    """In-memory registry; tool enabled flags are set at registration time
    based on Settings.sandbox_callback_tools and feature flags (T24 assembly).
    """

    def __init__(self) -> None:
        self._tools: dict[str, SandboxCallbackTool] = {}

    def register(self, tool: SandboxCallbackTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> SandboxCallbackTool | None:
        return self._tools.get(name)

    def list_enabled(self) -> list[SandboxCallbackTool]:
        return [t for t in self._tools.values() if t.enabled]

    def list_all(self) -> list[SandboxCallbackTool]:
        return list(self._tools.values())
