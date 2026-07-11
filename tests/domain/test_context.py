from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.context import ContextCompressionResult, ContextEngine


def test_context_compression_result_is_frozen():
    result = ContextCompressionResult(
        messages=[{"role": "user", "content": "hi"}],
        summary="summary text",
        compressed=True,
        skipped_reason=None,
        original_tokens=100,
        compressed_tokens=30,
    )
    with pytest.raises(FrozenInstanceError):
        result.compressed = False  # type: ignore[misc]


def test_context_compression_result_defaults():
    result = ContextCompressionResult(
        messages=[],
        summary="",
        compressed=False,
        skipped_reason="below_threshold",
        original_tokens=None,
        compressed_tokens=None,
    )
    assert result.skipped_reason == "below_threshold"
    assert result.original_tokens is None


def test_context_engine_protocol_methods_exist():
    # Protocol methods exist as attributes on the class
    assert hasattr(ContextEngine, "should_compress")
    assert hasattr(ContextEngine, "compress")


def test_context_compression_result_field_names():
    fields = ContextCompressionResult.__dataclass_fields__.keys()
    assert set(fields) == {
        "messages", "summary", "compressed", "skipped_reason",
        "original_tokens", "compressed_tokens", "summarized_message_indices",
    }
