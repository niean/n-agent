"""S4: Tests for InformationFlowService and InformationFlowStreamGuard.

Verifies:
- Non-stream redaction via release()
- Cross-chunk secret redaction via StreamGuard
- Transform exception does not leak original text
- Tool event arguments/result structured redaction
"""
from __future__ import annotations

import pytest

from app.application.information_flow_service import (
    InformationFlowService,
    InformationFlowStreamGuard,
    ReleaseResult,
)
from app.application.policy_snapshot import InformationFlowPolicyConfig
from app.domain.information_flow import (
    Classification,
    InformationFlowError,
    ReleaseTarget,
    SecretCatalog,
)
from app.domain.policy import PolicyOutcome


async def _collect(aiter):
    parts: list[str] = []
    async for chunk in aiter:
        parts.append(chunk)
    return "".join(parts)


def _make_service(
    *,
    log_llm_payloads: bool = False,
    store_usage_payloads: bool = True,
    redact_secrets: bool = True,
    secret_values: frozenset[str] | None = None,
) -> InformationFlowService:
    return InformationFlowService(
        InformationFlowPolicyConfig(
            log_llm_payloads=log_llm_payloads,
            store_usage_payloads=store_usage_payloads,
            redact_secrets=redact_secrets,
        ),
        SecretCatalog(secret_values=secret_values or frozenset()),
    )


class TestRelease:
    """Non-stream release tests."""

    def test_release_internal_no_secrets_passes_through(self):
        svc = _make_service()
        result = svc.release("hello", ReleaseTarget.CLIENT_RESPONSE)
        assert result.allowed
        assert result.content == "hello"
        assert result.error is None

    def test_release_with_secret_redacts(self):
        svc = _make_service(secret_values=frozenset({"sk-secret123"}))
        result = svc.release("key=sk-secret123", ReleaseTarget.CLIENT_RESPONSE)
        assert result.allowed
        assert result.content == "key=[REDACTED]"
        assert "sk-secret123" not in result.content

    def test_release_llm_payload_log_default_denies(self):
        svc = _make_service(log_llm_payloads=False)
        result = svc.release("request content", ReleaseTarget.LLM_PAYLOAD_LOG)
        assert not result.allowed
        assert result.content is None
        assert result.error == "information_release_denied"

    def test_release_llm_payload_log_enabled_with_secret_redacts(self):
        svc = _make_service(
            log_llm_payloads=True,
            secret_values=frozenset({"sk-secret123"}),
        )
        result = svc.release("key=sk-secret123", ReleaseTarget.LLM_PAYLOAD_LOG)
        assert result.allowed
        assert result.content == "key=[REDACTED]"

    def test_release_usage_retention_with_secret_redacts(self):
        svc = _make_service(secret_values=frozenset({"sk-secret123"}))
        result = svc.release('{"key":"sk-secret123"}', ReleaseTarget.USAGE_RETENTION)
        assert result.allowed
        assert result.content == '{"key":"[REDACTED]"}'
        assert "sk-secret123" not in result.content

    def test_release_usage_retention_disabled_denies(self):
        svc = _make_service(store_usage_payloads=False)
        result = svc.release("content", ReleaseTarget.USAGE_RETENTION)
        assert not result.allowed
        assert result.content is None

    def test_release_secret_label_no_transform_denies(self):
        svc = _make_service(redact_secrets=False)
        result = svc.release(
            "content",
            ReleaseTarget.CLIENT_RESPONSE,
            labels=frozenset({"secret"}),
        )
        assert not result.allowed
        assert result.content is None
        assert result.error == "information_release_denied"

    def test_release_result_is_frozen(self):
        result = svc_result = _make_service().release("hi", ReleaseTarget.CLIENT_RESPONSE)
        with pytest.raises(Exception):
            result.allowed = False  # type: ignore[misc]


