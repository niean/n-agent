from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

_MEMORY_CONTEXT_RE = re.compile(r"<memory-context>.*?</memory-context>", re.DOTALL)


def _extract_memory_block(request_messages_json: str | None) -> str:
    """从 usage_records.request_messages 提取 <memory-context> 块。

    request_messages 是发送给 LLM 的完整消息列表 JSON，其中最后一条 user 消息
    可能被前缀注入了 <memory-context>...</memory-context>。这里扫描所有消息，
    拼接所有出现的 memory-context 块（多 provider 场景仍只有一个外层块）。
    """
    if not request_messages_json:
        return ""
    try:
        messages = json.loads(request_messages_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(messages, list):
        return ""
    blocks: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or ""
                    if isinstance(text, str):
                        blocks.extend(_MEMORY_CONTEXT_RE.findall(text))
        elif isinstance(content, str):
            blocks.extend(_MEMORY_CONTEXT_RE.findall(content))
    return "\n\n".join(blocks)


def _normalize_observation_payload(payload: str | None) -> str | None:
    """Return a readable JSON payload for the observations API.

    Usage records contain a serialized LLM request/response.  Tool arguments
    within those payloads are themselves JSON strings, so a provider that
    emits ``\\uXXXX`` escapes leaves unreadable text after the outer payload is
    parsed by the Dashboard.  Normalize just that nested JSON representation;
    malformed payloads and arguments remain available unchanged for debugging.
    """
    if not isinstance(payload, str):
        return payload
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value

        result = {key: normalize(item) for key, item in value.items()}
        function = result.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
            return result
        try:
            normalized_function = dict(function)
            normalized_function["arguments"] = json.dumps(
                json.loads(function["arguments"]), ensure_ascii=False,
            )
            result["function"] = normalized_function
        except (json.JSONDecodeError, TypeError):
            pass
        return result

    return json.dumps(normalize(decoded), ensure_ascii=False)


def register_usage_routes(
    router: APIRouter,
    usage_service: Any,
    memory_store: Any = None,
    tool_service: Any = None,
    skill_service: Any = None,
) -> None:
    """Register /chat/usage/* API routes.

    The observations page shell (`/observations/sessions`,
    `/observations/sessions/{session_id}` and `/observations/modules`)
    is registered in dashboard.py alongside the other shell routes that
    share the index.html handler. This module only registers the JSON
    API endpoints.
    """

    @router.get("/chat/usage/overview")
    async def usage_overview():
        stats = await usage_service.get_overview_stats()
        return {
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
            "cache_read_tokens": stats.cache_read_tokens,
            "cache_write_tokens": stats.cache_write_tokens,
            "reasoning_tokens": stats.reasoning_tokens,
            "total_tokens": stats.total_tokens,
            "normalized_tokens": stats.normalized_tokens,
            "api_call_count": stats.api_call_count,
            "estimated_cost_usd": stats.estimated_cost_usd,
            "session_count": stats.session_count,
        }

    @router.get("/chat/usage/sessions")
    async def usage_sessions(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        summaries, total = await usage_service.list_sessions_paginated(page, page_size)
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "created_at": s.created_at,
                    "source": s.source,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "cache_read_tokens": s.cache_read_tokens,
                    "cache_write_tokens": s.cache_write_tokens,
                    "reasoning_tokens": s.reasoning_tokens,
                    "total_tokens": s.total_tokens,
                    "normalized_tokens": s.normalized_tokens,
                    "api_call_count": s.api_call_count,
                    "turn_count": s.turn_count,
                    "estimated_cost_usd": s.estimated_cost_usd,
                    "cost_status": s.cost_status,
                }
                for s in summaries
            ],
        }

    @router.get("/chat/usage/sessions/{session_id}")
    async def session_stats(session_id: str):
        stats = await usage_service.get_session_stats(session_id)
        return {
            "session_id": stats.session_id,
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
            "cache_read_tokens": stats.cache_read_tokens,
            "cache_write_tokens": stats.cache_write_tokens,
            "reasoning_tokens": stats.reasoning_tokens,
            "total_tokens": stats.total_tokens,
            "normalized_tokens": stats.normalized_tokens,
            "api_call_count": stats.api_call_count,
            "estimated_cost_usd": stats.estimated_cost_usd,
            "cost_status": stats.cost_status,
        }

    @router.get("/chat/usage/sessions/{session_id}/records")
    async def session_records(session_id: str, limit: int = Query(50, ge=1, le=500)):
        records = await usage_service.list_records(session_id, limit)
        return [
            {
                "id": r.id,
                "model": r.model,
                "requested_model": r.requested_model,
                "trigger_type": r.trigger_type,
                "provider": r.provider,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cache_read_tokens": r.cache_read_tokens,
                "cache_write_tokens": r.cache_write_tokens,
                "reasoning_tokens": r.reasoning_tokens,
                "total_tokens": r.total_tokens,
                "normalized_tokens": r.normalized_tokens,
                "estimated_cost_usd": r.estimated_cost_usd,
                "cost_status": r.cost_status,
                "latency_ms": r.latency_ms,
                "created_at": r.created_at,
                "request_messages": _normalize_observation_payload(r.request_messages),
                "response_message": _normalize_observation_payload(r.response_message),
                "tools": r.tools,
                "generation_params": r.generation_params,
            }
            for r in records
        ]

    @router.get("/chat/usage/sessions/{session_id}/compressions")
    async def session_compressions(session_id: str):
        comps = await usage_service.list_compressions(session_id)
        return [
            {
                "id": c.id,
                "before_tokens": c.before_tokens,
                "after_tokens": c.after_tokens,
                "tokens_saved": c.tokens_saved,
                "compression_ratio": c.compression_ratio,
                "created_at": c.created_at,
                "before_messages": c.before_messages,
                "after_messages": c.after_messages,
            }
            for c in comps
        ]

    @router.get("/chat/usage/sessions/{session_id}/breakdown")
    async def session_breakdown(session_id: str, request: Request):
        messages: list[dict] = []
        if memory_store is not None:
            try:
                msgs = await memory_store.list_messages(session_id)
                for m in msgs:
                    content = m.content
                    if isinstance(content, dict):
                        messages.append({"role": m.role, "content": content})
                    else:
                        messages.append({"role": m.role, "content": content or ""})
            except Exception:
                logger.warning("list_messages failed for %s", session_id, exc_info=True)
                messages = []
        from app.application.prompt_builder import build_system_prompt
        skills_index: str | None = None
        if skill_service is not None:
            try:
                skills_index = await skill_service.build_skills_index() or None
            except Exception:
                logger.warning("build_skills_index failed for breakdown", exc_info=True)
        system_prompt = build_system_prompt(skills_index=skills_index)
        tool_defs: list[dict] = []
        if tool_service is not None:
            try:
                defs = tool_service.list_definitions()
                for d in defs:
                    schema = getattr(d, "input_schema", None) or {}
                    name = getattr(d, "name", "") or ""
                    desc = getattr(d, "description", "") or ""
                    tool_defs.append({"name": name, "description": desc, "input_schema": schema})
            except Exception:
                logger.warning("tool_service.list_definitions failed", exc_info=True)
                tool_defs = []
        # 外部记忆块不在 memory_store 的持久化消息里（agent_graph 只注入到 api_messages 副本），
        # 需从最近一次 usage_records.request_messages 提取 <memory-context> 块。
        external_memory_block = ""
        try:
            records = await usage_service.list_records(session_id, limit=1)
            if records:
                external_memory_block = _extract_memory_block(records[0].request_messages)
        except Exception:
            logger.warning("extract memory block failed for %s", session_id, exc_info=True)
        breakdown = usage_service.get_context_breakdown(
            system_prompt, tool_defs, messages, external_memory_block,
        )
        return {
            "system_prompt": breakdown.system_prompt,
            "tool_definitions": breakdown.tool_definitions,
            "memory": breakdown.memory,
            "conversation": breakdown.conversation,
            "total": breakdown.total,
        }
