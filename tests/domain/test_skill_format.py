from __future__ import annotations

import pytest

from app.domain.skill_format import (
    SkillFormatError,
    SkillFormatRequest,
    SkillFormatResult,
    SkillFormatValidator,
    normalize_frontmatter,
)


_VALID_DESCRIPTION = "Demo skill (演示技能). Use when validating format."


def _fm(**overrides) -> dict:
    fm = {"name": "demo", "description": _VALID_DESCRIPTION}
    fm.update(overrides)
    return fm


def _req(
    frontmatter: dict | None = None,
    dir_name: str = "demo",
    body_line_count: int | None = None,
) -> SkillFormatRequest:
    if frontmatter is None:
        frontmatter = _fm()
    return SkillFormatRequest(
        frontmatter=frontmatter,
        dir_name=dir_name,
        body_line_count=body_line_count,
    )


# ---------------------------------------------------------------------------
# valid cases
# ---------------------------------------------------------------------------


def test_valid_minimal():
    result = SkillFormatValidator().validate(_req())
    assert result.valid is True
    assert result.errors == []
    assert result.warnings == []
    assert result.normalized_frontmatter is not None


def test_valid_with_all_whitelist_fields():
    fm = _fm(
        license="MIT",
        **{"allowed-tools": ["tool-a", "tool-b"]},
        metadata={"custom": "value"},
        compatibility=">=1.0",
    )
    result = SkillFormatValidator().validate(_req(fm))
    assert result.valid is True
    assert result.errors == []
    assert result.normalized_frontmatter is not None


# ---------------------------------------------------------------------------
# name validation
# ---------------------------------------------------------------------------


def test_name_required():
    fm = _fm()
    del fm["name"]
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("name" in e for e in result.errors)


@pytest.mark.parametrize(
    "bad_name",
    ["Demo", "demo_tool", "demo--tool", "-demo", "demo-", "demo tool"],
)
def test_name_must_be_kebab_case(bad_name: str):
    fm = _fm(name=bad_name)
    result = SkillFormatValidator().validate(_req(fm, dir_name=bad_name))
    assert not result.valid
    assert any("kebab" in e.lower() for e in result.errors)


def test_name_too_long():
    long_name = "a" * 65
    fm = _fm(name=long_name)
    result = SkillFormatValidator().validate(_req(fm, dir_name=long_name))
    assert not result.valid
    assert any("64" in e for e in result.errors)


def test_name_reserved_anthropic():
    fm = _fm(name="anthropic-helper")
    result = SkillFormatValidator().validate(_req(fm, dir_name="anthropic-helper"))
    assert not result.valid
    assert any("anthropic" in e.lower() for e in result.errors)


def test_name_reserved_claude():
    fm = _fm(name="my-claude-tool")
    result = SkillFormatValidator().validate(_req(fm, dir_name="my-claude-tool"))
    assert not result.valid
    assert any("claude" in e.lower() for e in result.errors)