class TestPublicArtifactRelease:
    """PUBLIC_ARTIFACT release through InformationFlowService (Task 4).

    Verifies the application contract: the redacted ReleaseResult.content is
    the ONLY content allowed into a text publish snapshot -- the caller cannot
    copy the original and redact only the response.
    """

    def test_public_artifact_known_secret_redacted_in_content(self):
        """Known secret in content -> release allowed, content redacted.

        The original secret string must NOT appear in ReleaseResult.content.
        """
        svc = _make_service(secret_values=frozenset({"sk-secret123"}))
        result = svc.release(
            "the key is sk-secret123 here",
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.INTERNAL,
            origin="llm_response",
        )
        assert result.allowed
        assert result.content is not None
        assert "sk-secret123" not in result.content
        assert "[REDACTED]" in result.content

    def test_public_artifact_secret_classification_denies_content_none(self):
        """SECRET classification -> DENY, content is None."""
        svc = _make_service()
        result = svc.release(
            "some content",
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.SECRET,
            origin="user",
        )
        assert not result.allowed
        assert result.content is None
        assert result.error == "information_release_denied"

    def test_public_artifact_sensitive_classification_denies_content_none(self):
        """SENSITIVE classification -> DENY, content is None."""
        svc = _make_service()
        result = svc.release(
            "some content",
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.SENSITIVE,
            origin="user",
        )
        assert not result.allowed
        assert result.content is None

    def test_public_artifact_known_secret_without_redaction_denies(self):
        """Known secret + redaction disabled -> DENY, content is None."""
        svc = _make_service(
            redact_secrets=False,
            secret_values=frozenset({"sk-secret123"}),
        )
        result = svc.release(
            "the key is sk-secret123 here",
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.INTERNAL,
            origin="llm_response",
        )
        assert not result.allowed
        assert result.content is None

    def test_public_artifact_no_risk_text_passes_through(self):
        """No-risk PUBLIC text -> allowed, content unchanged."""
        svc = _make_service(secret_values=frozenset({"sk-secret123"}))
        result = svc.release(
            "public announcement",
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.PUBLIC,
            origin="user",
        )
        assert result.allowed
        assert result.content == "public announcement"

    def test_public_artifact_release_content_is_only_content_for_snapshot(self):
        """The release content is the ONLY content allowed into a text publish snapshot.

        A snapshot built from the release must equal ReleaseResult.content exactly.
        The original content still contains the secret, proving that the snapshot
        cannot be built by copying the original and redacting only the response --
        it MUST use the redacted result.content.
        """
        original = "api key sk-secret123 was leaked"
        svc = _make_service(secret_values=frozenset({"sk-secret123"}))
        result = svc.release(
            original,
            ReleaseTarget.PUBLIC_ARTIFACT,
            classification=Classification.INTERNAL,
            origin="llm_response",
        )
        assert result.allowed
        snapshot = result.content
        assert snapshot is not None
        assert "sk-secret123" not in snapshot
        # The original still carries the secret -- any snapshot path that uses
        # `original` (then redacts only the response) would leak it.
        assert "sk-secret123" in original
        assert snapshot != original
        assert snapshot == "api key [REDACTED] was leaked"


class TestStreamGuardCrossChunk:
    """S4: cross-chunk secret redaction via StreamGuard."""

    @pytest.mark.asyncio
    async def test_cross_chunk_secret_redacted(self):
        """Secret 'abcdef' split across chunks must be fully redacted."""
        svc = _make_service(secret_values=frozenset({"abcdef"}))
        guard = svc.create_stream_guard()
        chunks = ["token=abc", "def;done"]
        result = await _collect(guard.transform(chunks))
        assert result == "token=[REDACTED];done"
        assert "abcdef" not in result

    @pytest.mark.asyncio
    async def test_cross_chunk_secret_at_boundary(self):
        """Secret split as (N-1)+1 across two chunks."""
        svc = _make_service(secret_values=frozenset({"secret_value"}))
        guard = svc.create_stream_guard()
        # "secret_valu" (11 chars) + "e" (1 char) = "secret_value" (12 chars)
        chunks = ["prefix_secret_valu", "e_suffix"]
        result = await _collect(guard.transform(chunks))
        assert "secret_value" not in result
        assert "[REDACTED]" in result

    @pytest.mark.asyncio
    async def test_no_secrets_passes_through(self):
        svc = _make_service()
        guard = svc.create_stream_guard()
        chunks = ["hello ", "world"]
        result = await _collect(guard.transform(chunks))
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_multiple_secrets_all_redacted(self):
        svc = _make_service(secret_values=frozenset({"key1", "key2longer"}))
        guard = svc.create_stream_guard()
        chunks = ["data=key1;more=key2longer;end"]
        result = await _collect(guard.transform(chunks))
        assert "key1" not in result
        assert "key2longer" not in result
        assert "[REDACTED]" in result

    @pytest.mark.asyncio
    async def test_empty_chunks(self):
        svc = _make_service(secret_values=frozenset({"secret"}))
        guard = svc.create_stream_guard()
        result = await _collect(guard.transform([]))
        assert result == ""

    @pytest.mark.asyncio
    async def test_single_chunk_with_secret(self):
        svc = _make_service(secret_values=frozenset({"secret"}))
        guard = svc.create_stream_guard()
        result = await _collect(guard.transform(["the secret here"]))
        assert result == "the [REDACTED] here"

    @pytest.mark.asyncio
    async def test_secret_spanning_three_chunks(self):
        """Secret split across 3 chunks should still be caught."""
        svc = _make_service(secret_values=frozenset({"abcdef"}))
        guard = svc.create_stream_guard()
        chunks = ["xxab", "cd", "efxx"]
        result = await _collect(guard.transform(chunks))
        assert "abcdef" not in result
        assert "[REDACTED]" in result

    @pytest.mark.asyncio
    async def test_lookbehind_does_not_drop_normal_text(self):
        """Verify the lookbehind doesn't cause text loss when no secret spans."""
        svc = _make_service(secret_values=frozenset({"abcdef"}))
        guard = svc.create_stream_guard()
        chunks = ["hello", " world", " this", " is", " fine"]
        result = await _collect(guard.transform(chunks))
        assert result == "hello world this is fine"


