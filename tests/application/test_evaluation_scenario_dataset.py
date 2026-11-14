"""T1：schema 2 场景数据契约与兼容加载测试。

覆盖 spec-261114-expand-e2e-experiments 4.1 节：
- schema 1 行为、hash 与结果不变（黄金契约）；
- schema 2 全部新增字段（execution/requirements/options/assertions/
  expected_output/bindings/lifecycle/required_metrics/source/
  comparison_scope）往返保留；
- 未知 profile/动作/断言类型/危险表达式/整型 schema_version/未知版本/
  非白名单 option/任意路径注入均前置拒绝；
- 旧服务语义：schema 2 不扩展 TARGET_KINDS，case target_kind 仍为
  agent_run；加载零网络、零业务初始化（纯声明式解析）。
"""
from __future__ import annotations

import copy
import json

import pytest

from app.application.evaluation_dataset import (
    compute_dataset_hash,
    load_dataset_manifest,
)
from app.domain.evaluation.scenario import (
    SCENARIO_ASSERTION_TYPES,
    SCENARIO_COMPARISON_SCOPES,
    SCENARIO_PROFILES,
    ScenarioCase,
)


def _base_manifest(schema_version="1", extra_case=None):
    case = {
        "case_id": "case-1",
        "input": {"turns": [{"message": "你好"}]},
        "target_kind": "agent_run",
        "expected_output": {"expectation": "回答包含 Hello"},
    }
    if extra_case:
        case.update(extra_case)
    return {
        "schema_version": schema_version,
        "dataset_id": "agent-e2e-scenarios",
        "dataset_version": "1",
        "target_kind": "agent_run",
        "cases": [case],
    }


def _write(tmp_path, content):
    content = copy.deepcopy(content)
    content["dataset_hash"] = compute_dataset_hash(content)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path, content


def _scenario_fields(**overrides):
    fields = {
        "execution": {
            "profile": "dashboard",
            "steps": [
                {"step_id": "turn-1", "action": "chat", "payload": {"message": "hello"}},
            ],
            "timeout_seconds": 120,
        },
        "requirements": {"required_tools": ["hello"]},
        "options": {},
        "assertions": [
            {
                "assertion_type": "tool_succeeded",
                "step_id": "turn-1",
                "params": {"tool": "hello"},
            },
        ],
        "expected_output": {
            "steps": {
                "turn-1": {"rubric": "回答包含 Hello", "reference": "Hello"},
            },
        },
        "bindings": {
            "plugin_seed": {"material": "plugin:hello", "sha256": "a" * 64},
        },
        "lifecycle": {"release_rule": "release_active_resources", "retention_rule": "evidence_retention_default"},
        "required_metrics": {"回答包含 Hello": "agent.scenario_assertions"},
        "source": {
            "repo_path": "tests/e2e/conversations/plugin-hello-chat.json",
            "source_case_id": "plugin-hello-chat",
            "source_sha256": "b" * 64,
            "rules_version": "e2e-conversion-v1",
            "transformations": ["原样保留"],
        },
        "comparison_scope": "fixed_fixture",
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# schema 1 黄金契约
# ---------------------------------------------------------------------------


def test_schema1_loads_unchanged_and_hash_stable(tmp_path):
    path, content = _write(tmp_path, _base_manifest())
    manifest = load_dataset_manifest(path)
    assert manifest.schema_version == "1"
    assert manifest.dataset_id == "agent-e2e-scenarios"
    case = manifest.cases[0]
    assert case.case_id == "case-1"
    assert case.scenario is None
    assert compute_dataset_hash(content) == manifest.dataset_hash


def test_integer_schema_version_rejected(tmp_path):
    content = _base_manifest()
    content["schema_version"] = 1
    path, _ = _write(tmp_path, content)
    with pytest.raises(ValueError, match="schema_version"):
        load_dataset_manifest(path)


def test_unknown_schema_version_rejected(tmp_path):
    path, _ = _write(tmp_path, _base_manifest(schema_version="3"))
    with pytest.raises(ValueError, match="schema_version"):
        load_dataset_manifest(path)


# ---------------------------------------------------------------------------
# schema 2 往返
# ---------------------------------------------------------------------------


def test_schema2_roundtrip_preserves_all_fields(tmp_path):
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": _scenario_fields()}))
    manifest = load_dataset_manifest(path)
    assert manifest.schema_version == "2"
    case = manifest.cases[0]
    assert isinstance(case.scenario, ScenarioCase)
    scenario = case.scenario
    assert scenario.execution.profile == "dashboard"
    assert scenario.execution.steps[0].step_id == "turn-1"
    assert scenario.execution.timeout_seconds == 120
    assert scenario.requirements.required_tools == ("hello",)
    assert scenario.assertions[0].assertion_type == "tool_succeeded"
    assert scenario.expected_output.steps["turn-1"]["rubric"] == "回答包含 Hello"
    assert scenario.bindings["plugin_seed"].sha256 == "a" * 64
    assert scenario.lifecycle.release_rule == "release_active_resources"
    assert scenario.required_metrics.coverages == {"回答包含 Hello": "agent.scenario_assertions"}
    assert scenario.source.repo_path == "tests/e2e/conversations/plugin-hello-chat.json"
    assert scenario.comparison_scope == "fixed_fixture"


def test_schema2_target_kind_still_agent_run_only(tmp_path):
    fields = _scenario_fields()
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields, "target_kind": "retrieval"}))
    with pytest.raises(ValueError, match="agent_run"):
        load_dataset_manifest(path)


