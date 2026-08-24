import logging

import pytest

from app.application.context_service import ContextService
from app.domain.agent import AgentState
from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class _FakeSkill:
    """Minimal stand-in for app.domain.skill.Skill with only the `name` attribute used by T2."""

    def __init__(self, name):
        self.name = name


class _FakeSkillService:
    """Fake skill_service implementing both build_skills_index() and list_for_llm()."""

    def __init__(
        self,
        *,
        index_text: str = "## Available Skills\n\n- general:\n  - a: skill a\n  - b: skill b",
        live_skills: list | None = None,
        chat_selectable_skills: list | None = None,
        raise_on_list: bool = False,
        return_none: bool = False,
    ):
        self._index_text = index_text
        self._live_skills = live_skills
        # chat_selectable defaults to live_skills (i.e. nothing hidden) so
        # pre-existing tests keep their original semantics; tests that need
        # to exercise the new filter supply an explicit list.
        self._chat_selectable_skills = (
            chat_selectable_skills
            if chat_selectable_skills is not None
            else list(live_skills) if live_skills is not None else None
        )
        self._raise_on_list = raise_on_list
        self._return_none = return_none

    async def build_skills_index(self) -> str:
        return self._index_text

    async def list_for_llm(self):
        if self._raise_on_list:
            raise RuntimeError("simulated failure")
        if self._return_none:
            return None
        return list(self._live_skills) if self._live_skills is not None else []

    async def list_chat_selectable(self):
        if self._raise_on_list:
            raise RuntimeError("simulated failure")
        if self._return_none:
            return None
        return list(self._chat_selectable_skills) if self._chat_selectable_skills is not None else []


async def _new_state(tmp_path, *, activated_skills):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.append_message("s1", ConversationMessage(role="user", content="q1"))
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "next"}],
    )
    if activated_skills is not None:
        state.run_options["activated_skills"] = activated_skills
    return store, state


def _system_prompt(state) -> str:
    return state.working_messages[0]["content"]


def _activated_section(prompt: str) -> str:
    """Slice out the `## Activated Skills` block (until the next `## ` heading or end).

    `## Available Skills` already mentions activated names, so searching the whole prompt
    for `` `b` ``/`` `a` `` would yield false positives; this helper scopes the search.
    """
    start = prompt.index("## Activated Skills")
    rest = prompt[start + len("## Activated Skills"):]
    next_heading = rest.find("\n## ")
    if next_heading == -1:
        return prompt[start:]
    return prompt[start : start + len("## Activated Skills") + next_heading]


@pytest.mark.asyncio
async def test_build_context_state_without_activated_skills_omits_section(tmp_path):
    """Scenario 1: no run_options['activated_skills'] -> no `## Activated Skills` section."""
    store, state = await _new_state(tmp_path, activated_skills=None)
    fake = _FakeSkillService(live_skills=[_FakeSkill("a"), _FakeSkill("b")])
    svc = ContextService(store, skill_service=fake)

    state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt


@pytest.mark.asyncio
async def test_build_context_state_intersects_with_live_skills_preserving_user_order(tmp_path):
    """Scenario 2: user order preserved; only live names kept; ordering vs Available Skills."""
    store, state = await _new_state(tmp_path, activated_skills=["b", "a", "zzz"])
    fake = _FakeSkillService(live_skills=[_FakeSkill("a"), _FakeSkill("b")])
    svc = ContextService(store, skill_service=fake)

    state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" in prompt
    section = _activated_section(prompt)
    # 用户顺序保留：b 在 a 之前
    assert section.index("`b`") < section.index("`a`")
    # 失效名称 zzz 不出现
    assert "zzz" not in section
    # Available Skills 在 Activated Skills 之前
    assert prompt.index("## Available Skills") < prompt.index("## Activated Skills")


@pytest.mark.asyncio
async def test_build_context_state_degrades_when_list_for_llm_raises(tmp_path, caplog):
    """Scenario 3: list_chat_selectable raises -> no section, no exception, no warning (caught silently)."""
    store, state = await _new_state(tmp_path, activated_skills=["a"])
    fake = _FakeSkillService(raise_on_list=True)
    svc = ContextService(store, skill_service=fake)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt


@pytest.mark.asyncio
async def test_build_context_state_degrades_when_list_for_llm_returns_none(tmp_path, caplog):
    """Scenario 4: list_chat_selectable returns None -> no section, warning logged."""
    store, state = await _new_state(tmp_path, activated_skills=["a"])
    fake = _FakeSkillService(return_none=True)
    svc = ContextService(store, skill_service=fake)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt
    assert any(
        "list_chat_selectable returned None" in record.getMessage()
        for record in caplog.records
        if record.name == "app.application.context_service"
    )


@pytest.mark.asyncio
async def test_build_context_state_rejects_string_activated_skills(tmp_path, caplog):
    """Scenario 5a: run_options['activated_skills'] is a string -> degrade, no char iteration."""
    store, state = await _new_state(tmp_path, activated_skills="foo")
    fake = _FakeSkillService(live_skills=[_FakeSkill("foo")])
    svc = ContextService(store, skill_service=fake)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt
    assert any(
        "not a list" in record.getMessage()
        for record in caplog.records
        if record.name == "app.application.context_service"
    )


@pytest.mark.asyncio
async def test_build_context_state_rejects_dict_activated_skills(tmp_path, caplog):
    """Scenario 5b: run_options['activated_skills'] is a dict -> degrade, no key iteration."""
    store, state = await _new_state(tmp_path, activated_skills={"a": 1})
    fake = _FakeSkillService(live_skills=[_FakeSkill("a"), _FakeSkill("1")])
    svc = ContextService(store, skill_service=fake)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt
    assert any(
        "not a list" in record.getMessage()
        for record in caplog.records
        if record.name == "app.application.context_service"
    )


@pytest.mark.asyncio
async def test_build_context_state_warns_when_skill_service_is_none(tmp_path, caplog):
    """Scenario 6: skill_service is None -> no section, warning logged."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    state = AgentState(
        session_id="s1",
        input_messages=[{"role": "user", "content": "next"}],
    )
    state.run_options["activated_skills"] = ["a"]
    svc = ContextService(store, skill_service=None)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" not in prompt
    assert any(
        "skill_service unavailable" in record.getMessage()
        for record in caplog.records
        if record.name == "app.application.context_service"
    )


@pytest.mark.asyncio
async def test_build_context_state_ignores_invalid_live_entries_but_keeps_valid(tmp_path, caplog):
    """Scenario 7: bad live entries logged once with N, valid entries retained."""
    store, state = await _new_state(tmp_path, activated_skills=["a", "b"])
    bad_skill = _FakeSkill("a")
    bad_skill.name = None  # 显式制造 None
    bad_object = object()  # 缺少 name 属性
    live = [
        bad_skill,
        bad_object,
        _FakeSkill(""),       # 空字符串
        _FakeSkill("   "),    # 仅空白
        _FakeSkill("a"),      # 合法
    ]
    fake = _FakeSkillService(live_skills=live)
    svc = ContextService(store, skill_service=fake)

    with caplog.at_level(logging.WARNING, logger="app.application.context_service"):
        state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    assert "## Activated Skills" in prompt
    section = _activated_section(prompt)
    # 合法条目 a 保留，b 因为不在 live 集合中所以被过滤
    assert "`a`" in section
    assert "`b`" not in section
    # 警告合并为一行，N == 4（None + 缺 name + 空串 + 空白）
    invalid_logs = [
        record for record in caplog.records
        if record.name == "app.application.context_service"
        and "without a valid name" in record.getMessage()
    ]
    assert len(invalid_logs) == 1
    assert "4" in invalid_logs[0].getMessage()


@pytest.mark.asyncio
async def test_build_context_state_uses_chat_selectable_for_filter(tmp_path):
    """Available Skills keeps the LLM-facing index (hidden still listed); Activated Skills drops hidden."""
    store, state = await _new_state(tmp_path, activated_skills=["visible", "hidden"])
    fake = _FakeSkillService(
        index_text="## Available Skills\n\n- general:\n  - visible: x\n  - hidden: y",
        live_skills=[_FakeSkill("visible"), _FakeSkill("hidden")],
        chat_selectable_skills=[_FakeSkill("visible")],  # only visible in chat
    )
    svc = ContextService(store, skill_service=fake)

    state = await svc.build_context_state(state)

    prompt = _system_prompt(state)
    # Available Skills still lists both names (LLM-facing index unchanged)
    assert "## Available Skills" in prompt
    assert "visible" in prompt
    assert "hidden" in prompt
    # Activated Skills only includes the chat-selectable one
    section = _activated_section(prompt)
    assert "`visible`" in section
    assert "`hidden`" not in section