class TestStreamGuardException:
    """S4: transform exception does not leak original text."""

    @pytest.mark.asyncio
    async def test_transform_exception_no_leak(self):
        """When feed() raises, transform() raises InformationFlowError
        and no unredacted content is yielded."""
        svc = _make_service(secret_values=frozenset({"sensitive_value"}))
        guard = svc.create_stream_guard()

        # Patch feed to raise on a specific chunk
        original_feed = guard.feed

        def faulty_feed(chunk: str) -> str:
            if "trigger" in chunk:
                raise RuntimeError("simulated redaction failure")
            return original_feed(chunk)

        guard.feed = faulty_feed  # type: ignore[assignment]

        chunks = [
            "safe_text_padding_to_exceed_lookbehind_buffer ",
            "trigger_with_sensitive_value",
        ]
        yielded: list[str] = []
        with pytest.raises(InformationFlowError):
            async for chunk in guard.transform(chunks):
                yielded.append(chunk)

        combined = "".join(yielded)
        # The sensitive value must never appear in yielded output
        assert "sensitive_value" not in combined

    @pytest.mark.asyncio
    async def test_transform_exception_stable_error_code(self):
        svc = _make_service()
        guard = svc.create_stream_guard()
        original_feed = guard.feed
        guard.feed = lambda chunk: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        with pytest.raises(InformationFlowError) as exc_info:
            async for _ in guard.transform(["chunk"]):
                pass
        assert exc_info.value.code == "information_release_denied"


class TestStructuredRedaction:
    """S4: tool event arguments/result structured redaction."""

    def test_redact_structured_credential_fields(self):
        svc = _make_service()
        data = {
            "api_key": "sk-123",
            "query": "hello",
            "nested": {"password": "secret", "safe": "ok"},
        }
        result = svc.redact_structured(data)
        assert result["api_key"] == "[REDACTED]"
        assert result["query"] == "hello"
        assert result["nested"]["password"] == "[REDACTED]"
        assert result["nested"]["safe"] == "ok"

    def test_redact_structured_list_of_dicts(self):
        svc = _make_service()
        data = [
            {"token": "abc", "name": "foo"},
            {"token": "def", "name": "bar"},
        ]
        result = svc.redact_structured(data)
        assert result[0]["token"] == "[REDACTED]"
        assert result[0]["name"] == "foo"
        assert result[1]["token"] == "[REDACTED]"

    def test_redact_structured_secret_values_in_strings(self):
        svc = _make_service(secret_values=frozenset({"sk-secret"}))
        data = {"url": "https://api.example.com?key=sk-secret", "name": "test"}
        result = svc.redact_structured(data)
        assert "sk-secret" not in result["url"]
        assert "[REDACTED]" in result["url"]
        assert result["name"] == "test"

    def test_redact_structured_case_insensitive_field_names(self):
        svc = _make_service()
        data = {"API_KEY": "val", "Api_Key": "val", "api_key": "val"}
        result = svc.redact_structured(data)
        assert result["API_KEY"] == "[REDACTED]"
        assert result["Api_Key"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"

    def test_redact_structured_disabled_when_redact_false(self):
        svc = _make_service(redact_secrets=False)
        data = {"api_key": "sk-123"}
        result = svc.redact_structured(data)
        assert result["api_key"] == "sk-123"

    def test_guard_transform_structured(self):
        svc = _make_service(secret_values=frozenset({"sk-secret"}))
        guard = svc.create_stream_guard()
        data = {"api_key": "sk-secret", "args": {"token": "xyz", "query": "hello"}}
        result = guard.transform_structured(data)
        assert result["api_key"] == "[REDACTED]"
        assert result["args"]["token"] == "[REDACTED]"
        assert result["args"]["query"] == "hello"

    def test_redact_structured_non_dict_passthrough(self):
        svc = _make_service(secret_values=frozenset({"sk-secret"}))
        assert svc.redact_structured(42) == 42
        assert svc.redact_structured(None) is None
        assert svc.redact_structured("text with sk-secret") == "text with [REDACTED]"
