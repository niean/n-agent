"""Tests for ArtifactPolicy -- the 16th domain Policy.

Pure domain tests -- no IO, no FastAPI, no SQLite. Validates the four
admission gates (edit / publish / delete / binary-classification) and the
publish idempotency key (artifact_id, current_checksum) with the reuse flag.

The Policy does NO IO: the request explicitly carries ``content_available``,
the active-publish checksum (queried by Application), the current artifact
snapshot, and config limits. The Policy never touches the registry.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.artifact import (
    Artifact,
    ArtifactKind,
    ArtifactSource,
    ArtifactStatus,
)
from app.domain.artifact_policy import (
    ArtifactPolicy,
    ArtifactPolicyAction,
    ArtifactPolicyOutcome,
    ArtifactPolicyRequest,
)
from app.domain.policy import PolicyOutcome


_VALID_CHECKSUM = "sha256:" + "a" * 64
_ALT_CHECKSUM = "sha256:" + "b" * 64
_PUBLISH_MAX = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_artifact(**overrides) -> Artifact:
    """Build a valid inline text artifact (manual source, source_ref=id)."""
    fields: dict = dict(
        id="art-1",
        name="doc.txt",
        kind=ArtifactKind.TEXT,
        mime="text/plain",
        content_ref=None,
        inline_content="hello",
        size=5,
        checksum=_VALID_CHECKSUM,
        source_kind=ArtifactSource.MANUAL,
        source_context_ref=None,
        summary="",
        classification=None,
        labels=None,
        status=ArtifactStatus.DRAFT,
        created_at=None,
        updated_at=None,
        created_by="user-1",
    )
    fields.update(overrides)
    if "source_ref" not in overrides:
        fields["source_ref"] = fields["id"]
    return Artifact(**fields)


def _binary_artifact(**overrides) -> Artifact:
    """Build a valid binary (image) artifact with content_ref."""
    fields: dict = dict(
        id="art-img-1",
        name="logo.png",
        kind=ArtifactKind.IMAGE,
        mime="image/png",
        content_ref="store://bucket/logo.png",
        inline_content=None,
        size=4096,
        checksum=_VALID_CHECKSUM,
        source_kind=ArtifactSource.MANUAL,
        source_context_ref=None,
        summary="",
        classification=None,
        labels=None,
        status=ArtifactStatus.DRAFT,
        created_at=None,
        updated_at=None,
        created_by="user-1",
    )
    fields.update(overrides)
    if "source_ref" not in overrides:
        fields["source_ref"] = fields["id"]
    return Artifact(**fields)


def _req(
    artifact: Artifact,
    action: ArtifactPolicyAction = ArtifactPolicyAction.PUBLISH,
    **kw,
) -> ArtifactPolicyRequest:
    base: dict = dict(
        artifact=artifact,
        action=action,
        content_available=True,
        active_publish_checksum=None,
        publish_max_bytes=_PUBLISH_MAX,
    )
    base.update(kw)
    return ArtifactPolicyRequest(**base)


# ---------------------------------------------------------------------------
# Outcome is frozen
# ---------------------------------------------------------------------------


def test_outcome_is_frozen():
    out = ArtifactPolicyOutcome(PolicyOutcome.ALLOW, "x")
    with pytest.raises(FrozenInstanceError):
        out.reuse = True  # type: ignore[misc]


def test_outcome_reuse_defaults_false():
    out = ArtifactPolicyOutcome(PolicyOutcome.ALLOW, "x")
    assert out.reuse is False


# ---------------------------------------------------------------------------
# edit_admission
# ---------------------------------------------------------------------------


def test_edit_archived_denied():
    art = _text_artifact(status=ArtifactStatus.ARCHIVED)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.EDIT))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "archived"
    assert d.reuse is False


def test_edit_draft_allowed():
    art = _text_artifact(status=ArtifactStatus.DRAFT)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.EDIT))
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reason == "edit_allowed"


def test_edit_published_allowed():
    art = _text_artifact(status=ArtifactStatus.PUBLISHED)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.EDIT))
    assert d.decision is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# delete_admission -- always ALLOW
# ---------------------------------------------------------------------------


def test_delete_draft_allowed():
    art = _text_artifact(status=ArtifactStatus.DRAFT)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.DELETE))
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reason == "delete_allowed"


def test_delete_archived_allowed():
    art = _text_artifact(status=ArtifactStatus.ARCHIVED)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.DELETE))
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reason == "delete_allowed"


def test_delete_allowed_even_with_active_publish():
    art = _text_artifact(status=ArtifactStatus.PUBLISHED)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.DELETE,
            active_publish_checksum=_VALID_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reason == "delete_allowed"
    assert d.reuse is False


# ---------------------------------------------------------------------------
# publish_admission -- deny gates
# ---------------------------------------------------------------------------


def test_publish_archived_denied():
    art = _text_artifact(status=ArtifactStatus.ARCHIVED)
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "archived"


def test_publish_content_unavailable_denied():
    art = _text_artifact()
    d = ArtifactPolicy().evaluate(
        _req(art, action=ArtifactPolicyAction.PUBLISH, content_available=False)
    )
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "content_unavailable"


def test_publish_size_over_limit_denied():
    art = _text_artifact(size=5)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            publish_max_bytes=4,
        )
    )
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "size_over_limit"


def test_publish_size_equal_limit_allowed():
    art = _text_artifact(size=5)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            publish_max_bytes=5,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW


def test_publish_kind_other_denied():
    art = _binary_artifact(kind=ArtifactKind.OTHER, mime="application/octet-stream")
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "kind_not_publishable"


@pytest.mark.parametrize(
    "kind",
    [
        ArtifactKind.DOCUMENT,
        ArtifactKind.MARKDOWN,
        ArtifactKind.CODE,
        ArtifactKind.HTML,
        ArtifactKind.DATA,
        ArtifactKind.CSV,
        ArtifactKind.JSON,
        ArtifactKind.TEXT,
        ArtifactKind.IMAGE,
        ArtifactKind.PDF,
    ],
)
def test_publish_whitelist_accepts_all_except_other(kind):
    art = (
        _text_artifact(kind=kind)
        if kind
        in {
            ArtifactKind.DOCUMENT,
            ArtifactKind.MARKDOWN,
            ArtifactKind.CODE,
            ArtifactKind.HTML,
            ArtifactKind.DATA,
            ArtifactKind.CSV,
            ArtifactKind.JSON,
            ArtifactKind.TEXT,
        }
        else _binary_artifact(kind=kind, classification="public")
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.ALLOW, kind


# ---------------------------------------------------------------------------
# publish idempotency: (artifact_id, current_checksum)
# ---------------------------------------------------------------------------


def test_publish_same_checksum_reuses():
    art = _text_artifact(checksum=_VALID_CHECKSUM)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=_VALID_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reuse is True
    assert d.reason == "publish_reuse"


def test_publish_different_checksum_replaces():
    art = _text_artifact(checksum=_VALID_CHECKSUM)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=_ALT_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reuse is False
    assert d.reason == "publish_replace"


def test_publish_no_active_publish_new():
    art = _text_artifact(checksum=_VALID_CHECKSUM)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=None,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reuse is False
    assert d.reason == "publish_new"


def test_reuse_scoped_to_same_artifact_id():
    """Reuse is keyed on (artifact_id, current_checksum). The Application
    passes ``active_publish_checksum`` for THIS artifact only; a different
    artifact with the same checksum must NOT trigger reuse. The Policy enforces
    this implicitly: it only compares the checksum carried in the request
    (which is scoped to this artifact by the Application). This test documents
    that when ``active_publish_checksum`` is None (no active publish for this
    artifact), reuse is False even though the checksum may match some other
    artifact's publish."""
    art = _text_artifact(checksum=_VALID_CHECKSUM)
    # No active publish for THIS artifact -> new publish, no reuse.
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=None,
        )
    )
    assert d.decision is PolicyOutcome.ALLOW
    assert d.reuse is False


