from __future__ import annotations

from pathlib import Path

import pytest

from app.application.prompt_builder import MANAGED_TOOL_GUIDANCE, SKILL_GUIDANCE, build_system_prompt


def test_managed_tool_guidance_routes_to_skill_view():
    text = MANAGED_TOOL_GUIDANCE
    assert "skill_view" in text
    assert "n-agent" in text
    assert "manage_schedule" in text
    assert text.count("\n") + 1 <= 4


def test_build_system_prompt_includes_managed_tool_guidance():
    assert MANAGED_TOOL_GUIDANCE in build_system_prompt()


def test_skill_guidance_routes_capability_requests_to_skill_discovery():
    text = SKILL_GUIDANCE
    assert "skills_list" in text
    assert "skill_view" in text
    assert "weather" in text
    assert "unavailable" in text
    assert SKILL_GUIDANCE in build_system_prompt()


def test_build_system_prompt_includes_skills_index_when_provided():
    idx = "<available_skills>\n  general:\n    - foo: do foo\n</available_skills>"
    prompt = build_system_prompt(skills_index=idx)
    assert idx in prompt
    assert prompt.index(idx) > prompt.index(SKILL_GUIDANCE)


def test_build_system_prompt_omits_skills_index_when_none():
    prompt = build_system_prompt()
    assert "<available_skills>" not in prompt


def test_build_system_prompt_includes_task_delegation_guidance():
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    prompt = build_system_prompt()
    assert TASK_DELEGATION_GUIDANCE in prompt
    assert "create_task" in prompt
    assert "list_tasks" in prompt


def test_task_delegation_guidance_covers_key_points():
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    # 委派多步/研究/文件产出/长耗时目标
    assert "create_task" in text
    # 查询本会话任务
    assert "list_tasks" in text
    # goal_mode 限定
    assert "goal_mode" in text
    # 管控操作走 /task 或看板
    assert "/task" in text
    # 委派后仅确认、不在同轮自行完成
    assert "confirm" in text.lower() or "one sentence" in text.lower() or "不要" in text
