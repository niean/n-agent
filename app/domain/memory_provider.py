from __future__ import annotations

from typing import Any, Protocol


class ExternalMemoryProvider(Protocol):
    """外部记忆提供者抽象基类。

    生命周期：
    - initialize() — 启动初始化，连接、创建资源
    - prefetch() / sync_turn() — 每轮对话调用
    - shutdown() — 优雅关闭
    """

    @property
    def name(self) -> str:
        """提供者短标识，例如 "builtin", "honcho", "mem0"."""
        ...

    def is_available(self) -> bool:
        """返回 True 表示此提供者已配置就绪可使用。

        仅做本地配置检查，不做网络调用。
        """
        ...

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """初始化提供者。

        kwargs 总是包含：
        - project_root: 项目根目录路径，用于项目级存储隔离
        - platform: "cli", "feishu", "openai" 等

        kwargs 可能包含：
        - agent_context: "primary", "subagent", "cron", "unattended" — 决定是否允许写入
          subagent 隐含 skip_memory=True：不注入 system_prompt_block、不调用 prefetch、
          不调用 sync_turn、工具调用一律拒绝写入。当前 N-Agent 多 Agent 编排尚未落地，
          subagent 读取路径的跳过逻辑待编排层接入时实现；写入闸门已由
          ``agent_context != "primary"`` 守护。
        - agent_identity: 配置名，用于多profile隔离
        - user_id: 平台用户标识（gateway 会话）
        """
        ...

    def system_prompt_block(self) -> str:
        """返回静态文本注入 system prompt。

        系统记忆提供者在这里输出 frozen snapshot 稳定知识。
        外部记忆提供者在这里输出使用说明。
        返回空字符串跳过。
        """
        ...

    def prefetch(self, query: str, *, session_id: str) -> str:
        """基于当前用户 query 召回相关上下文。

        在每轮 LLM 调用前调用。返回格式化文本，直接注入上下文。
        返回空字符串表示无相关内容。

        实现应快速返回，耗时召回应使用 queue_prefetch 后台预处理。

        返回内容会被包装进 <memory-context> 围栏，provider 不需要自己加。
        """
        ...

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        """排队后台预召，供下一轮使用。

        每轮结束后调用，默认 no-op。
        """
        ...

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        """持久化完整的一轮对话。

        只在完整回合结束（finalize）调用，应非阻塞后台处理。
        只有 agent_context=primary 才会调用。
        """
        ...

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """返回此提供者暴露给 LLM 的工具 schemas（OpenAI 格式）。

        每个 schema 会被包装为 N-Agent ToolDefinition。
        空列表表示无工具。内置提供者 external_memory 在这里暴露。
        """
        ...

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """处理 LLM 工具调用，返回 JSON 字符串结果。

        返回值必须是可反序列化为对象的 JSON 字符串，且包含
        ``{"success": bool, ...}`` 字段；调用方据此判断工具执行成功或失败。

        kwargs 包含：
        - agent_context: str 上下文，决定是否允许写入
        - session_id: str 当前会话 id
        """
        ...

    def shutdown(self) -> None:
        """关闭清理资源。"""
        ...

    # --- 可选钩子 ---

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
    ) -> None:
        """会话切换通知。

        Fires on /resume, /branch, /new，提供者需要更新缓存的会话状态。
        """
        pass

    def on_session_end(self, session_id: str) -> None:
        """会话结束通知。

        在会话被删除前调用，提供者可做会话级清理、摘要落盘、上下文交接。
        """
        pass

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """系统记忆写入通知，外部记忆提供者可镜像写入。"""
        pass

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str | None:
        """压缩前抢救钩子。

        在 HeuristicSummarizer 触发压缩前调用，入参为待压缩的消息列表。
        provider 可从中提取要点返回字符串，返回值会回填到 summary 作为补充上下文。
        返回 None 或空字符串表示不抢救。
        """
        return None

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """子 Agent 完成回调（父侧观察）。

        子 Agent 完成任务后，在父会话的 provider 上调用，把子任务 prompt 与
        子 Agent 最终回复作为观察交给父会话。子 Agent 自身不持有 provider 会话
        （skip_memory=True），其工作产出通过此钩子回流到父会话。

        事件源待定：依赖 N-Agent 多 Agent 编排落地。当前为接口占位，provider
        可空实现。
        """
        pass
