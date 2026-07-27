from app.interfaces.http.dashboard import _is_internal_system_prompt_echo


def test_dashboard_rejects_an_echo_of_its_system_prompt() -> None:
    assert _is_internal_system_prompt_echo(
        "## Identity\n\nYou are N-Agent(Niean's Agent), an intelligent, direct, and reliable AI agent.\n\n"
        "## Reasoning & Tools\n\nUse tools when needed."
    )


def test_dashboard_allows_a_user_question_about_identity() -> None:
    assert not _is_internal_system_prompt_echo("`## Identity` 这一节是什么意思？")
