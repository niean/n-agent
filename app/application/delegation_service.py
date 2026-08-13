"""DelegationService -- delegation orchestration facade (Application Layer).

Owns the end-to-end ``delegate`` flow:

  1. Parent capability + feature-flag check (no capability ->
     ``delegation_not_authorized``).
  2. Strict request parsing + normalization + fingerprint
     (DelegationRequestParser).
  3. DelegationPolicy admission evaluation (DENY -> ``delegation_invalid``).
  4. Parent BudgetService total-budget reservation (DENY ->
     ``delegation_budget_exceeded``); released on transaction failure.
  5. Derive a non-escalating child policy snapshot from the parent.
  6. registry.create_or_reconnect (single transaction: delegation + members
     + snapshot + ledger + event). Conflict (different fingerprint) ->
     release reservation + raise.
  7. Drive run_service.tick() until the delegation reaches a terminal state.
  8. Read the terminal ResultSet and filter every member/aggregator result
     through InformationFlowService.release(PARENT) before returning.

DelegationService does NOT finalize Tasks, adopt root Artifacts, or write
the user's final message. It returns a bounded, information-flow-filtered
``DelegationResultSet`` to the caller (the tool executor / parent adapter).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from app.application.delegation_request_parser import (
    DelegationError,
    DelegationRequestParser,
)
from app.domain.delegation import (
    DelegationChildSpec,
    DelegationCreateRequest,
    DelegationConflictError,
    DelegationMember,
    DelegationMemberRole,
    DelegationParentRef,
    DelegationResultSet,
    DelegationStatus,
    PolicySnapshotRecord,
)
from app.domain.delegation_policy import DelegationPolicy, DelegationPolicyRequest
from app.domain.information_flow import Classification, ReleaseTarget
from app.domain.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Ports (structural typing via Protocol)
# ---------------------------------------------------------------------------


class _ClockLike(Protocol):
    def now_iso(self) -> str: ...


class _InfoFlowLike(Protocol):
    def release(
        self,
        content: str,
        target: ReleaseTarget,
        *,
        classification: Classification = ...,
        origin: str = ...,
        labels: frozenset[str] = ...,
        run_id: str = ...,
        session_id: str = ...,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Guard against runaway tick loops (each tick is one scheduling pass).
_MAX_TICKS: int = 200


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DelegationService:
    """Delegation orchestration facade.

    ``parent_capability`` is a plain dict (not ToolExecutionContext) so the
    service stays testable; the T12 parent adapters adapt the real
    ``ToolExecutionContext`` to this dict interface. Expected keys:
    ``source``, ``scope_id``, ``run_id``, ``session_id``, ``actor_id``,
    ``has_capability``, ``classification``, ``parent_allowed_tools``,
    ``system_child_allowlist`` (and optionally ``parent_deadline``).
    """

    def __init__(
        self,
        *,
        registry: Any,
        run_service: Any,
        policy: DelegationPolicy,
        info_flow: _InfoFlowLike,
        clock: _ClockLike,
        config: Any,
        budget_service: Any | None = None,
        result_store: Any | None = None,
    ) -> None:
        self._registry = registry
        self._run_service = run_service
        self._policy = policy
        self._info_flow = info_flow
        self._clock = clock
        self._config = config
        self._budget_service = budget_service
        self._result_store = result_store
        self._parser = DelegationRequestParser()

    # ------------------------------------------------------------------
    # delegate
    # ------------------------------------------------------------------

    async def delegate(
        self,
        *,
        parent_capability: Mapping[str, Any],
        delegation_key: str,
        children: list[DelegationChildSpec] | tuple[DelegationChildSpec, ...],
        join_policy: str,
        aggregation: str,
        timeout_seconds: int,
        aggregator_instruction: str | None = None,
    ) -> DelegationResultSet:
        """Create (or reconnect to) a delegation, run it to terminal, and
        return the information-flow-filtered result set.

        Raises ``DelegationError`` with a stable code on any non-retriable
        failure (not authorized, invalid, budget exceeded, conflict).
        """
        # 1. Parent capability + feature flags.
        self._check_feature_flags(parent_capability)
        if not parent_capability.get("has_capability", False):
            raise DelegationError(
                "delegation_not_authorized",
                "parent lacks delegation capability",
            )

        # 2. Normalize + fingerprint.
        key = self._parser.normalize_key(delegation_key)
        norm_children = self._parser.normalize_children(children)
        fingerprint = self._parser.fingerprint(
            delegation_key, norm_children, join_policy, aggregation,
            timeout_seconds, aggregator_instruction,
        )

        # 3. Policy admission.
        policy_request = self._build_policy_request(
            parent_capability, norm_children, join_policy, aggregation,
            timeout_seconds,
        )
        if self._policy.evaluate(policy_request) is not PolicyOutcome.ALLOW:
            raise DelegationError(
                "delegation_invalid", "delegation policy denied the request"
            )

        # 4. Parent budget reservation (optional).
        reservation = await self._reserve_parent_budget(
            parent_capability, norm_children, aggregation, aggregator_instruction
        )

        # 5-6. Build snapshot + members + create request + persist.
        try:
            snapshot = self._build_snapshot(parent_capability)
            deadline_at = self._compute_deadline(
                parent_capability, timeout_seconds
            )
            members = self._build_members(
                norm_children, deadline_at, aggregation, aggregator_instruction
            )
            total_budget = self._total_budget(norm_children, aggregation)
            create_request = DelegationCreateRequest(
                parent=self._build_parent_ref(parent_capability),
                delegation_key=key,
                fingerprint=fingerprint,
                join_policy=join_policy,
                aggregation=aggregation,
                deadline_at=deadline_at,
                budget_total_tokens=total_budget,
                members=members,
                snapshot=snapshot,
            )
            delegation = await self._registry.create_or_reconnect(create_request)
        except DelegationConflictError as exc:
            await self._release_parent_budget(parent_capability, reservation)
            raise DelegationError(
                "delegation_conflict", str(exc)
            ) from exc
        except DelegationError:
            await self._release_parent_budget(parent_capability, reservation)
            raise
        except Exception as exc:
            await self._release_parent_budget(parent_capability, reservation)
            raise DelegationError(
                "delegation_create_failed", "create_or_reconnect failed"
            ) from exc

        # 7. Drive to terminal.
        self._run_service.set_parent_capability(delegation.id, dict(parent_capability))
        await self._drive_to_terminal(delegation.id)

        # 8. Read + filter result set.
        result_set = await self._registry.get_result_set(delegation.id)
        if result_set is None:
            # No terminal result set (should not happen after terminal drive).
            raise DelegationError(
                "delegation_no_result", "delegation produced no result set"
            )
        return self._filter_result_set(result_set, parent_capability)

    # ------------------------------------------------------------------
    # feature flags
    # ------------------------------------------------------------------

    def _check_feature_flags(self, cap: Mapping[str, Any]) -> None:
        if not getattr(self._config, "enabled", True):
            raise DelegationError(
                "delegation_disabled", "delegation feature is disabled"
            )
        source = cap.get("source", "")
        if source == "realtime" and not getattr(
            self._config, "realtime_enabled", True
        ):
            raise DelegationError(
                "delegation_disabled", "realtime delegation is disabled"
            )
        if source == "task" and not getattr(self._config, "task_enabled", True):
            raise DelegationError(
                "delegation_disabled", "task delegation is disabled"
            )

    # ------------------------------------------------------------------
    # policy request
    # ------------------------------------------------------------------

    def _build_policy_request(
        self,
        cap: Mapping[str, Any],
        children: tuple[DelegationChildSpec, ...],
        join_policy: str,
        aggregation: str,
        timeout_seconds: int,
    ) -> DelegationPolicyRequest:
        return DelegationPolicyRequest(
            parent=self._build_parent_ref(cap),
            has_capability=bool(cap.get("has_capability", False)),
            children=children,
            join_policy=join_policy,
            aggregation=aggregation,
            depth=1,
            parent_allowed_tools=frozenset(cap.get("parent_allowed_tools", frozenset())),
            system_child_allowlist=frozenset(cap.get("system_child_allowlist", frozenset())),
            max_children=getattr(self._config, "max_children", 8),
            max_runtime_seconds=getattr(self._config, "max_runtime_seconds", None),
            member_max_runtime_seconds=getattr(
                self._config, "member_max_runtime_seconds", None
            ),
            max_total_tokens=getattr(self._config, "max_total_tokens", None),
            max_tokens_per_child=getattr(self._config, "max_tokens_per_child", None),
            timeout_seconds=timeout_seconds,
            parent_deadline=cap.get("parent_deadline"),
            classification=cap.get("classification"),
        )

    # ------------------------------------------------------------------
    # budget
    # ------------------------------------------------------------------

    async def _reserve_parent_budget(
        self,
        cap: Mapping[str, Any],
        children: tuple[DelegationChildSpec, ...],
        aggregation: str,
        aggregator_instruction: str | None,
    ) -> Any | None:
        if self._budget_service is None:
            return None
        total = self._total_budget(children, aggregation)
        if total <= 0:
            return None
        from app.domain.budget import BudgetReserveKind, BudgetReserveRequest

        run_id = cap.get("run_id", "")
        request = BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=total,
        )
        decision = await self._budget_service.reserve(run_id, request)
        if decision.outcome is not PolicyOutcome.ALLOW:
            raise DelegationError(
                "delegation_budget_exceeded",
                "parent budget denied the delegation reservation",
            )
        return decision

    async def _release_parent_budget(
        self, cap: Mapping[str, Any], reservation: Any | None
    ) -> None:
        if self._budget_service is None or reservation is None:
            return
        run_id = cap.get("run_id", "")
        try:
            await self._budget_service.release(run_id, reservation)
        except Exception:
            pass  # best-effort release; ledger is the recovery authority

    def _total_budget(
        self,
        children: tuple[DelegationChildSpec, ...],
        aggregation: str,
    ) -> int:
        total = sum(c.budget_tokens for c in children)
        # Aggregator budget is counted only when aggregation is AGENT and an
        # aggregator member is appended (see _build_members).
        return max(total, 0)

    # ------------------------------------------------------------------
    # snapshot + members
    # ------------------------------------------------------------------

    def _build_snapshot(
        self, cap: Mapping[str, Any]
    ) -> PolicySnapshotRecord:
        """Derive a non-escalating child snapshot from the parent config.

        Only execution-required whitelisted fields are projected -- never
        secrets or executable objects. The child config is the parent
        config with delegation recursively disabled (children cannot
        delegate).
        """
        parent_config: dict[str, Any] = {
            "max_children": getattr(self._config, "max_children", 8),
            "max_runtime_seconds": getattr(self._config, "max_runtime_seconds", None),
            "max_total_tokens": getattr(self._config, "max_total_tokens", None),
        }
        child_config: dict[str, Any] = dict(parent_config)
        child_config["delegation_enabled"] = False  # no recursive delegation
        import hashlib
        import json
        checksum = hashlib.sha256(
            json.dumps(
                {"p": parent_config, "c": child_config},
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return PolicySnapshotRecord(
            profile_version="system-v1",
            parent_config=parent_config,
            child_config=child_config,
            aggregator_config=None,
            checksum=checksum,
        )

    def _compute_deadline(
        self, cap: Mapping[str, Any], timeout_seconds: int
    ) -> str | None:
        now = self._parse_clock(self._clock.now_iso())
        deadline = now + timedelta(seconds=timeout_seconds)
        parent_deadline_s = cap.get("parent_deadline")
        if isinstance(parent_deadline_s, (int, float)) and parent_deadline_s > 0:
            parent_deadline = now + timedelta(seconds=float(parent_deadline_s))
            if parent_deadline < deadline:
                deadline = parent_deadline
        return deadline.isoformat()

    @staticmethod
    def _parse_clock(iso: str) -> datetime:
        # Python 3.11+ fromisoformat accepts 'Z'.
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _build_members(
        self,
        children: tuple[DelegationChildSpec, ...],
        deadline_at: str | None,
        aggregation: str,
        aggregator_instruction: str | None,
    ) -> tuple[DelegationMember, ...]:
        from uuid import uuid4

        members: list[DelegationMember] = []
        for ordinal, spec in enumerate(children):
            members.append(
                DelegationMember.new(
                    delegation_id="",  # registry assigns the real id on insert
                    role=DelegationMemberRole.WORKER,
                    ordinal=ordinal,
                    title=spec.title,
                    instruction=spec.instruction,
                    skills=spec.skills,
                    allowed_tools=spec.allowed_tools,
                    execution_session_id=f"delegation-{uuid4()}",
                    deadline_at=deadline_at,
                    budget_tokens=spec.budget_tokens,
                    model_override=spec.model_override,
                    max_runtime_seconds=spec.max_runtime_seconds,
                )
            )
        if aggregation == "agent" and aggregator_instruction:
            members.append(
                DelegationMember.new(
                    delegation_id="",
                    role=DelegationMemberRole.AGGREGATOR,
                    ordinal=len(members),
                    title="aggregator",
                    instruction=aggregator_instruction.strip(),
                    skills=(),
                    allowed_tools=(),
                    execution_session_id=f"delegation-{uuid4()}",
                    deadline_at=deadline_at,
                    budget_tokens=0,
                    max_runtime_seconds=getattr(
                        self._config, "member_max_runtime_seconds", None
                    ),
                )
            )
        return tuple(members)

    @staticmethod
    def _build_parent_ref(cap: Mapping[str, Any]) -> DelegationParentRef:
        return DelegationParentRef(
            source=cap.get("source", ""),
            scope_id=cap.get("scope_id", ""),
            run_id=cap.get("run_id", ""),
            session_id=cap.get("session_id", ""),
        )

    # ------------------------------------------------------------------
    # drive to terminal
    # ------------------------------------------------------------------

    async def _drive_to_terminal(self, delegation_id: str) -> None:
        for _ in range(_MAX_TICKS):
            delegation = await self._registry.get(delegation_id)
            if delegation is None:
                raise DelegationError(
                    "deletion_missing", "delegation disappeared during execution"
                )
            if delegation.is_terminal:
                return
            await self._run_service.tick()
        # Loop exhausted without reaching terminal -- treat as failure.
        raise DelegationError(
            "delegation_timeout", "delegation did not reach terminal state"
        )

    # ------------------------------------------------------------------
    # information-flow filtering
    # ------------------------------------------------------------------

    def _filter_result_set(
        self,
        result_set: DelegationResultSet,
        cap: Mapping[str, Any],
    ) -> DelegationResultSet:
        """Filter every result summary through info_flow.release(PARENT).

        Denied or redacted content is replaced with a stable placeholder so
        no secret reaches the parent. Returns a new ``DelegationResultSet``
        with filtered member/aggregator results.
        """
        run_id = cap.get("run_id", "")
        session_id = cap.get("session_id", "")
        classification = self._classification(cap)
        filtered_members = tuple(
            self._filter_result(r, classification, run_id, session_id)
            for r in result_set.member_results
        )
        filtered_agg = (
            self._filter_result(
                result_set.aggregation_result, classification, run_id, session_id
            )
            if result_set.aggregation_result is not None
            else None
        )
        from dataclasses import replace

        return replace(
            result_set,
            member_results=filtered_members,
            aggregation_result=filtered_agg,
        )

    def _filter_result(
        self,
        result: Any,
        classification: Classification,
        run_id: str,
        session_id: str,
    ) -> Any:
        if result is None:
            return None
        from dataclasses import replace

        summary = getattr(result, "summary", "") or ""
        release = self._info_flow.release(
            summary,
            ReleaseTarget.PARENT,
            classification=classification,
            origin="delegation_result",
            run_id=run_id,
            session_id=session_id,
        )
        if release.allowed:
            new_summary = release.content if release.content is not None else summary
        else:
            new_summary = "[filtered]"
        return replace(result, summary=new_summary)

    @staticmethod
    def _classification(cap: Mapping[str, Any]) -> Classification:
        raw = cap.get("classification", "internal")
        try:
            return Classification(raw)
        except (ValueError, TypeError):
            return Classification.INTERNAL
