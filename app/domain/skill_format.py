from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.skill import SkillFrontmatter


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WHITELIST_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "compatibility",
)

_LEGACY_FIELDS: tuple[str, ...] = (
    "version",
    "platforms",
    "tags",
    "related_skills",
    "author",
    "setup_help",
    "required_env_vars",
)

_RESERVED_WORDS: tuple[str, ...] = ("anthropic", "claude")

_NAME_MAX_LEN = 64
_DESCRIPTION_MAX_LEN = 1024
_COMPATIBILITY_MAX_LEN = 500
_BODY_LINE_WARN_THRESHOLD = 500

# Stable output order for normalized frontmatter.
_NORMALIZE_ORDER: tuple[str, ...] = (
    "name",
    "description",
    "license",
    "allowed-tools",
    "compatibility",
    "metadata",
)

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_PAREN_CJK_PATTERN = re.compile(r"\([^)]*[一-鿿㐀-䶿][^)]*\)")
_PAREN_PATTERN = re.compile(r"\([^)]*\)")
_ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillFormatRequest:
    frontmatter: dict[str, Any]
    dir_name: str
    body_line_count: int | None = None


@dataclass(frozen=True)
class SkillFormatResult:
    valid: bool
    errors: list[str]
    warnings: list[str]
    normalized_frontmatter: dict[str, Any] | None


