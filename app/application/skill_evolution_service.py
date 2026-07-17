from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.application.chat_service import ChatCompletionInput

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundReviewResult:
    """Forked background review 的结构化结果，供 Curator consolidation 报告消费。

    final_text 为 assistant 最终文本；tool_calls 为本轮产生的 skill_manage 调用
    （{name, arguments}）；error 非空表示 fork 失败/超时。best-effort：仅收集
    final message 携带的 tool_calls，中间轮 tool_calls 未收集时分类退化为 pruned。
    """

    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    error: str | None = None

_SKILL_REVIEW_PROMPT = """\
你是一个 Skill 自进化审查 Agent。你将在后台审查对话摘要，识别值得持久化为 Skill 的非平凡工作流。

可用工具:
- skill_view: 读取现有 Skill 内容（修改前必须先读）
- skill_manage: 创建新 Skill (action=create) 或修补现有 Skill (action=patch)

审查准则:
1. 仅当对话摘要中体现出可复用、非平凡的工作流时才创建/修补 Skill
2. 修改现有 skill 前必须先调用 skill_view 读取当前内容，避免覆盖
3. 只修改 agent 自有的 skill，禁止修改 seed/pinned skill
4. 新建 skill 时提供清晰的 name、description 和 content
5. 如果对话摘要没有值得持久化的工作流，不做任何修改

Skill 格式规范（创建/修改 Skill 时必须遵循 Anthropic Agent Skills 格式）:
6. 创建或修改 Skill 前，先调用 skill_view("skill-creator") 学习规范
7. name 使用英文 kebab-case（如 deploy-staging），不超过 64 字符
8. description 使用英文第三人称 what+when 用途说明，并在括号内加中文 alias（如 "Deploy to staging (部署到预发). Use when ..."），不超过 1024 字符，不含尖括号
9. frontmatter 顶层只使用 name/description/license/allowed-tools/metadata/compatibility；扩展字段（version/platforms/tags 等）下沉到 metadata，值为 string（list 用逗号分隔）
10. body 遵循 progressive disclosure，超过 500 行时拆到 references/scripts/assets 子目录

你收到的消息是对话摘要，请基于摘要内容决定是否需要创建或修补 skill。\
"""


class SkillEvolutionService:
    """Background self-improvement review service.

    Periodically forks a background agent review that examines conversation
    digests and creates/patches Skills via skill_manage. All operations are
    fire-and-forget: exceptions never propagate to the caller.
    """

    def __init__(
        self,
        chat: Any,
        tool_service: Any,
        max_iterations: int = 16,
        max_concurrent: int = 1,
        enabled: bool = True,
        nudge_interval: int = 10,
        model: str | None = None,
        timeout_seconds: int = 120,
    ):
        self.chat = chat
        self.tool_service = tool_service
        self.max_iterations = max_iterations
        self.max_concurrent = max_concurrent
        self.enabled = enabled
        self.nudge_interval = nudge_interval
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._in_flight: int = 0

    async def maybe_trigger(
        self,
        session_id: str,
        turn_count: int,
        digest: str,
    ) -> None:
        """Fire-and-forget trigger for background skill review.

        Guards: disabled -> skip; turn_count not on interval -> skip;
        at max_concurrent in-flight -> skip. Otherwise spawn a background
        task that runs run_background_review.
        """
        if not self.enabled:
            return
        if turn_count % self.nudge_interval != 0:
            return
        if self._in_flight >= self.max_concurrent:
            logger.debug(
                "skill evolution skipped: max_concurrent reached (in_flight=%d)",
                self._in_flight,
            )
            return
        self._in_flight += 1
        asyncio.create_task(self._guarded_review(session_id, digest))

    async def _guarded_review(self, session_id: str, digest: str) -> None:
        """Wrapper that decrements the in-flight counter in finally."""
        try:
            await self.run_background_review(session_id, digest)
        finally:
            self._in_flight -= 1

    async def run_background_review(
        self,
        session_id: str,
        digest: str,
        prompt: str | None = None,
        max_iterations: int | None = None,
        timeout_seconds: int | None = None,
        allow_toolsets: set[str] | None = None,
        ingress_source: str | None = None,
    ) -> BackgroundReviewResult:
        """Run a background skill review forked-agent turn.

        Builds a skill-only toolset, constructs a ChatCompletionInput with
        background_review provenance, and calls chat.complete under a timeout.
        All exceptions are caught and returned as BackgroundReviewResult.error --
        this method never raises.

        Parameters allow Curator consolidation to override the system prompt
        (umbrella-building), iteration ceiling, timeout, and to narrow the
        toolset to skills-only (excluding memory). Defaults preserve the
        existing nudge-triggered background review behavior.

        ``ingress_source`` lets the Curator consolidation fork declare its
        session source (curator) so the lazily-created session is attributed
        correctly instead of falling back to ``api`` (模式十六). The nudge
        path leaves it None because it reuses an existing user session.
        """
        try:
            used_toolsets = (
                allow_toolsets if allow_toolsets is not None else {"skills", "memory"}
            )
            tools = self.tool_service.build_filtered_definitions(
                allow_toolsets=used_toolsets,
                allow_tool_names={"skill_manage", "skills_list", "skill_view"},
            )
            used_prompt = prompt if prompt is not None else _SKILL_REVIEW_PROMPT
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": used_prompt},
                {"role": "user", "content": digest},
            ]
            # Lazy import to avoid potential circular imports at module load time.
            from app.application.chat_service import ChatCompletionInput

            used_max_iter = (
                max_iterations if max_iterations is not None else self.max_iterations
            )
            used_timeout = (
                timeout_seconds
                if timeout_seconds is not None
                else self.timeout_seconds
            )
            trusted_metadata: dict[str, Any] = {
                "skill_write_origin": "background_review",
            }
            if ingress_source is not None:
                trusted_metadata["gateway.source"] = ingress_source
            request = ChatCompletionInput(
                model=self.model or "",
                messages=messages,
                stream=False,
                session_id=session_id,
                trusted_metadata=trusted_metadata,
                options={"max_iterations": used_max_iter},
            )
            result = await asyncio.wait_for(
                self.chat.complete(request),
                timeout=used_timeout,
            )
            return _extract_review_result(result)
        except Exception as e:
            logger.warning(
                "skill background review failed for session=%s",
                session_id,
                exc_info=True,
            )
            return BackgroundReviewResult(error=str(e))


def _extract_review_result(result: Any) -> BackgroundReviewResult:
    """从 ChatCompletionResult 提取 final_text 与 tool_calls。

    best-effort：仅收集 final assistant message 携带的 tool_calls；中间轮
    tool_calls 未收集时 Curator 分类退化为 pruned（保守）。
    """
    final_text = ""
    tool_calls: list[dict] = []
    msg = getattr(result, "message", None)
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            final_text = content
        elif isinstance(content, list):
            final_text = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in (None, "text")
            )
        tcs = msg.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or ""
                args_raw = fn.get("arguments") or tc.get("arguments") or ""
                if isinstance(args_raw, str) and len(args_raw) > 400:
                    args_raw = args_raw[:400] + "…"
                tool_calls.append({"name": name, "arguments": args_raw})
    return BackgroundReviewResult(final_text=final_text, tool_calls=tool_calls)