# ---------------------------------------------------------------------------
# binary publish: classification + labels gating (image/pdf)
# ---------------------------------------------------------------------------


def test_binary_image_public_no_sensitive_labels_allowed():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification="public",
        labels=("doc",),
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.ALLOW


def test_binary_pdf_public_no_labels_allowed():
    art = _binary_artifact(
        kind=ArtifactKind.PDF,
        mime="application/pdf",
        classification="public",
        labels=None,
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.ALLOW


def test_binary_image_non_public_denied():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification="internal",
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


def test_binary_image_classification_none_denied():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification=None,
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


def test_binary_image_sensitive_label_denied():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification="public",
        labels=("sensitive",),
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


def test_binary_image_secret_label_denied():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification="public",
        labels=("secret",),
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


def test_binary_pdf_secret_label_denied():
    art = _binary_artifact(
        kind=ArtifactKind.PDF,
        mime="application/pdf",
        classification="public",
        labels=("secret", "v2"),
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


def test_binary_pdf_non_public_denied():
    art = _binary_artifact(
        kind=ArtifactKind.PDF,
        mime="application/pdf",
        classification="confidential",
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"


# ---------------------------------------------------------------------------
# text kinds: no classification/labels gating (delegated to InformationFlowService)
# ---------------------------------------------------------------------------


def test_text_with_secret_label_allowed_by_policy():
    """Text content release/secret-scan is delegated to InformationFlowService
    at the Application layer. Policy only checks size/kind/archived/
    content_available for text kinds."""
    art = _text_artifact(
        kind=ArtifactKind.CODE,
        inline_content="secret = 'abc'",
        size=14,
        classification="confidential",
        labels=("secret",),
    )
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.ALLOW


def test_text_non_public_classification_allowed():
    art = _text_artifact(classification="internal")
    d = ArtifactPolicy().evaluate(_req(art, action=ArtifactPolicyAction.PUBLISH))
    assert d.decision is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# deny precedence over reuse
# ---------------------------------------------------------------------------


def test_archived_denies_even_with_matching_active_publish():
    art = _text_artifact(
        status=ArtifactStatus.ARCHIVED,
        checksum=_VALID_CHECKSUM,
    )
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=_VALID_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "archived"
    assert d.reuse is False


def test_binary_non_public_denies_even_with_matching_active_publish():
    art = _binary_artifact(
        kind=ArtifactKind.IMAGE,
        classification="internal",
        checksum=_VALID_CHECKSUM,
    )
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            active_publish_checksum=_VALID_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "binary_classification_denied"
    assert d.reuse is False


def test_content_unavailable_denies_even_with_matching_active_publish():
    art = _text_artifact(checksum=_VALID_CHECKSUM)
    d = ArtifactPolicy().evaluate(
        _req(
            art,
            action=ArtifactPolicyAction.PUBLISH,
            content_available=False,
            active_publish_checksum=_VALID_CHECKSUM,
        )
    )
    assert d.decision is PolicyOutcome.DENY
    assert d.reason == "content_unavailable"


# ---------------------------------------------------------------------------
# request is frozen
# ---------------------------------------------------------------------------


def test_request_is_frozen():
    art = _text_artifact()
    req = _req(art)
    with pytest.raises(FrozenInstanceError):
        req.action = ArtifactPolicyAction.EDIT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Policy conforms to the Policy Protocol
# ---------------------------------------------------------------------------


def test_artifact_policy_conforms_to_policy_protocol():
    """ArtifactPolicy structurally conforms to the Policy Protocol
    (``evaluate(request, context=None) -> outcome``). ``Policy`` is a
    Protocol without ``@runtime_checkable``, so we verify structural
    conformance (has callable ``evaluate``) rather than using
    ``isinstance``/``issubclass``."""
    p = ArtifactPolicy()
    assert hasattr(p, "evaluate")
    assert callable(p.evaluate)
    # Calling evaluate returns an ArtifactPolicyOutcome.
    art = _text_artifact()
    out = p.evaluate(_req(art, action=ArtifactPolicyAction.EDIT))
    assert isinstance(out, ArtifactPolicyOutcome)
