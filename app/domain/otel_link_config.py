"""OtelLinkConfig -- OTel external-link runtime configuration (Domain Layer).

Pure domain: defines the resolved config, per-field overrides, stored view,
resolved view, audit event, and the async Store/Provider/AuditSink ports.
No Settings, SQLite, FastAPI, or infrastructure imports. stdlib only.

A/B/C classification (see .harness/knowledge/03-conventions.md):
  - The 4 link base URLs are C-class runtime-tunable fields. DB stores
    per-field overrides (OtelLinkConfigOverrides); a None field means
    "DB does not override; follow env". "" means the link is disabled.

URL safety: overrides are validated by validate_otel_link_config before
persistence (http/https only, hostname required, no userinfo, no control
chars/whitespace/backslash/fragment, port must parse, length <= 2048).
Audit projections are redacted via _redact_url (scheme://host[:port] only);
raw URLs must never reach logs or error diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

MAX_OTEL_LINK_URL_LENGTH = 2048


class OtelLinkConfigValidationError(Exception):
    """Raised when an OTel link config value fails URL validation."""


class OtelLinkConfigConflictError(Exception):
    """Raised when a CAS save fails (version mismatch / first-write race)."""


class OtelLinkConfigStoreError(Exception):
    """Raised when the store cannot read/parse/serialize the config row."""


@dataclass(frozen=True)
class OtelLinkConfig:
    """Resolved OTel external-link config. Empty string means link disabled."""

    metric_base_url: str = ""
    log_base_url: str = ""
    trace_base_url: str = ""
    profile_base_url: str = ""


# Field names in stable order. Used for overrides merge + overridden_fields.
OTEL_LINK_CONFIG_FIELDS: tuple[str, ...] = (
    "metric_base_url",
    "log_base_url",
    "trace_base_url",
    "profile_base_url",
)


@dataclass(frozen=True)
class OtelLinkConfigOverrides:
    """Per-field DB overrides. None = "follow env"; "" = "disabled"."""

    metric_base_url: str | None = None
    log_base_url: str | None = None
    trace_base_url: str | None = None
    profile_base_url: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Serialize non-None overrides. None fields are omitted (not frozen)."""
        return {
            f: getattr(self, f) for f in OTEL_LINK_CONFIG_FIELDS if getattr(self, f) is not None
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "OtelLinkConfigOverrides":
        """Parse overrides from a JSON object. Strict: unknown keys, non-str
        (except None), or non-dict input raise OtelLinkConfigStoreError."""
        if not isinstance(data, dict):
            raise OtelLinkConfigStoreError("overrides must be a JSON object")
        kwargs: dict[str, str] = {}
        for k, v in data.items():
            if k not in OTEL_LINK_CONFIG_FIELDS:
                raise OtelLinkConfigStoreError(f"unknown override key: {k}")
            if v is None:
                continue  # absent/null both mean "follow env"
            if isinstance(v, bool) or not isinstance(v, str):
                raise OtelLinkConfigStoreError(f"override {k} must be str, got {type(v).__name__}")
            kwargs[k] = v
        return OtelLinkConfigOverrides(**kwargs)

    def overridden_fields(self) -> tuple[str, ...]:
        return tuple(f for f in OTEL_LINK_CONFIG_FIELDS if getattr(self, f) is not None)


@dataclass(frozen=True)
class StoredOtelLinkConfig:
    """What the store persists + reads. version>=1 once a row exists."""

    overrides: OtelLinkConfigOverrides
    version: int
    updated_at: str
    updated_by: str


@dataclass(frozen=True)
class ResolvedOtelLinkConfig:
    """Resolved config + metadata for the management surface.

    version=0 + overridden_fields=() when no DB row exists (first-write CAS);
    the version=0 sentinel is in-memory only and never written to DB.
    """

    config: OtelLinkConfig
    version: int
    overridden_fields: tuple[str, ...] = ()
    updated_at: str | None = None
    updated_by: str | None = None


def _is_forbidden_char(ch: str) -> bool:
    code = ord(ch)
    if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
        return True  # C0 / DEL / C1 control characters
    return ch.isspace() or ch == "\\"


def _validate_single_url(field_name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, str):
        raise OtelLinkConfigValidationError(f"field {field_name} must be str")
    if value == "":
        return  # empty string disables the link
    if len(value) > MAX_OTEL_LINK_URL_LENGTH:
        raise OtelLinkConfigValidationError(
            f"field {field_name} exceeds {MAX_OTEL_LINK_URL_LENGTH} characters"
        )
    if "#" in value:
        raise OtelLinkConfigValidationError(f"field {field_name} must not contain a fragment")
    for ch in value:
        if _is_forbidden_char(ch):
            raise OtelLinkConfigValidationError(
                f"field {field_name} contains whitespace/control/backslash characters"
            )
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise OtelLinkConfigValidationError(
            f"field {field_name} is not a parseable URL"
        ) from exc
    if parsed.scheme not in ("http", "https"):
        raise OtelLinkConfigValidationError(f"field {field_name} must use http or https scheme")
    if parsed.hostname is None:
        raise OtelLinkConfigValidationError(f"field {field_name} must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise OtelLinkConfigValidationError(f"field {field_name} must not contain userinfo")
    try:
        parsed.port  # noqa: B018 -- accessing raises ValueError on invalid port
    except ValueError as exc:
        raise OtelLinkConfigValidationError(
            f"field {field_name} has an invalid port"
        ) from exc


def validate_otel_link_config(
    cfg: OtelLinkConfig, fields: tuple[str, ...] = OTEL_LINK_CONFIG_FIELDS
) -> None:
    """Pure per-field URL validation. Raises OtelLinkConfigValidationError.

    `fields` selects which fields to validate; the service passes
    overridden_fields for stored values so unrelated env-backed fields never
    block an update.
    """
    for f in fields:
        _validate_single_url(f, getattr(cfg, f))


def merge_overrides(base: OtelLinkConfig, overrides: OtelLinkConfigOverrides) -> OtelLinkConfig:
    """Build a resolved OtelLinkConfig from an env base + DB overrides.

    None falls back to env base; "" overrides to disabled; non-empty str wins.
    """
    kwargs: dict[str, str] = {}
    for f in OTEL_LINK_CONFIG_FIELDS:
        ov = getattr(overrides, f)
        kwargs[f] = ov if ov is not None else getattr(base, f)
    return OtelLinkConfig(**kwargs)


def _redact_url(url: str) -> str:
    """Project a URL to scheme://host[:port], dropping path/query/fragment
    (which may carry credentials/tokens). "" -> ""; unparseable -> "<invalid>"."""
    if url == "":
        return ""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return "<invalid>"
    if parsed.scheme == "" or parsed.hostname is None:
        return "<invalid>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"  # urlsplit strips IPv6 brackets; restore them
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


@dataclass(frozen=True)
class OtelLinkConfigFieldState:
    """Audit-safe projection of one field: override state + redacted URL.

    override: "env" (no DB override), "custom" (non-empty override),
    "disabled" (empty-string override). url: redacted projection of the
    resolved URL; kept even for "env" state so the env value's projection
    is not lost.
    """

    override: str
    url: str


def build_otel_link_field_state(
    override_value: str | None, resolved: str
) -> OtelLinkConfigFieldState:
    """Build the audit field state from an override value + resolved URL."""
    if override_value is None:
        state = "env"
    elif override_value == "":
        state = "disabled"
    else:
        state = "custom"
    return OtelLinkConfigFieldState(override=state, url=_redact_url(resolved))


@dataclass(frozen=True)
class OtelLinkConfigAuditEvent:
    """Audit record for a config change. changed_fields maps field name to
    (before_state, after_state) redacted projections -- raw URLs never appear."""

    actor: str
    updated_at: str
    old_version: int
    new_version: int
    changed_fields: dict[str, tuple[OtelLinkConfigFieldState, OtelLinkConfigFieldState]] = field(
        default_factory=dict
    )


class OtelLinkConfigStore(Protocol):
    """Async port for the otel_link_config single-row store."""

    async def get(self) -> StoredOtelLinkConfig | None: ...

    async def save(
        self, overrides: OtelLinkConfigOverrides, expected_version: int, updated_by: str
    ) -> StoredOtelLinkConfig: ...


class OtelLinkConfigProvider(Protocol):
    """Async port: runtime services call current() at use-time for hot-reload."""

    async def current(self) -> OtelLinkConfig: ...


class OtelLinkConfigAuditSink(Protocol):
    """Best-effort async audit sink. Failure must not roll back a committed config."""

    async def record(self, event: OtelLinkConfigAuditEvent) -> None: ...
