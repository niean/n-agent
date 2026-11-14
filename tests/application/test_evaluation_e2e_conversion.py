"""T2：E2E 源样例到 schema 2 的无损离线转换测试。

覆盖 spec-261114-expand-e2e-experiments 4.2 节：
- 12 个源 JSON 全部可转换且覆盖报告完整（无静默丢条件）；
- 输入、每轮 rubric、expect、profile、task action_params 原样保留；
- mem0 合成事实、路径参数化必须记 transformations；
- 未知条件输出 unsupported（不静默丢弃、不伪造转换）；
- 转换是纯数据变换：零网络、零模型、零业务初始化、不自动生成金标。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.application import evaluation_e2e_conversion as conv
from app.application.evaluation_dataset import load_dataset_manifest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "tests/e2e/conversations"

ALL_CASES = (
    "acp-weather-chat",
    "browser-login-pod",
    "cli-weather-chat",
    "memory-file-chat",
    "memory-mem0-chat",
    "multimodal-vision-chat",
    "plugin-hello-chat",
    "rag-nkb-chat",
    "sandbox-execute-chat",
    "skill-weather-chat",
    "task-artifact-chat",
    "task-artifact-cmd",
)


def _source(case_id):
    return json.loads((SOURCE_DIR / f"{case_id}.json").read_text(encoding="utf-8"))


def _converted(result, case_id):
    for case in result.manifest["cases"]:
        if case["case_id"] == case_id:
            return case
    raise AssertionError(f"case {case_id} 未出现在转换结果中")


def _coverage_entry(result, case_id):
    for entry in result.coverage["cases"]:
        if entry["case_id"] == case_id:
            return entry
    raise AssertionError(f"case {case_id} 未出现在覆盖报告中")


@pytest.fixture(scope="module")
def result():
    return conv.convert_e2e_conversations(SOURCE_DIR)


# ---------------------------------------------------------------------------
# 全量覆盖
# ---------------------------------------------------------------------------


def test_all_12_sources_convert_with_complete_coverage(result):
    assert result.manifest["schema_version"] == "2"
    assert result.manifest["dataset_id"] == conv.DATASET_ID
    assert result.manifest["target_kind"] == "agent_run"
    ids = {case["case_id"] for case in result.manifest["cases"]}
    assert ids == set(ALL_CASES)
    assert result.coverage["summary"]["converted"] == 12
    assert result.coverage["summary"]["unsupported"] == 0
    for entry in result.coverage["cases"]:
        assert entry["status"] == "converted", entry


def test_manifest_converts_through_authoritative_loader(result, tmp_path):
    manifest = json.loads(json.dumps(result.manifest))
    dataset_dir = tmp_path / "agent-e2e-scenarios" / "1"
    dataset_dir.mkdir(parents=True)
    for rel_path, content in result.materials:
        target = dataset_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest["dataset_hash"] = conv.compute_manifest_hash(manifest)
    path = dataset_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    loaded = load_dataset_manifest(path)
    assert len(loaded.cases) == 12
    assert loaded.schema_version == "2"


# ---------------------------------------------------------------------------
# 逐项语义保留
# ---------------------------------------------------------------------------


def test_channel_maps_to_profile_exactly(result):
    expected = {
        "cli-weather-chat": "cli",
        "acp-weather-chat": "acp",
        "task-artifact-cmd": "task_api",
        "multimodal-vision-chat": "dashboard",
        "plugin-hello-chat": "dashboard",
    }
    for case_id, profile in expected.items():
        scenario = _converted(result, case_id)["scenario"]
        assert scenario["execution"]["profile"] == profile, case_id


def test_per_turn_rubrics_preserved_for_multi_turn(result):
    source = _source("browser-login-pod")
    case = _converted(result, "browser-login-pod")
    scenario = case["scenario"]
    assert len(scenario["execution"]["steps"]) == len(source["messages"]) == 2
    for position, message in enumerate(source["messages"]):
        step_id = scenario["execution"]["steps"][position]["step_id"]
        entry = scenario["expected_output"]["steps"][step_id]
        assert entry["rubric"] == message["expect"]["rubric"]


def test_task_action_params_preserved_verbatim(result):
    source = _source("task-artifact-cmd")
    params = source["messages"][0]["action_params"]
    case = _converted(result, "task-artifact-cmd")
    step = case["scenario"]["execution"]["steps"][0]
    assert step["action"] == "task_create"
    assert step["payload"] == {
        "title": params["title"],
        "body": params["body"],
        "goal_mode": params["goal_mode"],
        "priority": params["priority"],
    }


def test_expect_tools_become_tool_succeeded_assertions(result):
    source = _source("plugin-hello-chat")
    case = _converted(result, "plugin-hello-chat")
    assertions = case["scenario"]["assertions"]
    tool_assertions = [
        item for item in assertions if item["assertion_type"] == "tool_succeeded"
    ]
    assert source["messages"][0]["expect"]["tools"] == ["hello"]
    assert {"tool": "hello"} in [item["params"] for item in tool_assertions]


def test_forbidden_tools_become_tool_absent(result):
    for case_id in ("rag-nkb-chat", "browser-login-pod"):
        source = _source(case_id)
        forbidden = set()
        for message in source["messages"]:
            forbidden.update(message["expect"].get("forbidden_tools", []))
        assert forbidden, case_id
        case = _converted(result, case_id)
        absent = {
            item["params"]["tool"]
            for item in case["scenario"]["assertions"]
            if item["assertion_type"] == "tool_absent"
        }
        assert forbidden <= absent, case_id


def test_side_effects_become_structured_assertions(result):
    case = _converted(result, "task-artifact-cmd")
    assertions = case["scenario"]["assertions"]
    file_assertions = {
        item["params"]["filename"]: item["params"]["contains"]
        for item in assertions
        if item["assertion_type"] == "file_content"
    }
    assert file_assertions == {
        "task-output-a.txt": "artifact a",
        "task-output-b.md": "# artifact b",
    }
    artifact = [
        item for item in assertions if item["assertion_type"] == "artifact_set"
    ]
    assert artifact and artifact[0]["params"]["min_count"] == 2
    terminal = [
        item for item in assertions if item["assertion_type"] == "task_terminal"
    ]
    assert terminal and terminal[0]["params"]["within_seconds"] == 600


def test_acp_protocol_sequence_assertion(result):
    case = _converted(result, "acp-weather-chat")
    sequence = [
        item for item in case["scenario"]["assertions"]
        if item["assertion_type"] == "protocol_sequence"
    ]
    assert sequence
    assert sequence[0]["params"]["entries"] == [
        "initialize", "authenticate", "session/new", "session/prompt",
    ]


# ---------------------------------------------------------------------------
# 变换与绑定
# ---------------------------------------------------------------------------


def test_mem0_synthetic_fact_transformation_recorded(result):
    source = _source("memory-mem0-chat")
    case = _converted(result, "memory-mem0-chat")
    scenario = case["scenario"]
    transformations = " ".join(scenario["source"]["transformations"])
    assert "mem0" in transformations and "合成" in transformations
    # 不读取用户个人记忆：绑定必须是专用测试命名空间（逻辑别名）
    alias = scenario["options"]["external_memory_enabled"]
    assert isinstance(alias, str) and alias
    binding = scenario["bindings"][alias]
    assert "个人" not in binding["material"]
    assert binding["sha256"] == hashlib.sha256(
        "喜欢樱桃".encode("utf-8")
    ).hexdigest()
    assert scenario["source"]["source_case_id"] == source["id"]


def test_memory_file_binding_uses_logical_alias(result):
    scenario = _converted(result, "memory-file-chat")["scenario"]
    assert scenario["options"]["external_memory_enabled"] == "file_1"
    assert scenario["bindings"]["file_1"]["material"].startswith("memory-file")


def test_task_cmd_path_parameterization_recorded(result):
    transformations = " ".join(
        _converted(result, "task-artifact-cmd")["scenario"]["source"]["transformations"]
    )
    assert "隔离" in transformations or "参数化" in transformations


def test_image_material_and_fixture_wired(result):
    source_bytes = (SOURCE_DIR / "assets/chat-multimodal-1.png").read_bytes()
    materials = dict(result.materials)
    fixture_path = None
    case = _converted(result, "multimodal-vision-chat")
    fixture_path = case.get("fixture")
    assert fixture_path, "图片题必须声明 fixture"
    assert materials[fixture_path] == source_bytes
    assert case["fixture_sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_image_case_input_inlines_data_url_matching_fixture(result):
    """图片题输入内联 data URL（与 v1 相同形态）：执行层不解析路径引用，
    图片必须随输入进入消息；内联字节与 fixture 三路一致。"""
    import base64

    case = _converted(result, "multimodal-vision-chat")
    turn = case["input"]["turns"][0]
    assert "messages" in turn, "图片题输入必须内联 messages（执行层只透传消息）"
    parts = turn["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": turn["message"]}
    image_part = parts[1]
    assert image_part["type"] == "image_url"
    prefix, encoded = image_part["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    source_bytes = (SOURCE_DIR / "assets/chat-multimodal-1.png").read_bytes()
    assert base64.b64decode(encoded, validate=True) == source_bytes


# ---------------------------------------------------------------------------
# 溯源与覆盖
# ---------------------------------------------------------------------------


def test_source_provenance_and_no_identity_leak(result):
    for case in result.manifest["cases"]:
        case_id = case["case_id"]
        source = _source(case_id)
        scenario = case["scenario"]
        assert scenario["source"]["repo_path"] == f"tests/e2e/conversations/{case_id}.json"
        assert scenario["source"]["source_sha256"] == hashlib.sha256(
            (SOURCE_DIR / f"{case_id}.json").read_bytes()
        ).hexdigest()
        assert scenario["source"]["source_case_id"] == source["id"]
        assert "source_session_id" not in json.dumps(case)
        assert "source_session_id" not in json.dumps(scenario)


def test_required_metrics_cover_every_clause(result):
    for case_id in ALL_CASES:
        source = _source(case_id)
        scenario = _converted(result, case_id)["scenario"]
        coverages = scenario["required_metrics"]
        for message in source["messages"]:
            clause = message["expect"]["rubric"]
            assert clause in coverages, (case_id, clause)
            metric = coverages[clause]
            assert metric in (
                "agent.scenario_assertions", "agent.scenario_quality",
            ), (case_id, metric)


def test_rubric_clauses_not_marked_mechanized_without_rule(result):
    # 天气答复语义未机械化：必须映射到 quality 指标，不允许只保留硬断言
    for case_id in ("skill-weather-chat", "cli-weather-chat", "acp-weather-chat"):
        scenario = _converted(result, case_id)["scenario"]
        clause = _source(case_id)["messages"][0]["expect"]["rubric"]
        assert scenario["required_metrics"][clause] == "agent.scenario_quality"


def test_comparison_scope_mapping(result):
    expected = {
        "skill-weather-chat": "live_smoke",
        "rag-nkb-chat": "live_smoke",
        "browser-login-pod": "live_smoke",
        "cli-weather-chat": "channel_smoke",
        "acp-weather-chat": "channel_smoke",
        "plugin-hello-chat": "fixed_fixture",
        "sandbox-execute-chat": "fixed_fixture",
        "task-artifact-cmd": "fixed_fixture",
    }
    for case_id, scope in expected.items():
        assert _converted(result, case_id)["scenario"]["comparison_scope"] == scope


def test_unknown_construct_reported_unsupported(tmp_path):
    source = _source("plugin-hello-chat")
    source["channel"] = "carrier-pigeon"
    path = tmp_path / "plugin-hello-chat.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    result = conv.convert_e2e_conversations(tmp_path)
    assert result.coverage["summary"]["unsupported"] == 1
    entry = _coverage_entry(result, "plugin-hello-chat")
    assert entry["status"] == "unsupported"
    assert entry["unsupported"]
    assert result.manifest["cases"] == []


def test_unknown_side_effect_type_unsupported(tmp_path):
    source = _source("task-artifact-chat")
    source["side_effects"] = [{"type": "send_email", "to": "boss"}]
    path = tmp_path / "task-artifact-chat.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    result = conv.convert_e2e_conversations(tmp_path)
    entry = _coverage_entry(result, "task-artifact-chat")
    assert entry["status"] == "unsupported"


def test_lifecycle_rules_present(result):
    for case in result.manifest["cases"]:
        lifecycle = case["scenario"]["lifecycle"]
        assert lifecycle["release_rule"] == "release_active_resources"
        assert lifecycle["retention_rule"] == "retain_evidence_default"


def test_conversion_does_not_auto_generate_gold(result):
    # expected_output 只来自源 rubric（与源文本逐字一致），不自动生成金标
    for case_id in ALL_CASES:
        source = _source(case_id)
        scenario = _converted(result, case_id)["scenario"]
        rubrics = {
            entry["rubric"]
            for entry in scenario["expected_output"]["steps"].values()
        }
        source_rubrics = {message["expect"]["rubric"] for message in source["messages"]}
        assert rubrics == source_rubrics, case_id


def test_conversion_module_has_no_business_or_network_imports():
    import ast

    tree = ast.parse(Path(conv.__file__).read_text(encoding="utf-8"))
    forbidden = ("httpx", "requests", "socket", "subprocess", "app.main")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(f) for f in forbidden), alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(module.startswith(f) for f in forbidden), module