class SkillFormatError(Exception):
    """Raised when normalize_frontmatter receives a non-mapping input."""


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class SkillFormatValidator:
    """Validate Skill frontmatter against the Anthropic Agent Skills spec.

    Pure domain: no IO, no imports of Application/Infrastructure.
    """

    def validate(self, request: SkillFormatRequest) -> SkillFormatResult:
        errors: list[str] = []
        warnings: list[str] = []
        fm = request.frontmatter

        if not isinstance(fm, dict):
            errors.append("frontmatter must be a mapping")
            return SkillFormatResult(
                valid=False,
                errors=errors,
                warnings=warnings,
                normalized_frontmatter=None,
            )

        self._validate_name(fm, request.dir_name, errors)
        self._validate_description(fm, errors)
        self._validate_top_level_fields(fm, errors, warnings)
        self._validate_metadata(fm, errors)
        self._validate_allowed_tools(fm, errors)
        self._validate_compatibility(fm, errors)
        self._validate_body_line_count(request.body_line_count, warnings)

        normalized = normalize_frontmatter(fm)
        return SkillFormatResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            normalized_frontmatter=normalized,
        )

    # -- field validators --------------------------------------------------

    @staticmethod
    def _validate_name(fm: dict[str, Any], dir_name: str, errors: list[str]) -> None:
        name = fm.get("name")
        if name is None:
            errors.append("name is required")
            return
        if not isinstance(name, str):
            errors.append("name must be a string")
            return
        if not name:
            errors.append("name must not be empty")
            return
        if not _NAME_PATTERN.match(name):
            errors.append(
                f"name must be English kebab-case matching "
                f"^[a-z0-9]+(-[a-z0-9]+)*$, got {name!r}"
            )
        if len(name) > _NAME_MAX_LEN:
            errors.append(f"name must not exceed {_NAME_MAX_LEN} characters")
        for reserved in _RESERVED_WORDS:
            # Substring match is intentional -- strict anti-impersonation
            # so names like "reclaude" or "anthropic-helper" are rejected.
            if reserved in name.lower():
                errors.append(f"name must not contain reserved word {reserved!r}")
        if name != dir_name:
            errors.append(
                f"name must match dir_name: name={name!r} dir_name={dir_name!r}"
            )

    @staticmethod
    def _validate_description(fm: dict[str, Any], errors: list[str]) -> None:
        desc = fm.get("description")
        if desc is None:
            errors.append("description is required")
            return
        if not isinstance(desc, str):
            errors.append("description must be a string")
            return
        if not desc:
            errors.append("description must not be empty")
            return
        if len(desc) > _DESCRIPTION_MAX_LEN:
            errors.append(
                f"description must not exceed {_DESCRIPTION_MAX_LEN} characters"
            )
        if "<" in desc or ">" in desc:
            errors.append("description must not contain angle brackets < >")
        if not _PAREN_CJK_PATTERN.search(desc):
            errors.append(
                "description must contain a parenthesized Chinese alias "
                "with at least one CJK character"
            )
        else:
            without_parens = _PAREN_PATTERN.sub("", desc)
            if not _ASCII_LETTER_PATTERN.search(without_parens):
                errors.append(
                    "description must contain English usage text "
                    "outside the Chinese alias"
                )

    @staticmethod
    def _validate_top_level_fields(
        fm: dict[str, Any], errors: list[str], warnings: list[str]
    ) -> None:
        for key in fm:
            if key in _WHITELIST_FIELDS:
                continue
            if key in _LEGACY_FIELDS:
                warnings.append(
                    f"legacy top-level field {key!r} will be normalized "
                    f"into metadata"
                )
            else:
                errors.append(f"unknown top-level field {key!r}")

    @staticmethod
    def _validate_metadata(fm: dict[str, Any], errors: list[str]) -> None:
        meta = fm.get("metadata")
        if meta is None:
            return
        if not isinstance(meta, dict):
            errors.append("metadata must be a string->string mapping")
            return
        for k, v in meta.items():
            if not isinstance(v, str):
                errors.append(
                    f"metadata value for {k!r} must be a string, "
                    f"got {type(v).__name__}"
                )

    @staticmethod
    def _validate_allowed_tools(fm: dict[str, Any], errors: list[str]) -> None:
        at = fm.get("allowed-tools")
        if at is None:
            return
        if isinstance(at, str):
            return
        if isinstance(at, list):
            for item in at:
                if not isinstance(item, str):
                    errors.append(
                        f"allowed-tools list must contain only strings, "
                        f"got {type(item).__name__}"
                    )
            return
        errors.append(
            f"allowed-tools must be list[str] or comma-separated string, "
            f"got {type(at).__name__}"
        )

    @staticmethod
    def _validate_compatibility(fm: dict[str, Any], errors: list[str]) -> None:
        comp = fm.get("compatibility")
        if comp is None:
            return
        if not isinstance(comp, str):
            errors.append("compatibility must be a string")
            return
        if len(comp) > _COMPATIBILITY_MAX_LEN:
            errors.append(
                f"compatibility must not exceed {_COMPATIBILITY_MAX_LEN} characters"
            )

    @staticmethod
    def _validate_body_line_count(
        body_line_count: int | None, warnings: list[str]
    ) -> None:
        if body_line_count is not None and body_line_count > _BODY_LINE_WARN_THRESHOLD:
            warnings.append(
                f"body has {body_line_count} lines (>{_BODY_LINE_WARN_THRESHOLD}), "
                f"consider splitting into references/scripts/assets "
                f"for progressive disclosure"
            )


# ---------------------------------------------------------------------------
# Pure normalize function (reused by Infrastructure)
# ---------------------------------------------------------------------------


def normalize_frontmatter(frontmatter: Mapping[str, Any]) -> dict[str, Any]:
    """Sink legacy extension fields into metadata and produce a clean
    frontmatter with only whitelisted top-level keys in stable order.

    Raises SkillFormatError if *frontmatter* is not a mapping.
    """
    if not isinstance(frontmatter, Mapping):
        raise SkillFormatError("frontmatter must be a mapping")

    # Build metadata: existing metadata entries + sunk legacy fields.
    metadata: dict[str, str] = {}
    existing_meta = frontmatter.get("metadata")
    if isinstance(existing_meta, dict):
        for k, v in existing_meta.items():
            metadata[k] = v if isinstance(v, str) else str(v)

    for legacy_key in _LEGACY_FIELDS:
        if legacy_key not in frontmatter:
            continue
        value = frontmatter[legacy_key]
        if value is None:
            continue
        if isinstance(value, list):
            items: list[str] = []
            for item in value:
                s = item.strip() if isinstance(item, str) else str(item).strip()
                if s:
                    items.append(s)
            serialized = ",".join(items)
        elif isinstance(value, str):
            serialized = value.strip()
        else:
            serialized = str(value).strip()
        if serialized:
            metadata[legacy_key] = serialized

    result: dict[str, Any] = {}
    for key in _NORMALIZE_ORDER:
        if key == "metadata":
            if metadata:
                result[key] = metadata
            continue
        if key not in frontmatter:
            continue
        value = frontmatter[key]
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, list) and len(value) == 0:
            continue
        if isinstance(value, dict) and len(value) == 0:
            continue
        result[key] = value

    return result


