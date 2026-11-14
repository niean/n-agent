"""验证 E2E 图片题可由正式数据集、HTTP 执行器和 correctness Judge 消费。"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.application.evaluation_dataset import compute_dataset_hash, load_dataset_manifest
from app.application.evaluation_experiment import (
    EvaluationExperimentRunner,
    ExecuteExperimentRequest,
    ExperimentRootHandle,
)
from app.application.evaluation_service import EvaluationService, definition_from_mapping
from app.domain.evaluation.models import (
    EVIDENCE_SNAPSHOT_MAX_BYTES,
    EvidenceSnapshot,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.judge_provider import make_answer_correctness_judge
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.infrastructure.evaluation.target_runner import AgentHTTPTargetRunner

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evaluations/datasets/agent-e2e-v1"
SOURCE = ROOT / "tests/e2e/conversations/multimodal-vision-chat.json"
SOURCE_IMAGE = SOURCE.parent / "assets/chat-multimodal-1.png"
ANSWER = '界面标题为“有什么我能帮你的吗？”，有“对话/工作”分段选择器，对话已选中。'


def _raw():
    return json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))


def _source_message():
    return json.loads(SOURCE.read_text(encoding="utf-8"))["messages"][0]


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_e2e_dataset_preserves_reviewed_source_and_quality_boundary():
    dataset = load_dataset_manifest(DATASET)
    raw = _raw()
    source = _source_message()
    assert dataset.dataset_id == "agent-e2e-v1"
    assert dataset.schema_version == dataset.dataset_version == "1"
    assert dataset.target_kind == "agent_run"
    assert len(dataset.cases) == 1
    case = dataset.cases[0]
    assert case.case_id == "multimodal-vision-chat"
    assert case.target_kind == "agent_run"
    assert case.input["turns"][0]["message"] == source["content"]
    assert dict(case.expected_output) == {"expectation": source["expect"]["rubric"]}
    assert raw["role"] == "smoke"
    assert "冒烟" in raw["quality_note"] and "总体质量" in raw["quality_note"]
    assert compute_dataset_hash(raw) == dataset.dataset_hash
    assert raw["source"] == {
        "path": str(SOURCE.relative_to(ROOT)),
        "message_id": source["id"],
        "sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    }
    assert "source_session_id" not in set(_keys(raw))
    assert "task_success" not in set(_keys(raw))
    assert not case.allowed_side_effects and not case.cleanup_rule


def test_inline_image_matches_hashed_fixture_and_original():
    case = load_dataset_manifest(DATASET).cases[0]
    parts = case.input["turns"][0]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": _source_message()["content"]}
    assert parts[1]["type"] == "image_url"
    prefix, encoded = parts[1]["image_url"]["url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    fixture = (DATASET / case.fixture).read_bytes()
    assert base64.b64decode(encoded, validate=True) == fixture == SOURCE_IMAGE.read_bytes()
    assert hashlib.sha256(fixture).hexdigest() == _raw()["cases"][0]["fixture_sha256"]


class _Evidence:
    def __init__(self):
        self.snapshots = {}

    async def save_snapshot(self, target, snapshot):
        ref = f"snapshot-{len(self.snapshots)}"
        self.snapshots[ref] = snapshot
        return ref

    async def load_snapshot(self, snapshot_ref):
        return self.snapshots[snapshot_ref]

    async def has_snapshot(self, snapshot_ref):
        return snapshot_ref in self.snapshots


@contextmanager
def _root_scope(identity):
    yield ExperimentRootHandle(trace_id=None, span_id=None, is_valid=False)


SCENARIO_DATASET = ROOT / "evaluations/datasets/agent-e2e-scenarios/1"


def _sse(chunks, *, terminated=True):
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    if terminated:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


@pytest.mark.asyncio
async def test_scenario_snapshot_turns_carry_judge_slots_and_evidence(tmp_path):
    """R1 场景题快照：turns 携带逐轮 request/answer/rubric（scenario_quality
    Judge 槽位），scenario_evidence 来自正式端口且如实透传。"""
    from app.infrastructure.evaluation.scenario_adapters import ScenarioDashboardSSERunner

    manifest = load_dataset_manifest(SCENARIO_DATASET / "manifest.json")
    hello = next(c for c in manifest.cases if c.case_id == "plugin-hello-chat")
    subset = load_dataset_manifest(SCENARIO_DATASET / "manifest.json")
    from app.domain.evaluation.models import DatasetManifest
    dataset = DatasetManifest(
        schema_version=subset.schema_version, dataset_id=subset.dataset_id,
        dataset_version=subset.dataset_version, dataset_hash=subset.dataset_hash,
        target_kind=subset.target_kind, cases=(hello,),
    )
    tool_calls_read = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/chat/sessions") and request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-x"})
        if url.endswith("/tool-calls"):
            tool_calls_read.append(url)
            return httpx.Response(200, json=[
                {"tool": "hello", "status": "success", "result": "你好！",
                 "run_id": "run-hello"},
            ])
        return httpx.Response(200, content=_sse([
            {"choices": [{"index": 0, "delta": {"content": "你好！"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "run_id": "run-hello"},
        ]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    target = ScenarioDashboardSSERunner(
        base_url="https://agent.example", model="test-model", client=client,
    )
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    service = EvaluationService(store, enabled=True)
    await service.register_preset_definitions(judge_model="test-judge-model")
    evidence = _Evidence()
    runner = EvaluationExperimentRunner(
        service=service, ledger=store, target_runner=target, evidence=evidence,
        experiment_root_scope=_root_scope,
    )
    try:
        report = await runner.execute(ExecuteExperimentRequest(
            dataset=dataset, name="scenario-slot-contract", target_config={},
            evaluator_set=(("agent.scenario_quality",
                            EvaluationService.EXPERIMENT_PRESET_VERSION),),
        ))
    finally:
        await target.aclose()
    assert len(report.items) == 1
    # 正式端口被读取（真实工具证据入口）
    assert len(tool_calls_read) == 1
    snapshot = await evidence.load_snapshot(next(iter(evidence.snapshots)))
    turn = snapshot.content["turns"][0]
    assert turn["step_id"] == "turn-1"
    assert turn["request"] == hello.input["turns"][0]["message"]
    assert turn["answer"] == "你好！"
    expected_rubric = hello.scenario.expected_output.steps["turn-1"]["rubric"]
    assert turn["rubric"] == expected_rubric
    assert snapshot.content["assertions"], "断言契约进入快照"
    steps = snapshot.content["scenario_evidence"]["steps"]
    assert steps == [{"step_id": "turn-1", "run_id": "run-hello",
                      "answer": "你好！"}]
    assert snapshot.content["scenario_evidence"]["status"] == "complete"
    assert snapshot.content["scenario_evidence"]["tool_calls"][0]["tool"] == "hello"


@pytest.mark.asyncio
async def test_image_reaches_target_and_reference_reaches_correctness_judge(tmp_path):
    dataset = load_dataset_manifest(DATASET)
    expected_messages = dataset.cases[0].input["turns"][0]["messages"]
    target_calls = []

    def target_handler(request):
        assert str(request.url) == "https://agent.example/v1/chat/completions"
        assert request.headers["x-session-id"]
        body = json.loads(request.content)
        assert body["messages"] == expected_messages
        target_calls.append(body)
        return httpx.Response(200, json={
            "model": "test-vision-model",
            "run_id": "run-test-1",
            "choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(target_handler))
    target = AgentHTTPTargetRunner(
        base_url="https://agent.example", model="test-vision-model", client=client,
    )
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    service = EvaluationService(store, enabled=True)
    await service.register_preset_definitions(judge_model="test-judge-model")
    evidence = _Evidence()
    runner = EvaluationExperimentRunner(
        service=service, ledger=store, target_runner=target, evidence=evidence,
        experiment_root_scope=_root_scope,
    )
    try:
        report = await runner.execute(ExecuteExperimentRequest(
            dataset=dataset, name="e2e-dataset-contract", target_config={},
            evaluator_set=(("agent.answer_correctness", EvaluationService.EXPERIMENT_PRESET_VERSION),),
        ))
    finally:
        await target.aclose()
    assert len(target_calls) == 1
    assert len(report.items) == 1
    assert report.items[0].case_id == "multimodal-vision-chat"
    assert report.items[0].cleanup_status == "not_required"
    assert report.cleanup_failures == 0
    assert len(evidence.snapshots) == 1
    snapshot = await evidence.load_snapshot(next(iter(evidence.snapshots)))
    assert snapshot.content["request"] == _source_message()["content"]
    assert snapshot.content["answer"] == ANSWER
    assert snapshot.content["reference"] == _source_message()["expect"]["rubric"]
    assert len(canonical_json_bytes(dict(snapshot.content))) < EVIDENCE_SNAPSHOT_MAX_BYTES
    judge_calls = []

    def judge_handler(request):
        body = json.loads(request.content)
        slots = json.loads(body["messages"][1]["content"])["evidence"]
        by_role = {slot["role"]: slot for slot in slots}
        assert by_role["reference"]["content"] == _source_message()["expect"]["rubric"]
        assert by_role["answer"]["content"] == ANSWER
        judge_calls.append(body)
        score = {"value": 1, "reason": "测试参考槽位", "evidence_refs": [by_role["reference"]["evidence_id"]]}
        return httpx.Response(200, json={
            "model": "test-judge-model",
            "choices": [{"message": {"content": json.dumps(score)}, "finish_reason": "stop"}],
        })

    judge = make_answer_correctness_judge(
        base_url="https://judge.example/v1", api_key="test-placeholder",
        model="test-judge-model", transport=httpx.MockTransport(judge_handler),
    )
    definition_row = await store.get_definition(
        "agent.answer_correctness", EvaluationService.EXPERIMENT_PRESET_VERSION,
    )
    definition = definition_from_mapping(definition_row["definition"])
    try:
        result = await judge.evaluate(definition, snapshot)
        assert result["status"] == "value"
        assert result["value"] == 1
        without_reference = dict(snapshot.content)
        without_reference.pop("reference")
        skipped = await judge.evaluate(definition, EvidenceSnapshot(
            content=without_reference, redaction_version=snapshot.redaction_version,
        ))
        assert skipped["status"] == "skipped"
        assert skipped["skip_reason"] == "missing_reference"
        assert len(judge_calls) == 1
    finally:
        await judge.aclose()
