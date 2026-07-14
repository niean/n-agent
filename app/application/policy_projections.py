"""Application projections -- cross-domain mapper for ContextPolicy inputs.

This module is the Application-layer mapper that projects MemoryAccessDecision-
allowed refs and ToolService-exposed schemas into Context-owned input types
(``ContextCandidateSet``, ``ToolContextCandidates``).

Key rule: ``app/domain/context_policy.py`` does NOT import MemoryPolicy,
ToolPolicy, or MemoryStore.  The mapper (this module) does the projection
so the Domain policy stays pure.

Projections:
- ``project_messages``: ``ConversationMessage`` list -> ``ContextMessageCandidate`` tuple
- ``project_memory_blocks``: external memory context string -> ``ContextMemoryCandidate`` tuple
- ``project_tool_schemas``: ToolService filtered schemas -> ``ToolContextCandidates``
- ``project_working_messages``: provider-format dict messages -> ``ContextMessageCandidate`` tuple
  (used in the fallback path when ContextService.compress_prepared_context is
  called without a prior build_context_state)
- ``build_context_policy_request``: assembles a ``ContextPolicyRequest`` from
  config values + projected candidates
"""

from __future__ import annotations

import json
from typing import Any

from app.domain.context_policy import (
    ContextCandidateSet,
    ContextMemoryCandidate,
    ContextMessageCandidate,
    ContextPolicyRequest,
    ToolContextCandidates,
)
from app.domain.session import ConversationMessage


def project_messages(
    messages: list[ConversationMessage],
) -> tuple[ContextMessageCandidate, ...]:
    """Project ConversationMessage list to Context-owned candidates.

    Only the fields ContextPolicy needs are carried over. The
    ConversationMessage type (and its datetime/uuid concerns) stays
    outside the policy.
    """
    result: list[ContextMessageCandidate] = []
    for msg in messages:
        has_tool_calls = (
            msg.role == "assistant"
            and isinstance(msg.content, dict)
            and bool(msg.content.get("tool_calls"))
        )
        result.append(
            ContextMessageCandidate(
                id=msg.id,
                role=msg.role,
                content=msg.content,
                is_summary=msg.is_summary,
                is_summarized=msg.is_summarized,
                has_tool_calls=has_tool_calls,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )
        )
    return tuple(result)


def project_working_messages(
    messages: list[dict[str, Any]],
) -> tuple[ContextMessageCandidate, ...]:
    """Project provider-format dict messages to Context-owned candidates.

    Used in the fallback path: when ``compress_prepared_context`` is called
    without a prior ``build_context_state`` (e.g. in unit tests), the
    working_messages are already in dict format and need projection.
    """
    result: list[ContextMessageCandidate] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        has_tool_calls = bool(msg.get("tool_calls"))
        if has_tool_calls and isinstance(content, dict):
            content = {"content": content, "tool_calls": msg.get("tool_calls", [])}
        result.append(
            ContextMessageCandidate(
                id=msg.get("id", f"wm-{idx}"),
                role=role,
                content=content,
                is_summary=(
                    role == "user"
                    and isinstance(content, str)
                    and content.startswith("[CONTEXT SUMMARY]: ")
                ),
                is_summarized=False,
                has_tool_calls=has_tool_calls,
                tool_call_id=msg.get("tool_call_id"),
                name=msg.get("name"),
            )
        )
    return tuple(result)


def project_memory_blocks(
    memory_context: str,
    provider: str = "external",
) -> tuple[ContextMemoryCandidate, ...]:
    """Project external memory context string to Context-owned candidates.

    If the memory context is empty, returns an empty tuple (no injection).
    """
    if not memory_context:
        return ()
    return (
        ContextMemoryCandidate(
            provider=provider,
            block_text=memory_context,
        ),
    )


def project_tool_schemas(
    schemas: list[dict[str, Any]],
) -> ToolContextCandidates:
    """Project ToolService-exposed (already filtered) schemas to Context-owned."""
    return ToolContextCandidates(schemas=tuple(schemas))


def build_context_policy_request(
    candidates: ContextCandidateSet,
    tool_candidates: ToolContextCandidates | None = None,
    *,
    context_length: int = 32000,
    compression_threshold: float = 0.50,
    compression_target_ratio: float = 0.20,
    protect_first_n: int = 3,
    protect_last_n: int = 10,
    cooldown_seconds: int = 300,
    tail_budget_enabled: bool = False,
    force: bool = False,
    in_cooldown: bool = False,
    existing_summary: str = "",
    model_context_window: int | None = None,
) -> ContextPolicyRequest:
    """Assemble a ContextPolicyRequest from projected inputs + config values.

    The Application layer (ContextService) reads config values from the
    ContextEngine (ContextCompressor) or ContextPolicyConfig, and passes
    them here. The Domain policy never imports the Application config types.
    """
    return ContextPolicyRequest(
        candidates=candidates,
        tool_candidates=tool_candidates or ToolContextCandidates(),
        model_context_window=model_context_window or context_length,
        context_length=context_length,
        compression_threshold=compression_threshold,
        compression_target_ratio=compression_target_ratio,
        protect_first_n=protect_first_n,
        protect_last_n=protect_last_n,
        cooldown_seconds=cooldown_seconds,
        tail_budget_enabled=tail_budget_enabled,
        force=force,
        in_cooldown=in_cooldown,
        existing_summary=existing_summary,
    )