# ---------------------------------------------------------------------------
# Metadata helpers (pure, shared with Infrastructure)
# ---------------------------------------------------------------------------


def deserialize_metadata_list(val: Any) -> list[str]:
    """Deserialize a metadata value into a list[str].

    - list -> stringify each item, trim, drop empties
    - str  -> split on comma, trim, drop empties (no escape support)
    - other scalar -> stringify, trim, single-element list (or empty)
    - None -> []
    """
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    if val is None:
        return []
    s = str(val).strip()
    return [s] if s else []


def metadata_get(
    raw: dict[str, Any], key: str, *, is_list: bool = False
) -> Any:
    """Read an extension field metadata-first with top-level legacy fallback.

    Returns ``[]`` (is_list) or ``""`` (scalar) when the key is absent from
    both ``metadata`` and the top level.
    """
    md = raw.get("metadata")
    if isinstance(md, dict) and key in md:
        val = md[key]
    elif key in raw:
        val = raw[key]
    else:
        return [] if is_list else ""
    if is_list:
        return deserialize_metadata_list(val)
    return val


# ---------------------------------------------------------------------------
# SkillFrontmatter construction from a raw dict (metadata-aware)
# ---------------------------------------------------------------------------


def skill_frontmatter_from_dict(
    raw: dict[str, Any], fallback_name: str, platforms: list[str]
) -> SkillFrontmatter:
    """Build a SkillFrontmatter from a raw frontmatter dict.

    Reads extension fields metadata-first with top-level legacy fallback.
    Stores ``normalize_frontmatter(raw)`` as ``raw`` so that legacy fields
    are sunk into metadata. Defensive: normalize should never fail on a
    dict, but a scan must not crash on a weird-but-valid dict.
    """
    version = str(metadata_get(raw, "version") or "")
    tags = metadata_get(raw, "tags", is_list=True)
    related_skills = metadata_get(raw, "related_skills", is_list=True)
    author = str(metadata_get(raw, "author") or "")
    setup_help_val = metadata_get(raw, "setup_help")
    # Empty string (including missing -> "") becomes None.
    setup_help = str(setup_help_val) if setup_help_val else None
    required_env_vars = metadata_get(raw, "required_env_vars", is_list=True)

    # allowed-tools: top-level whitelist field; list[str] or comma string.
    allowed_tools = deserialize_metadata_list(raw.get("allowed-tools"))
    # compatibility: top-level whitelist field.
    compatibility = str(raw.get("compatibility") or "")

    # raw: store normalized frontmatter (legacy sunk into metadata).
    try:
        normalized_raw = normalize_frontmatter(raw)
    except Exception:
        normalized_raw = raw
    normalized_metadata = (
        dict(normalized_raw["metadata"])
        if isinstance(normalized_raw.get("metadata"), dict)
        else {}
    )

    return SkillFrontmatter(
        name=str(raw.get("name") or fallback_name),
        description=str(raw.get("description") or ""),
        version=version,
        platforms=list(platforms),
        tags=tags,
        related_skills=related_skills,
        author=author,
        license=str(raw.get("license") or ""),
        setup_help=setup_help,
        required_env_vars=required_env_vars,
        raw=normalized_raw,
        metadata=normalized_metadata,
        compatibility=compatibility,
        allowed_tools=allowed_tools,
    )
