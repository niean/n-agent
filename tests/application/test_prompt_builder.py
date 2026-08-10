from __future__ import annotations

from pathlib import Path

import pytest

from app.application.prompt_builder import (
    ARTIFACT_GUIDANCE,
    BROWSER_GUIDANCE,
    MANAGED_TOOL_GUIDANCE,
    SKILL_GUIDANCE,
    TASK_GUIDANCE,
    build_system_prompt,
)


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


# ---------------------------------------------------------------------------
# Chat 自然语言审批：Task Delegation 指引契约（T 6）
# ---------------------------------------------------------------------------


def test_task_delegation_guidance_routes_approval_intent_to_tools():
    """当 approve_task/reject_task/revise_task 工具可用、且用户对待批准
    任务表达明确批准/拒绝/修改意图时，应调用对应工具，而不是引导用户
    去 /task 命令或看板。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    assert "approve_task" in text
    assert "reject_task" in text
    assert "revise_task" in text


def test_task_delegation_guidance_task_id_default_and_note_semantics():
    """task_id 可省略，缺省取当前会话最近一个 waiting-approval 任务；
    approve/reject 的 note 可选携带反馈，revise 的 note 必填携带修订指示。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "task_id" in text
    # 缺省 task_id -> 当前会话最近 waiting-approval
    assert "waiting" in lowered and ("latest" in lowered or "current session" in lowered)
    # note 语义
    assert "note" in text
    assert "optional" in lowered
    assert "requires" in lowered or "required" in lowered or "must" in lowered


def test_task_delegation_guidance_progress_question_uses_list_tasks():
    """用户只问任务进度时调 list_tasks，不调审批工具。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "list_tasks" in text
    assert "progress" in lowered


def test_task_delegation_guidance_ambiguous_intent_asks_first():
    """意图含混（如只有"这个不行"无法区分 reject/revise）时先追问，
    不猜测决策。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "ambig" in lowered or "clarif" in lowered or ("ask" in lowered and "first" in lowered)


def test_task_delegation_guidance_forbids_proactive_approval_every_message():
    """不得每条消息主动审批；用户未表达明确决策意图时不调用审批工具。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "proactive" in lowered or "every message" in lowered
    assert "do not" in lowered


def test_task_delegation_guidance_cancel_retry_still_out_of_scope():
    """cancel/retry 仍不用自然语言承载，引导用户用 /task 命令或看板。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "cancel" in lowered
    assert "retry" in lowered
    assert "/task" in text or "kanban" in lowered


def test_task_delegation_guidance_drops_old_approve_reject_natural_language_ban():
    """旧的'Approve, reject, cancel, and retry are not handled via natural
    language'完整条款必须消失；任何仍含 'not handled via natural language'
    的句子不得再含 approve/reject（cancel/retry 可保留）。"""
    import re

    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    lowered = text.lower()
    assert "approve, reject, cancel, and retry are not handled via natural language" not in lowered
    sentences = re.split(r"(?<=[.!?])\s+", lowered)
    for sentence in sentences:
        if "not handled via natural language" in sentence:
            assert "approve" not in sentence, f"approve still paired with NL ban: {sentence!r}"
            assert "reject" not in sentence, f"reject still paired with NL ban: {sentence!r}"


def test_task_delegation_guidance_preserves_create_and_list_guidance():
    """保留既有 create_task 委派指引与 list_tasks 进度查询指引不变。"""
    from app.application.prompt_builder import TASK_DELEGATION_GUIDANCE

    text = TASK_DELEGATION_GUIDANCE
    assert "create_task" in text
    assert "list_tasks" in text
    assert "goal_mode" in text


# ---------------------------------------------------------------------------
# Chat 自然语言审批：Task Guidance worker 三决策语义契约（T 6）
# ---------------------------------------------------------------------------


def test_task_guidance_worker_section_contains_three_decision_semantics():
    """Task Worker 段固定包含 approved/rejected/revised 三种决策语义。"""
    from app.application.prompt_builder import TASK_GUIDANCE

    text = TASK_GUIDANCE
    lowered = text.lower()
    assert "### task worker" in lowered
    assert "approved" in lowered
    assert "rejected" in lowered
    assert "revised" in lowered


