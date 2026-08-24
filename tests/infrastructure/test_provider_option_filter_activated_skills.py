"""activated_skills is a CONTROL key: consumed internally, never sent to a
Provider request payload. It must be registered in all three
_INTERNAL_OPTION_KEYS sets (agent_graph, openai_compatible, anthropic_provider)
and must be filtered out by both adapters' option builders."""

from app.infrastructure.llm import anthropic_provider, openai_compatible


def test_activated_skills_registered_in_all_internal_key_sets():
    from app.application.agent_graph import _INTERNAL_OPTION_KEYS as GRAPH
    from app.infrastructure.llm.anthropic_provider import _INTERNAL_OPTION_KEYS as ANT
    from app.infrastructure.llm.openai_compatible import _INTERNAL_OPTION_KEYS as OAI

    for keys in (GRAPH, OAI, ANT):
        assert "activated_skills" in keys


def test_activated_skills_filtered_out_by_both_adapters():
    assert openai_compatible._provider_options(
        {"activated_skills": ["a"], "temperature": 0.1}
    ) == {"temperature": 0.1}

    # claude-sonnet-4-6, not claude-opus-4-7: the latter strips sampling params
    # by an unrelated pre-existing rule, which would mask this assertion.
    anthropic = anthropic_provider._provider_options(
        "claude-sonnet-4-6",
        {"activated_skills": ["a"], "temperature": 0.1},
    )
    assert "activated_skills" not in anthropic
    assert anthropic["temperature"] == 0.1
