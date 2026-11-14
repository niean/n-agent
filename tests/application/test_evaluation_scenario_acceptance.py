"""场景数据集跨批验收（T13）：4/7/10/12 版本集合、源 hash、指标覆盖、
版本不可覆盖、已发布字节冻结。

只验证仓库内已发布数据集的契约完整性；不证明真实环境可运行（真实
执行证据见各批 S4 的 Docker 验证记录）。"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from app.application.evaluation_review import EvaluationReviewService
from app.infrastructure.evaluation.store import SQLiteEvaluationStore

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evaluations/datasets/agent-e2e-scenarios"
SCENARIO_METRICS = {"agent.scenario_assertions", "agent.scenario_quality"}

# 已发布版本字节冻结（发现漂移即说明有人改了历史版本而非新建版本）：
# R1 四题 / R2 七题 / R3 十题 / R4 十二题。
FROZEN_MANIFEST_SHA256 = {
    "1": "3c98d8eda972093e91f49508f3dddd897ee7841454f1e960b2643fa79224a88e",
    "2": "c03607483eb1d4971eff69c346c78eb9be06bc0ce9d3726aac0a47705bb86538",
    "3": "3975217c63ca0e6702fce56fa4601976cb5051bfaf4560316d8084682b7a176e",
    "4": "190b6bdd5ce47138da827237c477695f3d6ab968f62b9f8f4a4b71c91cfdc3cb",
}

EXPECTED_CASE_COUNTS = {"1": 4, "2": 7, "3": 10, "4": 12}
# 每批新增题（覆盖集合断言：新题只增不顶替）。
BATCH_ADDED_CASES = {
    "1": {
        "multimodal-vision-chat",
        "plugin-hello-chat",
        "sandbox-execute-chat",
        "skill-weather-chat",
    },
    "2": {"memory-file-chat", "memory-mem0-chat", "rag-nkb-chat"},
    "3": {"browser-login-pod", "task-artifact-chat", "task-artifact-cmd"},
    "4": {"cli-weather-chat", "acp-weather-chat"},
}
# 批次关键失败路径的结构性保证：通道题必须带通道断言，禁用工具必须
# 有 tool_absent 断言（不被"普通聊天答对"伪装）。
CHANNEL_ASSERTIONS = {
    "acp": {"protocol_sequence"},
    "cli": {"cli_channel"},
}


def _load(version: str) -> dict:
    return json.loads(
        (SCENARIOS / version / "manifest.json").read_text(encoding="utf-8")
    )


def _cases(version: str) -> dict[str, dict]:
    return {c["case_id"]: c for c in _load(version)["cases"]}


def test_scenario_versions_are_cumulative_and_frozen():
    """4/7/10/12 版本集合：累计超集、每批只增不顶替、已发布字节不变。"""
    previous: set[str] = set()
    for version in ("1", "2", "3", "4"):
        manifest = _load(version)
        assert manifest["schema_version"] == "2"
        assert manifest["dataset_id"] == "agent-e2e-scenarios"
        assert manifest["target_kind"] == "agent_run"
        cases = {c["case_id"] for c in manifest["cases"]}
        assert len(cases) == EXPECTED_CASE_COUNTS[version]
        assert previous < cases, f"v{version} 非累计超集"
        assert cases - previous == BATCH_ADDED_CASES[version]
        raw_bytes = (SCENARIOS / version / "manifest.json").read_bytes()
        assert hashlib.sha256(raw_bytes).hexdigest() == (
            FROZEN_MANIFEST_SHA256[version]
        ), f"v{version} 已发布字节被改动"
        previous = cases
    # v1 图片附件逐字节冻结（旧版本素材不变）
    fixture = ROOT / "evaluations/datasets/agent-e2e-v1/fixtures/chat-multimodal-1.png"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        "e12f5c4574af6f57c5eb7b0fae6a773e6a23698355da29fed8c40a3d1a973328"
    )


def test_every_case_source_hash_matches_repo_file():
    """源 hash：全部版本全部 case 的 source.repo_path 存在且逐字节匹配
    source_sha256（转换不虚构来源）。"""
    for version in ("1", "2", "3", "4"):
        for case_id, case in _cases(version).items():
            scenario = case.get("scenario") or {}
            source = scenario.get("source") or {}
            assert source, f"v{version}/{case_id} 缺 source 溯源"
            path = ROOT / source["repo_path"]
            assert path.is_file(), f"v{version}/{case_id} 源文件缺失"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == source["source_sha256"], (
                f"v{version}/{case_id} 源 hash 漂移: {source['repo_path']}"
            )
            assert source["source_case_id"]
            # R1 以 v1 规则转换，R2 起收紧为 v2（live/通道证据要求）
            expected_rules = (
                "e2e-conversion-v1" if version == "1" else "e2e-conversion-v2"
            )
            assert source["rules_version"] == expected_rules


def test_every_rubric_clause_has_required_metric():
    """每条要求有对应指标：expected_output 逐轮 rubric 必须被
    required_metrics 覆盖，指标名限定在场景指标闭集内。"""
    for version in ("1", "2", "3", "4"):
        for case_id, case in _cases(version).items():
            scenario = case["scenario"]
            coverages = scenario["required_metrics"]
            assert coverages, f"v{version}/{case_id} required_metrics 为空"
            unknown = set(coverages.values()) - SCENARIO_METRICS
            assert not unknown, f"v{version}/{case_id} 未注册指标: {unknown}"
            steps = scenario["expected_output"]["steps"]
            for step_id, payload in steps.items():
                rubric = payload.get("rubric")
                if isinstance(rubric, str) and rubric:
                    assert rubric in coverages, (
                        f"v{version}/{case_id} step {step_id} rubric 无指标覆盖"
                    )


def test_channel_and_tool_claims_have_deterministic_assertions():
    """关键失败路径结构保证：通道题必须携带通道断言（acp 协议序列、
    cli 入口），required_tools 必须有对应 tool_succeeded 断言（证据
    fail-closed，回答文本不能替代真实工具执行）。"""
    for version in ("1", "2", "3", "4"):
        for case_id, case in _cases(version).items():
            scenario = case["scenario"]
            profile = scenario["execution"]["profile"]
            assertion_types = {
                a["assertion_type"] for a in scenario["assertions"]
            }
            required = CHANNEL_ASSERTIONS.get(profile, set())
            missing = required - assertion_types
            assert not missing, (
                f"v{version}/{case_id} 通道 {profile} 缺断言: {missing}"
            )
            tools = scenario["requirements"]["required_tools"]
            tool_asserted = {
                a["params"]["tool"]
                for a in scenario["assertions"]
                if a["assertion_type"] == "tool_succeeded"
            }
            unasserted = set(tools) - tool_asserted
            assert not unasserted, (
                f"v{version}/{case_id} required_tools 无 tool_succeeded "
                f"覆盖: {unasserted}"
            )
            lifecycle = scenario["lifecycle"]
            assert lifecycle["release_rule"]
            assert lifecycle["retention_rule"]
            assert scenario["comparison_scope"]


def _import_payload(version: str) -> dict:
    manifest = _load(version)
    cases = []
    for case in manifest["cases"]:
        case = dict(case)
        if "fixture" in case:
            asset = (SCENARIOS / version / case.pop("fixture")).read_bytes()
            case.pop("fixture_sha256")
            case["fixture_content"] = base64.b64encode(asset).decode("ascii")
        cases.append(case)
    return {
        "schema_version": manifest["schema_version"],
        "dataset_id": manifest["dataset_id"],
        "dataset_version": manifest["dataset_version"],
        "target_kind": manifest["target_kind"],
        "approved_by": "niean",
        "source_result_ids": [],
        "cases": cases,
    }


@pytest.mark.asyncio
async def test_reimport_same_version_conflicts_and_keeps_original(tmp_path):
    """不同版本不可覆盖：同版本号再导入（内容被篡改）必须冲突，且已
    发布版本读回保持原内容。"""
    review = EvaluationReviewService(
        store=SQLiteEvaluationStore(str(tmp_path / "evaluations.db")),
        datasets_root=tmp_path / "datasets",
    )
    payload = _import_payload("1")
    first = await review.import_dataset(payload)
    original_hash = first["dataset_hash"]
    assert original_hash

    mutated = json.loads(json.dumps(payload))
    mutated["cases"][0]["input"]["turns"][0]["message"] = "被篡改的提问"
    with pytest.raises(Exception) as exc_info:
        await review.import_dataset(mutated)
    from app.domain.evaluation.errors import EvaluationConflictError

    assert isinstance(exc_info.value, EvaluationConflictError)

    readback = await review.get_dataset("agent-e2e-scenarios", "1")
    assert readback["dataset_hash"] == original_hash
    stored_cases = {c["case_id"]: c for c in readback["cases"]}
    assert stored_cases["multimodal-vision-chat"]["input"]["turns"][0][
        "message"
    ] != "被篡改的提问"
