import json
import pytest
from app.infrastructure.memory.external.holographic import HolographicAdapter


@pytest.fixture
def adapter(tmp_path):
    a = HolographicAdapter(
        config={
            "db_path": str(tmp_path / "holo.db"),
            "default_trust": 0.5,
            "min_trust_threshold": 0.3,
            "temporal_decay_half_life": 0,
            "auto_extract": False,
        }
    )
    a.initialize(session_id="s1", project_root=".")
    return a


def test_name_and_available(adapter):
    assert adapter.name == "holographic"
    assert adapter.is_available() is True


def test_prefetch_returns_matching_fact(adapter):
    adapter.handle_tool_call("fact_store", {"action": "add", "content": "user prefers python", "category": "user_pref"})
    result = adapter.prefetch("python", session_id="s1")
    assert "python" in result


def test_fact_store_add_and_search(adapter):
    adapter.handle_tool_call("fact_store", {"action": "add", "content": "uses vim", "category": "tool"})
    parsed = json.loads(adapter.handle_tool_call("fact_store", {"action": "search", "query": "vim"}))
    assert parsed["success"] is True
    assert parsed["count"] >= 1


def test_fact_feedback_helpful_increases_trust(adapter):
    add = json.loads(adapter.handle_tool_call("fact_store", {"action": "add", "content": "fact x", "category": "general"}))
    fact_id = add["fact_id"]
    before = json.loads(adapter.handle_tool_call("fact_store", {"action": "list"}))["facts"][0]["trust_score"]
    adapter.handle_tool_call("fact_feedback", {"action": "helpful", "fact_id": fact_id})
    after = json.loads(adapter.handle_tool_call("fact_store", {"action": "list"}))["facts"][0]["trust_score"]
    assert after > before


def test_sync_turn_auto_extract(tmp_path):
    a = HolographicAdapter(config={
        "db_path": str(tmp_path / "holo.db"), "default_trust": 0.5,
        "min_trust_threshold": 0.3, "temporal_decay_half_life": 0, "auto_extract": True,
    })
    a.initialize(session_id="s1", project_root=".")
    a.sync_turn("I prefer python and use vim daily", "noted", session_id="s1")
    facts = json.loads(a.handle_tool_call("fact_store", {"action": "list"}))["facts"]
    assert len(facts) >= 1


def test_on_session_end_noop(adapter):
    adapter.on_session_end("s1")  # 不抛异常即可


def test_recall_mode_default_hybrid(tmp_path):
    a = HolographicAdapter(config={"db_path": str(tmp_path / "h.db")})
    assert a._recall_mode == "hybrid"


def test_recall_mode_invalid_falls_back_to_hybrid(tmp_path):
    a = HolographicAdapter(config={"db_path": str(tmp_path / "h.db"), "recall_mode": "none"})
    assert a._recall_mode == "hybrid"


def test_recall_mode_explicit_context(tmp_path):
    a = HolographicAdapter(config={"db_path": str(tmp_path / "h.db"), "recall_mode": "context"})
    assert a._recall_mode == "context"


def test_recall_mode_explicit_tools(tmp_path):
    a = HolographicAdapter(config={"db_path": str(tmp_path / "h.db"), "recall_mode": "tools"})
    assert a._recall_mode == "tools"


def test_fact_search_search_action(adapter):
    adapter.handle_tool_call("fact_store", {"action": "add", "content": "uses vim", "category": "tool"})
    parsed = json.loads(adapter.handle_tool_call("fact_search", {"action": "search", "query": "vim"}))
    assert parsed["success"] is True
    assert parsed["count"] >= 1


def test_fact_search_list_action(adapter):
    adapter.handle_tool_call("fact_store", {"action": "add", "content": "fact a", "category": "general"})
    parsed = json.loads(adapter.handle_tool_call("fact_search", {"action": "list"}))
    assert parsed["success"] is True
    assert parsed["count"] >= 1


def test_fact_search_probe_related_reason_contradict(adapter):
    adapter.handle_tool_call("fact_store", {"action": "add", "content": "uses python", "category": "tool", "tags": ""})
    for action in ("probe", "related", "reason", "contradict"):
        args = {"action": action}
        if action in ("probe", "related"):
            args["entity"] = "python"
        elif action == "reason":
            args["entities"] = ["python"]
        parsed = json.loads(adapter.handle_tool_call("fact_search", args))
        assert parsed["success"] is True, f"{action} failed: {parsed}"


def test_fact_search_rejects_write_action(adapter):
    parsed = json.loads(adapter.handle_tool_call("fact_search", {"action": "add", "content": "x"}))
    assert parsed["success"] is False
    assert "unknown action" in parsed["error"]


def _adapter_with_mode(tmp_path, mode):
    a = HolographicAdapter(config={
        "db_path": str(tmp_path / "h.db"), "default_trust": 0.5,
        "min_trust_threshold": 0.3, "temporal_decay_half_life": 0,
        "auto_extract": False, "recall_mode": mode,
    })
    a.initialize(session_id="s1", project_root=".")
    return a


def test_get_tool_schemas_context_returns_empty(tmp_path):
    a = _adapter_with_mode(tmp_path, "context")
    assert a.get_tool_schemas() == []


def test_get_tool_schemas_tools_returns_fact_search_only(tmp_path):
    a = _adapter_with_mode(tmp_path, "tools")
    names = [s["name"] for s in a.get_tool_schemas()]
    assert names == ["fact_search"]


def test_get_tool_schemas_hybrid_returns_all(tmp_path):
    a = _adapter_with_mode(tmp_path, "hybrid")
    names = [s["name"] for s in a.get_tool_schemas()]
    assert names == ["fact_search", "fact_store", "fact_feedback"]


def test_prefetch_tools_mode_returns_empty(tmp_path):
    a = _adapter_with_mode(tmp_path, "tools")
    a.handle_tool_call("fact_store", {"action": "add", "content": "uses vim", "category": "tool"})
    assert a.prefetch("vim", session_id="s1") == ""


def test_prefetch_context_mode_still_recalls(tmp_path):
    a = _adapter_with_mode(tmp_path, "context")
    a.handle_tool_call("fact_store", {"action": "add", "content": "uses vim", "category": "tool"})
    assert "vim" in a.prefetch("vim", session_id="s1")


def test_tools_mode_fact_store_not_reachable(tmp_path):
    a = _adapter_with_mode(tmp_path, "tools")
    parsed = json.loads(a.handle_tool_call("fact_store", {"action": "add", "content": "x"}))
    assert parsed["success"] is False
    assert "unknown tool" in parsed["error"]


def test_hybrid_mode_fact_store_add_reachable(tmp_path):
    a = _adapter_with_mode(tmp_path, "hybrid")
    parsed = json.loads(a.handle_tool_call("fact_store", {"action": "add", "content": "x"}))
    assert parsed["success"] is True


def test_system_prompt_block_mode_label(tmp_path):
    for mode, label in [("tools", "tools-only"), ("context", "context-injection"), ("hybrid", "hybrid")]:
        a = _adapter_with_mode(tmp_path, mode)
        block = a.system_prompt_block()
        assert label in block
        assert "facts stored" in block
