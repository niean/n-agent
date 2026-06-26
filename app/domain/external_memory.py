from __future__ import annotations

from typing import Protocol


class ExternalMemoryConfigRegistry(Protocol):
    """全局外置记忆激活配置持久化端口."""

    def get_enabled(self) -> set[str] | None:
        """返回当前保存的激活提供者集合.

        Returns:
            None = 没有用户保存配置，需要上层 fallback 到 Settings
            set[str] = 明确配置，空set表示禁用所有提供者
        """
        ...

    def set_enabled(self, provider_names: list[str]) -> None:
        """保存新的激活提供者集合."""
        ...

    def create_tables(self) -> None:
        """初始化 schema，如果不存在."""
        ...