def test_schema2_missing_scenario_field_rejected(tmp_path):
    path, _ = _write(tmp_path, _base_manifest("2"))
    with pytest.raises(ValueError, match="scenario"):
        load_dataset_manifest(path)


def test_schema2_unknown_top_level_case_field_rejected(tmp_path):
    fields = _scenario_fields()
    fields["run_id"] = "run-should-not-be-here"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_closed_vocabularies():
    assert SCENARIO_PROFILES == ("agent", "dashboard", "cli", "acp", "task_api")
    assert set(SCENARIO_ASSERTION_TYPES) == {
        "tool_succeeded", "tool_absent", "result_contains",
        "kb_source_and_quote", "task_terminal", "artifact_set",
        "file_content", "browser_state", "protocol_sequence",
        # T11：CLI 通道真实入口核验
        "cli_channel",
    }
    assert SCENARIO_COMPARISON_SCOPES == ("fixed_fixture", "live_smoke", "channel_smoke")


@pytest.mark.parametrize("profile", ["http", "shell", "", None, "agent_run"])
def test_unknown_profile_rejected(tmp_path, profile):
    fields = _scenario_fields(execution={"profile": profile, "steps": _scenario_fields()["execution"]["steps"], "timeout_seconds": 10})
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="profile"):
        load_dataset_manifest(path)


def test_unknown_step_action_rejected(tmp_path):
    fields = _scenario_fields()
    fields["execution"]["steps"][0]["action"] = "exec"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="action"):
        load_dataset_manifest(path)


def test_duplicate_step_id_rejected(tmp_path):
    fields = _scenario_fields()
    fields["execution"]["steps"].append(
        {"step_id": "turn-1", "action": "chat", "payload": {"message": "again"}}
    )
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="step_id"):
        load_dataset_manifest(path)


def test_task_create_payload_whitelist_enforced(tmp_path):
    fields = _scenario_fields()
    fields["execution"]["steps"] = [{
        "step_id": "spawn",
        "action": "task_create",
        "payload": {"title": "T", "body": "B", "goal_mode": True, "priority": 2},
    }]
    fields["assertions"] = [{
        "assertion_type": "task_terminal",
        "step_id": "spawn",
        "params": {"status": "succeeded", "within_seconds": 600},
    }]
    fields["expected_output"] = {"steps": {"spawn": {"rubric": "任务成功"}}}
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    manifest = load_dataset_manifest(path)
    assert manifest.cases[0].scenario.execution.steps[0].payload["goal_mode"] is True

    fields["execution"]["steps"][0]["payload"]["session_id"] = "injected"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_unknown_assertion_type_rejected(tmp_path):
    fields = _scenario_fields()
    fields["assertions"][0]["assertion_type"] = "custom_js_check"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="assertion_type"):
        load_dataset_manifest(path)


def test_assertion_unknown_param_key_rejected(tmp_path):
    fields = _scenario_fields()
    fields["assertions"][0]["params"] = {"tool_name": "hello", "pattern": ".*"}
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_assertion_nested_program_payload_rejected(tmp_path):
    fields = _scenario_fields()
    fields["assertions"][0]["params"] = {
        "tool_name": "execute_code",
        "expr": {"$code": "import os"},
    }
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_assertion_step_id_must_reference_execution_step(tmp_path):
    fields = _scenario_fields()
    fields["assertions"][0]["step_id"] = "turn-missing"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="step_id"):
        load_dataset_manifest(path)


def test_options_whitelist_enforced(tmp_path):
    fields = _scenario_fields(options={"external_memory_enabled": "file_1"})
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    manifest = load_dataset_manifest(path)
    assert manifest.cases[0].scenario.options.external_memory_enabled == "file_1"

    fields["options"]["run_id"] = "run-1"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_bindings_reject_invalid_sha256(tmp_path):
    fields = _scenario_fields()
    fields["bindings"]["mem0_space"] = {"material": "mem0:test", "sha256": "xyz"}
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="sha256"):
        load_dataset_manifest(path)


def test_source_rejects_absolute_or_escaping_repo_path(tmp_path):
    fields = _scenario_fields()
    fields["source"]["repo_path"] = "/etc/passwd"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)

    fields["source"]["repo_path"] = "../outside/conversations.json"
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_unknown_comparison_scope_rejected(tmp_path):
    fields = _scenario_fields(comparison_scope="best_effort")
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="comparison_scope"):
        load_dataset_manifest(path)


def test_step_timeout_must_not_exceed_execution_timeout(tmp_path):
    fields = _scenario_fields()
    fields["execution"]["step_timeouts"] = {"turn-1": 999}
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError):
        load_dataset_manifest(path)


def test_expected_output_step_keys_must_be_known(tmp_path):
    fields = _scenario_fields()
    fields["expected_output"]["steps"]["ghost"] = {"rubric": "x"}
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    with pytest.raises(ValueError, match="step_id"):
        load_dataset_manifest(path)


def test_whole_scenario_scope_via_star(tmp_path):
    fields = _scenario_fields()
    fields["expected_output"]["steps"]["*"] = {"rubric": "整体语义"}
    fields["assertions"].append({
        "assertion_type": "result_contains",
        "step_id": None,
        "params": {"text": "Hello"},
    })
    path, _ = _write(tmp_path, _base_manifest("2", {"scenario": fields}))
    manifest = load_dataset_manifest(path)
    assert "*" in manifest.cases[0].scenario.expected_output.steps
