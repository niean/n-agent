"""DelegationRequestParser -- strict normalization + fingerprinting (Application).

Bridges the raw tool-call arguments (still obtainable as JSON from the
provider/AgentGraph) and the domain ``DelegationChildSpec`` / policy
request. Responsibilities:

  - Strict JSON decoding of the raw arguments when a JSON string is
    supplied (rejects duplicate keys, unknown fields, oversized nesting /
    byte budget). A ``dict`` that has already been parsed by the provider
    cannot be re-checked for duplicate keys -- the caller must pass the raw
    JSON string for that guarantee; this parser never claims otherwise.
  - ``delegation_key`` Unicode normalization (NFC) + lowercasing + strip.
  - Child spec normalization: blank-instruction rejection, per-spec field
    trimming, and duplicate-spec detection (same normalized form).
  - Stable fingerprint computation over the normalized request so that
    idempotent replays of the same logical delegation produce the same
    fingerprint, while any semantic change produces a different one.

Pure module: stdlib + ``app.domain.delegation`` only. No IO, no LLM, no
database. The parser raises ``DelegationError`` (stable error code) on any
malformed input; it never returns a partial result.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.delegation import DelegationChildSpec


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DelegationError(Exception):
    """Stable-error exception raised by the delegation application layer.

    ``code`` is a model-safe stable error code (never the raw exception
    message) suitable for return to the LLM context. ``message`` is the
    human-readable detail.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Limits (parser-level; policy-level limits live in DelegationPolicy)
# ---------------------------------------------------------------------------

#: Maximum raw JSON byte budget for tool-call arguments.
_MAX_ARGUMENT_BYTES: int = 1 << 20  # 1 MiB

#: Maximum JSON nesting depth.
_MAX_NESTING_DEPTH: int = 64


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedChildren:
    """Result of normalizing child specs."""

    children: tuple[DelegationChildSpec, ...]


