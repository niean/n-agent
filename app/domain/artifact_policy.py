"""ArtifactPolicy -- the 16th domain Policy (Domain Layer).

Pure domain: imports only stdlib + ``app.domain.policy`` (shared kernel) +
``app.domain.artifact`` (own domain types). No Application, Infrastructure,
or other Policy imports (AST-enforced by
``tests/architecture/test_policy_boundaries.py``).

Governance scope (Artifact workbench):
  1. edit_admission: archived status -> DENY; otherwise ALLOW.
  2. publish_admission: DENY if archived, content not available, size over
     the configured publish limit, or kind not in the publish whitelist
     (``other`` is never publishable). Binary kinds (image/pdf) additionally
     require ``classification == "public"`` and no ``sensitive``/``secret``
     labels. Text content release/secret-scan is delegated to
     InformationFlowService at the Application layer -- this Policy does NOT
     inspect text classification/labels.
  3. publish idempotency: keyed on ``(artifact_id, current_checksum)``. When
     the artifact's active publish has the SAME checksum -> ALLOW + reuse=True
     (Application looks up the existing publish_id). When the active publish
     checksum DIFFERS -> ALLOW + reuse=False (replacement; Application revokes
     the old active publish in the same DB transaction). Reuse is scoped to
     the same artifact_id: the Application passes ``active_publish_checksum``
     for THIS artifact only, so a different artifact with the same checksum
     never triggers reuse.
  4. delete_admission: ALLOW always (including when an active publish exists
     -- published snapshots survive via independent storage). Archived also
     ALLOW.

The request carries the current Artifact snapshot, the action, config limits,
and the active-publish fact (queried by Application via the registry). The
Policy does NO IO and does not access the registry.

Returns ``ArtifactPolicyOutcome`` (decision + stable reason code + reuse flag).
Does NOT return publish_id -- the Policy is pure; Application looks up the
existing publish_id when ``reuse=True``.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.domain.artifact import Artifact, ArtifactKind, ArtifactStatus
from app.domain.policy import Policy, PolicyOutcome


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All kinds are publishable EXCEPT ``other`` (unknown binary cannot be safely
# rendered/previewed).
_PUBLISH_WHITELIST: frozenset[ArtifactKind] = frozenset({
    ArtifactKind.DOCUMENT,
    ArtifactKind.MARKDOWN,
    ArtifactKind.CODE,
    ArtifactKind.HTML,
    ArtifactKind.DATA,
    ArtifactKind.CSV,
    ArtifactKind.JSON,
    ArtifactKind.IMAGE,
    ArtifactKind.PDF,
    ArtifactKind.TEXT,
})

# Binary kinds subject to classification/labels gating on publish.
_BINARY_KINDS: frozenset[ArtifactKind] = frozenset({
    ArtifactKind.IMAGE,
    ArtifactKind.PDF,
    ArtifactKind.OTHER,
})

# Labels that block binary publish when present.
_SENSITIVE_LABELS: frozenset[str] = frozenset({"sensitive", "secret"})

# Classification required for binary publish.
_PUBLIC_CLASSIFICATION = "public"


# ---------------------------------------------------------------------------
# Request / outcome / action
# ---------------------------------------------------------------------------


class ArtifactPolicyAction(str, Enum):
    """The action being evaluated against the ArtifactPolicy."""

    EDIT = "edit"
    PUBLISH = "publish"
    DELETE = "delete"


@dataclass(frozen=True)
class ArtifactPolicyRequest:
    """ArtifactPolicy evaluation request (IO-free).

    The Application layer constructs this request from:
      - the current Artifact snapshot (carries ``checksum``, ``size``,
        ``kind``, ``status``, ``classification``, ``labels``),
      - the active-publish checksum for THIS artifact (queried via
        ``ArtifactRegistry.get_active_publish`` -- None when no active
        publish exists),
      - the content-availability fact (queried via ``ArtifactContentStore``),
      - the configured publish size limit.

    The Policy never queries the registry or content store itself.
    """

    artifact: Artifact
    action: ArtifactPolicyAction
    content_available: bool
    active_publish_checksum: str | None
    publish_max_bytes: int


@dataclass(frozen=True)
class ArtifactPolicyOutcome:
    """ArtifactPolicy evaluation outcome.

    Fields:
      decision: the PolicyOutcome (allow/deny/require_approval).
      reason: stable reason code (logged/audited; never user-facing prose).
      reuse: True only for publish when the active publish has the same
          checksum as the current artifact (idempotent re-publish). The
          Application looks up the existing publish_id when reuse=True.
          Always False for deny/edit/delete outcomes.
    """

    decision: PolicyOutcome
    reason: str
    reuse: bool = False


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class ArtifactPolicy(Policy):
    """Artifact admission / publish governance Policy.

    Pure domain: no IO, no cross-Policy imports. All checks are pure
    functions of the request. Deny gates are evaluated before the idempotency
    check so that an ineligible artifact never triggers reuse.
    """

    def evaluate(
        self,
        request: ArtifactPolicyRequest,
        context: None = None,
    ) -> ArtifactPolicyOutcome:
        r = request
        art = r.artifact

        if r.action is ArtifactPolicyAction.EDIT:
            return self._evaluate_edit(art)
        if r.action is ArtifactPolicyAction.DELETE:
            return ArtifactPolicyOutcome(
                PolicyOutcome.ALLOW, "delete_allowed"
            )
        # PUBLISH
        return self._evaluate_publish(r, art)

    # ------------------------------------------------------------------
    # Admission sub-evaluators
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_edit(art: Artifact) -> ArtifactPolicyOutcome:
        if art.status is ArtifactStatus.ARCHIVED:
            return ArtifactPolicyOutcome(PolicyOutcome.DENY, "archived")
        return ArtifactPolicyOutcome(PolicyOutcome.ALLOW, "edit_allowed")

    @staticmethod
    def _evaluate_publish(
        r: ArtifactPolicyRequest, art: Artifact
    ) -> ArtifactPolicyOutcome:
        # --- deny gates (order: archived -> content -> size -> kind -> binary) ---

        if art.status is ArtifactStatus.ARCHIVED:
            return ArtifactPolicyOutcome(PolicyOutcome.DENY, "archived")

        if not r.content_available:
            return ArtifactPolicyOutcome(
                PolicyOutcome.DENY, "content_unavailable"
            )

        if art.size > r.publish_max_bytes:
            return ArtifactPolicyOutcome(PolicyOutcome.DENY, "size_over_limit")

        if art.kind not in _PUBLISH_WHITELIST:
            return ArtifactPolicyOutcome(
                PolicyOutcome.DENY, "kind_not_publishable"
            )

        # Binary classification/labels gating (image/pdf; ``other`` already
        # denied by the whitelist above). Text kinds skip this gate -- text
        # content release/secret-scan is delegated to InformationFlowService.
        if art.kind in _BINARY_KINDS:
            if art.classification != _PUBLIC_CLASSIFICATION:
                return ArtifactPolicyOutcome(
                    PolicyOutcome.DENY, "binary_classification_denied"
                )
            labels = art.labels or ()
            if _SENSITIVE_LABELS.intersection(labels):
                return ArtifactPolicyOutcome(
                    PolicyOutcome.DENY, "binary_classification_denied"
                )

        # --- idempotency: (artifact_id, current_checksum) ---
        # ``active_publish_checksum`` is scoped to THIS artifact by the
        # Application; a different artifact with the same checksum never
        # reaches here with a non-None value.
        active = r.active_publish_checksum
        if active is not None and active == art.checksum:
            return ArtifactPolicyOutcome(
                PolicyOutcome.ALLOW, "publish_reuse", reuse=True
            )
        if active is not None:
            # Different checksum -> replacement publish.
            return ArtifactPolicyOutcome(
                PolicyOutcome.ALLOW, "publish_replace", reuse=False
            )
        return ArtifactPolicyOutcome(
            PolicyOutcome.ALLOW, "publish_new", reuse=False
        )