def test_name_must_match_dir_name():
    fm = _fm(name="demo")
    result = SkillFormatValidator().validate(_req(fm, dir_name="other"))
    assert not result.valid
    assert any("dir" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# description validation
# ---------------------------------------------------------------------------


def test_description_required():
    fm = _fm()
    del fm["description"]
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("description" in e for e in result.errors)


def test_description_too_long():
    fm = _fm(description="A skill (技能). " + "x" * 1020)
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("1024" in e for e in result.errors)


def test_description_no_angle_brackets():
    fm = _fm(description="Demo skill (演示技能). Use when <testing>.")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("bracket" in e.lower() or "<" in e for e in result.errors)


def test_description_must_have_chinese_alias():
    fm = _fm(description="Demo skill. Use when validating format.")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("alias" in e.lower() or "cjk" in e.lower() for e in result.errors)


def test_description_alias_must_have_cjk():
    fm = _fm(description="Demo skill (demo). Use when validating format.")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("alias" in e.lower() or "cjk" in e.lower() for e in result.errors)


def test_description_must_have_english_text():
    fm = _fm(description="演示技能 (演示技能)")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("english" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# top-level field whitelist
# ---------------------------------------------------------------------------


def test_legacy_fields_warn_not_reject():
    fm = _fm(
        version="1.0",
        platforms=["web"],
        tags=["foo"],
        related_skills=["bar"],
        author="someone",
        setup_help="run setup",
        required_env_vars=["API_KEY"],
    )
    result = SkillFormatValidator().validate(_req(fm))
    assert result.valid is True
    assert len(result.warnings) >= 7


def test_unknown_field_error():
    fm = _fm(unknown_field="bad")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("unknown" in e.lower() for e in result.errors)


def test_normalized_populated_even_with_errors():
    fm = _fm(unknown_field="bad")
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert result.normalized_frontmatter is not None
    assert "unknown_field" not in result.normalized_frontmatter


# ---------------------------------------------------------------------------
# metadata validation
# ---------------------------------------------------------------------------


def test_metadata_nested_dict_error():
    fm = _fm(metadata={"nested": {"a": 1}})
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("metadata" in e.lower() for e in result.errors)


def test_metadata_non_string_value_error():
    fm = _fm(metadata={"count": 5})
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("metadata" in e.lower() for e in result.errors)


def test_metadata_list_value_error():
    fm = _fm(metadata={"items": ["a", "b"]})
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("metadata" in e.lower() for e in result.errors)


# ---------------------------------------------------------------------------
# allowed-tools validation
# ---------------------------------------------------------------------------


def test_allowed_tools_list_of_strings():
    fm = _fm(**{"allowed-tools": ["tool-a", "tool-b"]})
    result = SkillFormatValidator().validate(_req(fm))
    assert result.valid is True


def test_allowed_tools_comma_string():
    fm = _fm(**{"allowed-tools": "tool-a, tool-b"})
    result = SkillFormatValidator().validate(_req(fm))
    assert result.valid is True


def test_allowed_tools_list_non_string_element():
    fm = _fm(**{"allowed-tools": ["tool-a", 123]})
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("allowed-tools" in e for e in result.errors)


def test_allowed_tools_wrong_type():
    fm = _fm(**{"allowed-tools": 123})
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("allowed-tools" in e for e in result.errors)


def test_normalized_frontmatter_uses_yaml_key_allowed_tools():
    fm = _fm(**{"allowed-tools": ["tool-a"]})
    result = SkillFormatValidator().validate(_req(fm))
    assert result.valid
    assert "allowed-tools" in result.normalized_frontmatter
    assert "allowed_tools" not in result.normalized_frontmatter


# ---------------------------------------------------------------------------
# compatibility validation
# ---------------------------------------------------------------------------


def test_compatibility_must_be_string():
    fm = _fm(compatibility=123)
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("compatibility" in e.lower() for e in result.errors)


def test_compatibility_too_long():
    fm = _fm(compatibility="x" * 501)
    result = SkillFormatValidator().validate(_req(fm))
    assert not result.valid
    assert any("500" in e for e in result.errors)


# ---------------------------------------------------------------------------
# body_line_count
# ---------------------------------------------------------------------------


def test_body_line_count_warning():
    result = SkillFormatValidator().validate(_req(body_line_count=501))
    assert result.valid is True
    assert any("500" in w or "body" in w.lower() for w in result.warnings)


def test_body_line_count_ok():
    result = SkillFormatValidator().validate(_req(body_line_count=500))
    assert result.valid is True
    assert not any("body" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# frontmatter not dict
# ---------------------------------------------------------------------------


def test_frontmatter_not_dict():
    result = SkillFormatValidator().validate(
        SkillFormatRequest(frontmatter="not a dict", dir_name="demo")
    )
    assert not result.valid
    assert result.normalized_frontmatter is None


# ---------------------------------------------------------------------------
# normalize_frontmatter
# ---------------------------------------------------------------------------


def test_normalize_sinks_legacy_to_metadata():
    fm = _fm(version="1.0", tags=["foo", "bar"], author="someone")
    normalized = normalize_frontmatter(fm)
    assert "version" not in normalized
    assert "tags" not in normalized
    assert "author" not in normalized
    meta = normalized["metadata"]
    assert meta["version"] == "1.0"
    assert meta["tags"] == "foo,bar"
    assert meta["author"] == "someone"


def test_normalize_list_fields_serialized():
    fm = _fm(
        platforms=["web", "mobile"],
        required_env_vars=["A", "B"],
        related_skills=["skill-x"],
    )
    normalized = normalize_frontmatter(fm)
    meta = normalized["metadata"]
    assert meta["platforms"] == "web,mobile"
    assert meta["required_env_vars"] == "A,B"
    assert meta["related_skills"] == "skill-x"


def test_normalize_scalar_to_string():
    fm = _fm(version=1.0, author=42)
    normalized = normalize_frontmatter(fm)
    meta = normalized["metadata"]
    assert meta["version"] == "1.0"
    assert meta["author"] == "42"


def test_normalize_skips_none_valued_legacy_field():
    fm = _fm(version=None, tags=None)
    normalized = normalize_frontmatter(fm)
    assert "metadata" not in normalized or "version" not in normalized.get("metadata", {})
    meta = normalized.get("metadata", {})
    assert "version" not in meta
    assert "tags" not in meta
    assert "None" not in meta.values()


def test_normalize_order_stable():
    fm = _fm(
        license="MIT",
        **{"allowed-tools": ["t"]},
        compatibility=">=1.0",
        metadata={"k": "v"},
        version="1.0",
    )
    normalized = normalize_frontmatter(fm)
    keys = list(normalized.keys())
    expected = ["name", "description", "license", "allowed-tools", "compatibility", "metadata"]
    assert keys == expected


def test_normalize_drops_empty_fields():
    fm = _fm(license="", **{"allowed-tools": []}, compatibility="")
    normalized = normalize_frontmatter(fm)
    assert "license" not in normalized
    assert "allowed-tools" not in normalized
    assert "compatibility" not in normalized


def test_normalize_keeps_existing_metadata():
    fm = _fm(metadata={"custom": "value"}, version="1.0")
    normalized = normalize_frontmatter(fm)
    meta = normalized["metadata"]
    assert meta["custom"] == "value"
    assert meta["version"] == "1.0"


def test_normalize_drops_empty_list_items():
    fm = _fm(tags=["foo", "", "  ", "bar"])
    normalized = normalize_frontmatter(fm)
    assert normalized["metadata"]["tags"] == "foo,bar"


def test_normalize_no_metadata_when_no_legacy():
    fm = _fm(license="MIT")
    normalized = normalize_frontmatter(fm)
    assert "metadata" not in normalized


def test_normalize_raises_for_non_dict():
    with pytest.raises(SkillFormatError):
        normalize_frontmatter("not a dict")  # type: ignore[arg-type]


def test_valid_with_warnings_populates_normalized():
    fm = _fm(version="1.0")
    result = SkillFormatValidator().validate(_req(fm, body_line_count=600))
    assert result.valid is True
    assert result.warnings
    assert result.normalized_frontmatter is not None
    assert result.normalized_frontmatter["metadata"]["version"] == "1.0"