def test_task_guidance_worker_revised_treats_note_as_input():
    """revised：把 note 当作本轮调整输入，可按修订后路径工作或重新提案。"""
    from app.application.prompt_builder import TASK_GUIDANCE

    text = TASK_GUIDANCE
    lowered = text.lower()
    assert "revised" in lowered
    assert "note" in lowered
    assert "proposal" in lowered or "path" in lowered


def test_task_guidance_worker_does_not_promise_specific_outcome():
    """指引只约束 worker 解释决策，不承诺模型必然产生用户期待的具体结果。"""
    from app.application.prompt_builder import TASK_GUIDANCE

    text = TASK_GUIDANCE
    lowered = text.lower()
    assert (
        "not promise" in lowered
        or "not guarantee" in lowered
        or "does not promise" in lowered
        or "does not guarantee" in lowered
    )


def test_task_guidance_worker_keeps_task_propose_change_contract():
    """既有 task_propose_change 契约（run ends immediately、WAITING_APPROVAL）
    保持不变。"""
    from app.application.prompt_builder import TASK_GUIDANCE

    text = TASK_GUIDANCE
    assert "task_propose_change" in text
    assert "immediately" in text
    assert "WAITING_APPROVAL" in text


def test_browser_guidance_absent_by_default():
    prompt = build_system_prompt()
    assert "## Browser Guidance" not in prompt


def test_browser_guidance_present_when_provided():
    prompt = build_system_prompt(browser_guidance=BROWSER_GUIDANCE)
    assert prompt.count("## Browser Guidance") == 1
    # key guidance content present
    assert "browser_observe" in prompt
    assert "element_ref" in prompt
    assert "stale_element_ref" in prompt
    assert "sensitive_field_requires_takeover" in prompt
    assert "web_fetch" in prompt


def test_browser_guidance_placed_before_safety():
    prompt = build_system_prompt(browser_guidance=BROWSER_GUIDANCE)
    browser_idx = prompt.index("## Browser Guidance")
    safety_idx = prompt.index("## Safety")
    assert browser_idx < safety_idx


# ---------------------------------------------------------------------------
# T11: Artifact Guidance
# ---------------------------------------------------------------------------


def test_artifact_guidance_omitted_by_default():
    """No artifact_guidance arg -> no Artifact Guidance section in the prompt."""
    prompt = build_system_prompt()
    assert "## Artifact Guidance" not in prompt
    assert ARTIFACT_GUIDANCE not in prompt


def test_artifact_guidance_included_when_provided():
    """artifact_guidance non-empty -> dedicated Artifact Guidance section."""
    prompt = build_system_prompt(artifact_guidance=ARTIFACT_GUIDANCE)
    assert "## Artifact Guidance" in prompt
    assert ARTIFACT_GUIDANCE in prompt
    # appears exactly once (no duplication)
    assert prompt.count("## Artifact Guidance") == 1
    assert prompt.count(ARTIFACT_GUIDANCE) == 1


def test_artifact_guidance_coexists_with_task_guidance():
    """Artifact Guidance and Task Guidance both present and not duplicated."""
    prompt = build_system_prompt(artifact_guidance=ARTIFACT_GUIDANCE)
    assert "## Task Guidance" in prompt
    assert TASK_GUIDANCE in prompt
    assert "## Artifact Guidance" in prompt
    assert ARTIFACT_GUIDANCE in prompt
    # each guidance body appears exactly once
    assert prompt.count(TASK_GUIDANCE) == 1
    assert prompt.count(ARTIFACT_GUIDANCE) == 1


def test_artifact_guidance_coexists_with_browser_guidance():
    """Both dynamic guidance blocks (Browser + Artifact) render together."""
    prompt = build_system_prompt(
        browser_guidance=BROWSER_GUIDANCE,
        artifact_guidance=ARTIFACT_GUIDANCE,
    )
    assert "## Browser Guidance" in prompt
    assert "## Artifact Guidance" in prompt


def test_artifact_guidance_empty_string_omitted():
    """Empty-string artifact_guidance is treated as absent (no empty section)."""
    prompt = build_system_prompt(artifact_guidance="")
    assert "## Artifact Guidance" not in prompt