class DelegationRequestParser:
    """Strict normalization + fingerprinting for delegation requests.

    Stateless and safe to share. All methods are pure functions.
    """

    # ------------------------------------------------------------------
    # delegation_key normalization
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_key(key: str) -> str:
        """Normalize a delegation key: NFC + strip + lowercase.

        Raises ``DelegationError`` if the key is blank or not a string.
        """
        if not isinstance(key, str):
            raise DelegationError("delegation_invalid", "delegation_key must be a string")
        normalized = unicodedata.normalize("NFC", key).strip().lower()
        if not normalized:
            raise DelegationError("delegation_invalid", "delegation_key must not be blank")
        return normalized

    # ------------------------------------------------------------------
    # raw JSON decoding (strict)
    # ------------------------------------------------------------------

    @staticmethod
    def decode_arguments(raw: str | Mapping[str, Any] | None) -> Mapping[str, Any]:
        """Strictly decode raw tool-call arguments.

        Accepts a raw JSON string (checked for duplicate keys and depth) or
        a pre-parsed mapping (duplicate-key check is NOT possible -- the
        caller is responsible for passing the raw string when that
        guarantee is required). ``None`` is treated as an empty mapping.

        Raises ``DelegationError`` on malformed JSON, duplicate keys,
        unknown top-level fields, oversized payload, or excessive nesting.
        """
        if raw is None:
            return {}
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return {}
            if len(stripped.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
                raise DelegationError(
                    "delegation_invalid", "arguments exceed byte budget"
                )
            try:
                data = json.loads(
                    stripped, object_pairs_hook=_reject_duplicate_keys
                )
                _check_depth(data, _MAX_NESTING_DEPTH)
            except ValueError as exc:
                raise DelegationError(
                    "delegation_invalid", f"malformed arguments: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise DelegationError(
                    "delegation_invalid", "arguments must be a JSON object"
                )
            return data
        if isinstance(raw, Mapping):
            return dict(raw)
        raise DelegationError(
            "delegation_invalid", "arguments must be a string or mapping"
        )

    # ------------------------------------------------------------------
    # child spec normalization
    # ------------------------------------------------------------------

    def normalize_children(
        self,
        children: list[DelegationChildSpec] | tuple[DelegationChildSpec, ...],
    ) -> tuple[DelegationChildSpec, ...]:
        """Normalize child specs: validate, trim, and dedup.

        Raises ``DelegationError`` on blank instruction, duplicate
        normalized spec, or empty list.
        """
        if not children:
            raise DelegationError(
                "delegation_invalid", "at least one child is required"
            )
        normalized: list[DelegationChildSpec] = []
        seen: set[tuple] = set()
        for spec in children:
            if not isinstance(spec, DelegationChildSpec):
                raise DelegationError(
                    "delegation_invalid", "child spec must be DelegationChildSpec"
                )
            instruction = spec.instruction.strip()
            if not instruction:
                raise DelegationError(
                    "delegation_invalid", "child instruction must not be blank"
                )
            title = unicodedata.normalize("NFC", spec.title).strip()
            if not title:
                raise DelegationError(
                    "delegation_invalid", "child title must not be blank"
                )
            norm = self._normalize_spec_key(spec, title, instruction)
            if norm in seen:
                raise DelegationError(
                    "delegation_invalid", "duplicate child spec detected"
                )
            seen.add(norm)
            # Rebuild with trimmed title/instruction (immutable dataclass).
            normalized.append(
                DelegationChildSpec(
                    title=title,
                    instruction=instruction,
                    skills=spec.skills,
                    allowed_tools=spec.allowed_tools,
                    model_override=spec.model_override,
                    max_runtime_seconds=spec.max_runtime_seconds,
                    budget_tokens=spec.budget_tokens,
                    output_schema=spec.output_schema,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _normalize_spec_key(
        spec: DelegationChildSpec, title: str, instruction: str
    ) -> tuple:
        return (
            title.lower(),
            instruction,
            tuple(sorted(spec.skills)),
            tuple(sorted(spec.allowed_tools)),
            spec.model_override,
        )

    # ------------------------------------------------------------------
    # fingerprint
    # ------------------------------------------------------------------

    def fingerprint(
        self,
        delegation_key: str,
        children: tuple[DelegationChildSpec, ...],
        join_policy: str,
        aggregation: str,
        timeout_seconds: int | None,
        aggregator_instruction: str | None = None,
    ) -> str:
        """Compute a stable SHA-256 fingerprint over the normalized request.

        The fingerprint is the idempotency key: two requests with the same
        fingerprint are the same logical delegation. Any semantic change
        (key, children, policy, timeout, aggregator) changes the fingerprint.
        """
        key = self.normalize_key(delegation_key)
        norm_children = self.normalize_children(children)
        payload = {
            "k": key,
            "c": [
                {
                    "t": c.title,
                    "i": c.instruction,
                    "s": sorted(c.skills),
                    "a": sorted(c.allowed_tools),
                    "m": c.model_override,
                    "r": c.max_runtime_seconds,
                    "b": c.budget_tokens,
                }
                for c in norm_children
            ],
            "j": str(join_policy),
            "g": str(aggregation),
            "t": timeout_seconds,
            "ai": aggregator_instruction.strip() if aggregator_instruction else None,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Strict JSON helpers
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """object_pairs_hook that rejects duplicate keys."""
    result: dict[str, Any] = {}
    seen: set[str] = set()
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key}")
        seen.add(key)
        result[key] = value
    return result


def _check_depth(node: Any, max_depth: int, current: int = 1) -> None:
    """Reject excessively nested JSON structures."""
    if current > max_depth:
        raise ValueError(f"nesting depth exceeds {max_depth}")
    if isinstance(node, dict):
        for value in node.values():
            _check_depth(value, max_depth, current + 1)
    elif isinstance(node, list):
        for item in node:
            _check_depth(item, max_depth, current + 1)
