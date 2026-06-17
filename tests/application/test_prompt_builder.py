from __future__ import annotations

from pathlib import Path

import pytest

from app.application.prompt_builder import MANAGED_TOOL_GUIDANCE, build_system_prompt


def test_managed_tool_guidance_routes_to_skill_view():
    text = MANAGED_TOOL_GUIDANCE
    assert "skill_view" in text
    assert "n-agent" in text
    assert "manage_schedule" in text
    assert text.count("\n") + 1 <= 4


def test_build_system_prompt_includes_managed_tool_guidance():
    assert MANAGED_TOOL_GUIDANCE in build_system_prompt()
