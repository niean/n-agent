"""Content profile probing for artifact export routing.

Pure function: no third-party libraries, no IO.  Classifies revision
content bytes into a :class:`ContentProfile` (markdown / tabular / text /
binary) used by the Application layer to advertise export capabilities and
by the Infrastructure exporter to route format selection.

Rules (from spec):

- markdown/document -> MARKDOWN
- image/pdf/other -> BINARY
- CSV: parse with stdlib csv.reader; at least one row, one column, and
  consistent column counts across all rows -> TABULAR; otherwise TEXT.
  Empty fields are preserved as data.  Empty CSV -> TEXT.
- JSON: non-empty array of equal-length scalar array-of-arrays, or
  array-of-objects with a consistent key set and all values scalar or
  null (column order from the first object's keys) -> TABULAR.  Empty
  array, mixed elements, nested values, non-array, or arrays-of-arrays
  containing null -> TEXT.
- Other text kinds (code/html/text/data-non-json) -> TEXT.
- Decode/parse failures fall back to TEXT/BINARY; the probe never raises.
"""
from __future__ import annotations

import csv
import json
from io import StringIO

from app.domain.artifact import ArtifactKind
from app.domain.artifact_exporter import ContentProfile

__all__ = ["probe_content_profile"]


# Kinds that are always BINARY (no text-level diff/conversion semantics).
_BINARY_KINDS = frozenset({
    ArtifactKind.IMAGE,
    ArtifactKind.PDF,
    ArtifactKind.OTHER,
})

# Kinds whose content is markdown-structured.
_MARKDOWN_KINDS = frozenset({
    ArtifactKind.MARKDOWN,
    ArtifactKind.DOCUMENT,
})


def probe_content_profile(
    kind: ArtifactKind, mime: str, data: bytes
) -> ContentProfile:
    """Classify revision content bytes into a ContentProfile.

    Pure and total: never raises.  Decode/parse failures degrade to
    TEXT or BINARY rather than propagating exceptions, so a malformed
    payload never yields a 500 from the capabilities path.
    """
    if kind in _BINARY_KINDS:
        return ContentProfile.BINARY

    if kind in _MARKDOWN_KINDS:
        return ContentProfile.MARKDOWN

    if kind is ArtifactKind.CSV:
        return _probe_csv(data)

    if kind is ArtifactKind.JSON:
        return _probe_json(data)

    if kind is ArtifactKind.DATA:
        # DATA may carry JSON or CSV; probe by content structure, fall
        # back to TEXT.  Any other content is plain text.
        return _probe_data(data)

    # CODE / HTML / TEXT and any unknown text kind.
    return ContentProfile.TEXT


# ---------------------------------------------------------------------------
# CSV probing
# ---------------------------------------------------------------------------


def _probe_csv(data: bytes) -> ContentProfile:
    """Return TABULAR if *data* is a rectangular CSV, else TEXT."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ContentProfile.TEXT

    if not text:
        return ContentProfile.TEXT

    try:
        reader = csv.reader(StringIO(text))
        rows = list(reader)
    except csv.Error:
        return ContentProfile.TEXT

    if not rows:
        return ContentProfile.TEXT

    # All rows must be non-empty (at least one column) and the same width.
    width = len(rows[0])
    if width == 0:
        return ContentProfile.TEXT
    for row in rows:
        if len(row) != width:
            return ContentProfile.TEXT
    return ContentProfile.TABULAR


# ---------------------------------------------------------------------------
# JSON probing
# ---------------------------------------------------------------------------


# JSON scalar types allowed in tabular cells.
_JSON_SCALAR_TYPES = frozenset({"str", "int", "float", "bool"})


def _is_json_scalar(value: object) -> bool:
    """True for str/int/float/bool (not None, not dict/list)."""
    return type(value).__name__ in _JSON_SCALAR_TYPES


def _probe_json(data: bytes) -> ContentProfile:
    """Return TABULAR if *data* is a tabular JSON array, else TEXT."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ContentProfile.TEXT

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return ContentProfile.TEXT

    if not isinstance(parsed, list) or not parsed:
        return ContentProfile.TEXT

    # Array-of-arrays: every element a list, equal length, all scalars.
    if all(isinstance(el, list) for el in parsed):
        if not parsed:
            return ContentProfile.TEXT
        width = len(parsed[0])
        if width == 0:
            return ContentProfile.TEXT
        for el in parsed:
            if len(el) != width:
                return ContentProfile.TEXT
            for cell in el:
                if not _is_json_scalar(cell):
                    return ContentProfile.TEXT
        return ContentProfile.TABULAR

    # Array-of-objects: every element a dict, consistent key set, values
    # scalar or null.  Column order follows the first object's keys.
    if all(isinstance(el, dict) for el in parsed):
        first_keys = list(parsed[0].keys())
        first_key_set = set(first_keys)
        if not first_keys:
            return ContentProfile.TEXT
        for el in parsed:
            if set(el.keys()) != first_key_set:
                return ContentProfile.TEXT
            for value in el.values():
                if value is not None and not _is_json_scalar(value):
                    return ContentProfile.TEXT
        return ContentProfile.TABULAR

    # Mixed elements (some lists, some dicts, some scalars).
    return ContentProfile.TEXT


# ---------------------------------------------------------------------------
# DATA kind probing
# ---------------------------------------------------------------------------


def _probe_data(data: bytes) -> ContentProfile:
    """DATA may carry JSON or CSV; probe structure, fall back to TEXT."""
    json_profile = _probe_json(data)
    if json_profile is ContentProfile.TABULAR:
        return ContentProfile.TABULAR
    csv_profile = _probe_csv(data)
    if csv_profile is ContentProfile.TABULAR:
        return ContentProfile.TABULAR
    return ContentProfile.TEXT
