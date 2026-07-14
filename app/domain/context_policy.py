"""Context Policy -- Domain-level governance for context assembly.

This module is pure Domain: it imports only stdlib/typing/dataclasses and
``app.domain.context``.  It does NOT import MemoryStore, MemoryPolicy,
ToolPolicy, pydantic, or Infrastructure.

The Policy takes a ``ContextPolicyRequest`` (Context-owned candidates + config
values) and returns a ``ContextPlan``.  It performs NO database reads, NO
LLM calls, and NO tool execution.

Cross-domain rule: ContextPolicy receives ONLY Context-owned input types
(``ContextCandidateSet``, ``ToolContextCandidates``).  The Application mapper
(``policy_projections.py``) projects MemoryAccessDecision-allowed refs and
ToolService-exposed schemas into these Context-owned types before calling
the policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Context-owned input types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextMessageCandidate:
    """Context-owned view of a conversation message for policy decisions.

    Carries only the fields ContextPolicy needs to decide selection,
    grouping, and compression -- WITHOUT being a ConversationMessage.
    """

    id: str
    role: str
    content: Any
    is_summary: bool = False
    is_summarized: bool = False
    has_tool_calls: bool = False
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ContextMemoryCandidate:
    """Context-owned view of an external memory block for injection."""

    provider: str
    block_text: str
    origin: str = ""


@dataclass(frozen=True)
class ContextCandidateSet:
    """Input to ContextPolicy: message + memory candidates.

    Messages are already policy-gated (only MemoryAccessDecision-allowed
    refs are projected here by the Application mapper).
    """

    messages: tuple[ContextMessageCandidate, ...]
    memory_blocks: tuple[ContextMemoryCandidate, ...] = ()


@dataclass(frozen=True)
class ToolContextCandidates:
    """Context-owned view of tool schemas for token allocation.

    Projected from ToolService-exposed (already filtered) schemas.
    ContextPolicy uses this only for token budget estimation, NOT for
    tool exposure decisions (those are ToolPolicy's domain).
    """

    schemas: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Plan types (ContextPolicy output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompressionPlan:
    """What to compress: head/tail protection + target ratio.

    The ContextEngine (ContextCompressor) executes this plan: it protects
    the first ``head_n`` and last ``tail_n`` messages, summarizes the
    middle, and aims for ``target_ratio`` compression.
    """

    head_n: int
    tail_n: int
    target_ratio: float
    force: bool = False


@dataclass(frozen=True)
class InjectionPlan:
    """Where/how to inject external memory into the context.

    External memory is injected (prepended) into the target user message
    to maintain conversation flow.  When ``target_message_id`` is None,
    no injection occurs (no user message or no memory blocks).
    """

    target_message_id: str | None = None
    position: str = "prepend"


@dataclass(frozen=True)
class TokenAllocation:
    """Token budget allocation across context sections."""

    system: int = 0
    session: int = 0
    turn: int = 0
    tool: int = 0
    total: int = 0


@dataclass(frozen=True)
class ContextPlan:
    """Output of ContextPolicy: the full context assembly plan.

    - ``selected_message_ids``: which candidate messages to include.
    - ``compression``: if not None, the ContextEngine should compress.
    - ``injection``: where to inject external memory.
    - ``token_allocation``: budget for system/session/turn/tool.
    - ``reasons``: human-readable decision trail.
    """

    selected_message_ids: tuple[str, ...]
    compression: CompressionPlan | None
    injection: InjectionPlan
    token_allocation: TokenAllocation
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Request type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPolicyRequest:
    """Input to ``ContextPolicy.evaluate``.

    Carries Context-owned candidates, tool candidates, model context window,
    and config values (unpacked from Application's ``ContextPolicyConfig``
    by the Application mapper).  All fields are plain values -- no DB
    references, no store handles, no policy decision objects from other
    domains.
    """

    candidates: ContextCandidateSet
    tool_candidates: ToolContextCandidates = field(default_factory=ToolContextCandidates)
    model_context_window: int = 32000
    context_length: int = 32000
    compression_threshold: float = 0.50
    compression_target_ratio: float = 0.20
    protect_first_n: int = 3
    protect_last_n: int = 10
    cooldown_seconds: int = 300
    tail_budget_enabled: bool = False
    force: bool = False
    in_cooldown: bool = False
    existing_summary: str = ""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ContextPolicy(Protocol):
    """Domain Protocol for context assembly governance."""

    def evaluate(self, request: ContextPolicyRequest) -> ContextPlan: ...


# ---------------------------------------------------------------------------
# Token estimation (Domain-level heuristic, matching ContextCompressor)
# ---------------------------------------------------------------------------


def _estimate_token_count(content: Any) -> int:
    """Estimate token count for a single message content."""
    if isinstance(content, str):
        return len(content) // 4 + 10
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += len(part.get("text", "")) // 4
                elif part.get("type") == "image_url":
                    total += 1500
                else:
                    total += len(json.dumps(part, ensure_ascii=False, default=str)) // 4
            else:
                total += len(str(part)) // 4
        return total + 10
    if isinstance(content, dict):
        return len(json.dumps(content, ensure_ascii=False, default=str)) // 4 + 10
    return len(str(content or "")) // 4 + 10


def _estimate_messages_tokens(messages: tuple[ContextMessageCandidate, ...]) -> int:
    """Estimate total token count for a tuple of message candidates."""
    total = 0
    for msg in messages:
        if msg.has_tool_calls and isinstance(msg.content, dict):
            # Content dict is {"content": ..., "tool_calls": [...]}.
            # Estimate content and tool_calls separately to avoid
            # double-counting (matching ContextCompressor._estimate_tokens
            # which reads content and tool_calls as separate fields).
            inner_content = msg.content.get("content", "")
            total += _estimate_token_count(inner_content)
            tool_calls = msg.content.get("tool_calls") or []
            if tool_calls:
                total += len(json.dumps(tool_calls, ensure_ascii=False, default=str)) // 4
        else:
            total += _estimate_token_count(msg.content)
        if msg.tool_call_id:
            total += len(msg.tool_call_id) // 4
        if msg.name:
            total += len(msg.name) // 4
    return total


# ---------------------------------------------------------------------------
# DefaultContextPolicy implementation
# ---------------------------------------------------------------------------


class DefaultContextPolicy:
    """Default ContextPolicy implementation.

    Decision table (first-stage defaults, matching existing behavior):
    - context_length=32000, threshold=0.50, target=0.20
    - protect first 3 / last 10
    - cooldown 300s
    - tail budget disabled
    - Keep current 3-segment compression, latest summary, tool completeness
    - External memory injected to the last user message
    - Use ONLY upstream-allowed candidates (from ContextCandidateSet)
    """

    def evaluate(self, request: ContextPolicyRequest) -> ContextPlan:
        reasons: list[str] = []

        # 1. Select messages
        selected_ids = self._select_messages(request.candidates.messages)
        reasons.append(f"selected {len(selected_ids)} messages")

        # 2. Decide compression
        compression = self._decide_compression(request, reasons)

        # 3. Injection plan
        injection = self._build_injection_plan(request.candidates)
        if injection.target_message_id:
            reasons.append(f"inject memory to message {injection.target_message_id}")
        else:
            reasons.append("no memory injection")

        # 4. Token allocation
        token_allocation = self._allocate_tokens(request)
        reasons.append(
            f"token allocation: system={token_allocation.system} "
            f"session={token_allocation.session} "
            f"turn={token_allocation.turn} "
            f"tool={token_allocation.tool}"
        )

        return ContextPlan(
            selected_message_ids=selected_ids,
            compression=compression,
            injection=injection,
            token_allocation=token_allocation,
            reasons=tuple(reasons),
        )

    # -- message selection ------------------------------------------------

    def _select_messages(
        self,
        messages: tuple[ContextMessageCandidate, ...],
    ) -> tuple[str, ...]:
        """Select messages: all non-summarized + latest summary only.

        Matches existing ``_build_latest_compressed_context`` behavior:
        - Find the latest (last) summary message.
        - If no summary: select all non-summarized messages.
        - If summary exists: select all non-summarized + non-summary
          messages, plus the latest summary. Drop old summaries and
          summarized messages.
        """
        latest_summary_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].is_summary:
                latest_summary_idx = idx
                break

        if latest_summary_idx == -1:
            return tuple(m.id for m in messages if not m.is_summarized)

        selected: list[str] = []
        for idx, m in enumerate(messages):
            if m.is_summary and idx != latest_summary_idx:
                continue
            if m.is_summarized and not m.is_summary:
                continue
            selected.append(m.id)
        return tuple(selected)

    # -- compression decision ---------------------------------------------

    def _decide_compression(
        self,
        request: ContextPolicyRequest,
        reasons: list[str],
    ) -> CompressionPlan | None:
        """Decide whether to compress based on threshold, cooldown, and force."""
        if request.in_cooldown and not request.force:
            reasons.append("compression skipped: cooldown")
            return None

        tokens = _estimate_messages_tokens(request.candidates.messages)
        threshold = max(1, int(request.context_length * request.compression_threshold))

        if not request.force and tokens <= threshold:
            reasons.append(
                f"compression skipped: tokens {tokens} <= threshold {threshold}"
            )
            return None

        if request.force:
            reasons.append("compression forced")
        else:
            reasons.append(
                f"compression triggered: tokens {tokens} > threshold {threshold}"
            )

        return CompressionPlan(
            head_n=request.protect_first_n,
            tail_n=request.protect_last_n,
            target_ratio=request.compression_target_ratio,
            force=request.force,
        )

    # -- injection plan ---------------------------------------------------

    def _build_injection_plan(
        self,
        candidates: ContextCandidateSet,
    ) -> InjectionPlan:
        """Build injection plan: target the last non-summary user message."""
        if not candidates.memory_blocks:
            return InjectionPlan(target_message_id=None)
        for msg in reversed(candidates.messages):
            if msg.role == "user" and not msg.is_summary:
                return InjectionPlan(
                    target_message_id=msg.id,
                    position="prepend",
                )
        return InjectionPlan(target_message_id=None)

    # -- token allocation -------------------------------------------------

    def _allocate_tokens(
        self,
        request: ContextPolicyRequest,
    ) -> TokenAllocation:
        """Allocate token budgets across context sections.

        Simple heuristic: system ~10% (capped at 2000), tools ~10%
        (scaled by tool count), remaining split evenly between session
        history and current turn.
        """
        total = request.model_context_window
        system = min(total // 10, 2000)
        tool_count = len(request.tool_candidates.schemas)
        tool = min(total // 10, max(100, tool_count * 100))
        remaining = max(0, total - system - tool)
        session = remaining // 2
        turn = remaining - session
        return TokenAllocation(
            system=system,
            session=session,
            turn=turn,
            tool=tool,
            total=total,
        )
