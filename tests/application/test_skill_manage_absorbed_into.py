from __future__ import annotations

from app.application.skill_service import (
    SkillManageRequestBuilder,
    skill_manage_tool_definition,
)
from app.domain.skill import SkillWriteAction, SkillWriteOrigin


def test_skill_manage_schema_includes_absorbed_into():
    defn = skill_manage_tool_definition()
    assert "absorbed_into" in defn.input_schema["properties"]
    assert defn.input_schema["properties"]["absorbed_into"]["type"] == "string"
    assert defn.input_schema["additionalProperties"] is False


def test_builder_delete_absorbed_into_default_empty():
    req = SkillManageRequestBuilder.delete("old-skill", SkillWriteOrigin.FOREGROUND)
    assert req.action == SkillWriteAction.DELETE
    assert req.absorbed_into == ""
    assert req.name == "old-skill"


def test_builder_delete_absorbed_into_set():
    req = SkillManageRequestBuilder.delete(
        "old-skill", SkillWriteOrigin.BACKGROUND_REVIEW, absorbed_into="umbrella-skill"
    )
    assert req.absorbed_into == "umbrella-skill"
    assert req.origin == SkillWriteOrigin.BACKGROUND_REVIEW
