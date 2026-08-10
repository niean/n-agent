"""Tests for probe_content_profile pure function.

The probe lives in the Application layer: pure stdlib (csv, json), no
third-party libraries, no IO. It classifies revision content bytes into a
ContentProfile used to route export format selection.
"""
from __future__ import annotations

import pytest

from app.application.artifact_content_profile import probe_content_profile
from app.domain.artifact import ArtifactKind
from app.domain.artifact_exporter import ContentProfile


# ---------------------------------------------------------------------------
# Markdown / document -> MARKDOWN
# ---------------------------------------------------------------------------


def test_markdown_kind_is_markdown_profile():
    profile = probe_content_profile(
        ArtifactKind.MARKDOWN, "text/markdown", b"# Title\n\nparagraph"
    )
    assert profile is ContentProfile.MARKDOWN


def test_document_kind_is_markdown_profile():
    profile = probe_content_profile(
        ArtifactKind.DOCUMENT, "text/plain", b"some document text"
    )
    assert profile is ContentProfile.MARKDOWN


# ---------------------------------------------------------------------------
# Binary kinds -> BINARY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [ArtifactKind.IMAGE, ArtifactKind.PDF, ArtifactKind.OTHER])
def test_binary_kinds_are_binary_profile(kind: ArtifactKind):
    profile = probe_content_profile(kind, "application/octet-stream", b"\x00\x01")
    assert profile is ContentProfile.BINARY


# ---------------------------------------------------------------------------
# CSV -> TABULAR / TEXT
# ---------------------------------------------------------------------------


def test_csv_rectangular_is_tabular():
    data = b"a,b,c\n1,2,3\n4,5,6\n"
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", data) is ContentProfile.TABULAR


def test_csv_single_row_single_col_is_tabular():
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", b"only\n") is ContentProfile.TABULAR


def test_csv_empty_fields_preserved_as_data_is_tabular():
    # empty fields are data, all rows same column count
    data = b"a,b,c\n,,\n,,\n"
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", data) is ContentProfile.TABULAR


def test_csv_ragged_rows_falls_back_to_text():
    data = b"a,b,c\n1,2\n"
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", data) is ContentProfile.TEXT


def test_empty_csv_is_text():
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", b"") is ContentProfile.TEXT


def test_csv_only_header_is_tabular():
    # at least one row, one column, consistent column counts
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", b"header\n") is ContentProfile.TABULAR


def test_csv_quoted_fields_with_commas_is_tabular():
    data = b'name,value\n"hello, world",1\n"foo",2\n'
    assert probe_content_profile(ArtifactKind.CSV, "text/csv", data) is ContentProfile.TABULAR


# ---------------------------------------------------------------------------
# JSON -> TABULAR / TEXT
# ---------------------------------------------------------------------------


def test_json_array_of_objects_consistent_keys_is_tabular():
    data = b'[{"a": 1, "b": 2}, {"a": 3, "b": 4}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TABULAR


def test_json_array_of_arrays_equal_length_is_tabular():
    data = b'[[1, 2], [3, 4]]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TABULAR


def test_json_array_of_objects_null_values_is_tabular():
    data = b'[{"a": 1, "b": null}, {"a": null, "b": 2}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TABULAR


def test_json_empty_array_is_text():
    assert probe_content_profile(ArtifactKind.JSON, "application/json", b"[]") is ContentProfile.TEXT


def test_json_object_not_array_is_text():
    assert probe_content_profile(
        ArtifactKind.JSON, "application/json", b'{"a": 1}'
    ) is ContentProfile.TEXT


def test_json_array_of_objects_inconsistent_keys_is_text():
    data = b'[{"a": 1}, {"b": 2}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


def test_json_array_of_objects_nested_values_is_text():
    data = b'[{"a": {"x": 1}}, {"a": {"x": 2}}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


def test_json_array_of_scalars_is_text():
    data = b'[1, 2, 3]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


def test_json_mixed_array_elements_is_text():
    data = b'[1, "two", {"a": 3}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


def test_json_ragged_array_of_arrays_is_text():
    data = b'[[1, 2], [3]]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


def test_json_invalid_syntax_is_text():
    assert probe_content_profile(
        ArtifactKind.JSON, "application/json", b'{not valid json'
    ) is ContentProfile.TEXT


def test_json_array_of_objects_value_order_uses_first_object_keys():
    # column order follows first object's key order
    data = b'[{"b": 1, "a": 2}, {"b": 3, "a": 4}]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TABULAR


def test_json_array_of_arrays_scalar_strings_is_tabular():
    data = b'[["a", "b"], ["c", "d"]]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TABULAR


def test_json_array_of_arrays_with_null_is_text():
    # null is not a scalar array element for tabular purposes (only
    # array-of-objects allows null values per spec)
    data = b'[[1, null], [2, 3]]'
    assert probe_content_profile(ArtifactKind.JSON, "application/json", data) is ContentProfile.TEXT


# ---------------------------------------------------------------------------
# Other text kinds -> TEXT
# ---------------------------------------------------------------------------


def test_text_kind_is_text_profile():
    assert probe_content_profile(
        ArtifactKind.TEXT, "text/plain", b"hello"
    ) is ContentProfile.TEXT


def test_code_kind_is_text_profile():
    assert probe_content_profile(
        ArtifactKind.CODE, "text/x-python", b"print('hi')"
    ) is ContentProfile.TEXT


def test_html_kind_is_text_profile():
    assert probe_content_profile(
        ArtifactKind.HTML, "text/html", b"<p>hi</p>"
    ) is ContentProfile.TEXT


def test_data_kind_non_tabular_text_is_text_profile():
    # ragged CSV-like content is not tabular, nor valid JSON
    assert probe_content_profile(
        ArtifactKind.DATA, "application/octet-stream", b"a,b\nc\n"
    ) is ContentProfile.TEXT


# ---------------------------------------------------------------------------
# Decode failure -> TEXT / BINARY, never raises
# ---------------------------------------------------------------------------


def test_csv_invalid_utf8_falls_back_to_text():
    # invalid UTF-8 bytes -> decode failure -> TEXT, not an exception
    assert probe_content_profile(
        ArtifactKind.CSV, "text/csv", b"\xff\xfe\x00"
    ) is ContentProfile.TEXT


def test_json_invalid_utf8_falls_back_to_text():
    assert probe_content_profile(
        ArtifactKind.JSON, "application/json", b"\xff\xfe\x00"
    ) is ContentProfile.TEXT


def test_binary_kind_invalid_bytes_is_binary():
    assert probe_content_profile(
        ArtifactKind.IMAGE, "image/png", b"\x89PNG\r\n\x1a\n"
    ) is ContentProfile.BINARY


# ---------------------------------------------------------------------------
# DATA kind with JSON content
# ---------------------------------------------------------------------------


def test_data_kind_with_tabular_json_is_tabular():
    data = b'[{"a": 1, "b": 2}, {"a": 3, "b": 4}]'
    assert probe_content_profile(ArtifactKind.DATA, "application/json", data) is ContentProfile.TABULAR


def test_data_kind_with_csv_content_is_tabular():
    data = b"a,b\n1,2\n"
    assert probe_content_profile(ArtifactKind.DATA, "text/csv", data) is ContentProfile.TABULAR


# ---------------------------------------------------------------------------
# Pure function: no mutation of input
# ---------------------------------------------------------------------------


def test_probe_does_not_mutate_input_bytes():
    data = bytearray(b"a,b\n1,2\n")
    original = bytes(data)
    probe_content_profile(ArtifactKind.CSV, "text/csv", data)
    assert bytes(data) == original
