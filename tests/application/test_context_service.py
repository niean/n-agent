from app.application.context_service import ContextService
from app.domain.agent import AgentState
from app.domain.tool import ToolExecutionContext
from app.domain.tool_policy import ToolExposurePolicy


class RecordingToolService:
    def __init__(self):
        self.calls = []

    def list_openai_tools(self, risk_level=None, context=None):
        self.calls.append((risk_level, context))
        return [{"type": "function", "function": {"name": "visible"}}]


def _build(policy_marker=None):
    tool_service = RecordingToolService()
    service = ContextService(object(), tool_service=tool_service)
    state = AgentState(
        session_id="s",
        working_messages=[{"role": "user", "content": "hello"}],
    )
    execution_context = ToolExecutionContext(session_id="s")
    options = {"tool_execution_context": execution_context}
    if policy_marker is not None:
        options["tool_exposure_policy"] = policy_marker
    provider_context = service.build_provider_context(state, options)
    return tool_service, options, execution_context, provider_context


def test_context_service_maps_safe_only_to_safe_only_without_leaking_options():
    tool_service, options, execution_context, provider_context = _build("safe_only")

    assert tool_service.calls == [(ToolExposurePolicy.SAFE_ONLY, execution_context)]
    assert options == {
        "tool_exposure_policy": "safe_only",
        "tool_execution_context": execution_context,
    }
    assert provider_context.messages == [{"role": "user", "content": "hello"}]
    assert provider_context.tools[0]["function"]["name"] == "visible"


def test_context_service_maps_all_missing_and_existing_other_values_to_default():
    for marker in ("all", None, "default", "current-other"):
        tool_service, _, execution_context, _ = _build(marker)
        assert tool_service.calls == [(ToolExposurePolicy.DEFAULT, execution_context)]
