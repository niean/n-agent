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
    idx = "## Available Skills\n\n- general:\n  - foo: do foo"
    prompt = build_system_prompt(skills_index=idx)
    assert idx in prompt
    assert prompt.index(idx) > prompt.index(SKILL_GUIDANCE)


def test_build_system_prompt_omits_skills_index_when_none():
    prompt = build_system_prompt()
    assert "## Available Skills" not in prompt


def test_build_system_prompt_includes_task_delegation_guidance():
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    prompt = build_system_prompt()
    assert TASK_DELEGATION_GUIDANCE in prompt
    assert "create_task" in prompt
    assert "list_tasks" in prompt


def test_build_system_prompt_includes_task_worker_guidance_fixed():
    """TASK_GUIDANCE 作为固定 block 内置于 build_system_prompt（普通对话与 worker 共用），
    避免 worker 运行时追加导致 system prompt 中途变更、LLM prefix cache 失效。"""
    from app.application.prompt_builder import TASK_GUIDANCE

    prompt = build_system_prompt()
    assert TASK_GUIDANCE in prompt
    assert "task_show" in prompt
    assert "task_complete" in prompt
    assert "task_propose_change" in prompt


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


def test_build_system_prompt_uses_unified_section_format():
    """治理：每个静态拼接块统一为 '## <Title>' 标题章节，按固定顺序排列。"""
    prompt = build_system_prompt()
    assert prompt.startswith("## Identity\n")
    titles = [
        "## Identity",
        "## Reasoning & Tools",
        "## Knowledge Base",
        "## Skills",
        "## Managed Resources",
        "## Task Delegation",
        "## Task Guidance",
        "## Safety",
    ]
    pos = -1
    for title in titles:
        assert title in prompt, f"missing section: {title}"
        idx = prompt.index(title)
        assert idx > pos, f"{title} out of order"
        pos = idx
