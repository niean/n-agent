# tests/application/test_evaluation_dataset.py
"""T12: DatasetManifest 加载与校验（spec 9.1）。

覆盖：
- manifest 字段完整性：schema_version、dataset_id/version/hash、case_id、
  input、expected_output 或 qrels、rubric_ref、target_kind、slice、
  fixture/corpus_revision、允许副作用及清理规则；
- hash 校验：manifest 内容 hash 与声明一致；附件/fixture 路径存在性与
  hash 校验失败立即终止（fail-fast）；loader 不执行数据集内任何脚本
  （静态 AST 断言，无 exec/eval/动态 import 触点）；
- qrels 按 corpus_revision+document_revision+chunk_id 标注并去重；
  no_answer query 必须声明对应 no_answer 指标适用性；
- 数据集不属于 Config Bundle 白名单（repo 制品，不是运行时配置，
  不随 restart 清空）。
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from app.application.evaluation_dataset import (
    DATASET_SCHEMA_VERSION,
    DatasetManifestError,
    compute_dataset_hash,
    load_dataset_manifest,
    qrels_identity,
)
from app.domain.evaluation.models import DatasetManifest
from app.domain.evaluation.metrics_deterministic import (
    ChunkIdentity,
    kb_recall_at_5,
)

ROOT = Path(__file__).resolve().parents[2]
LOADER_PATH = ROOT / "app" / "application" / "evaluation_dataset.py"

FIXTURE_TEXT = "kb frozen corpus chunk\n"
RUBRIC_TEXT = "# rubric\n"
CORPUS_REVISION = "corpus-2026-11"


# ---------------------------------------------------------------------------
# 测试夹具构造
# ---------------------------------------------------------------------------


def _fixture_sha() -> str:
    return hashlib.sha256(FIXTURE_TEXT.encode("utf-8")).hexdigest()


def _rubric_sha() -> str:
    return hashlib.sha256(RUBRIC_TEXT.encode("utf-8")).hexdigest()


def _qrels_case() -> dict:
    return {
        "case_id": "kb-001",
        "input": {"query": "如何部署 n-agent"},
        "target_kind": "retrieval",
        "slice": "normal",
        "corpus_revision": CORPUS_REVISION,
        "qrels": [
            {
                "corpus_revision": CORPUS_REVISION,
                "document_revision": "doc-1",
                "chunk_id": "chunk-1",
                "grade": 2,
            },
            {
                "corpus_revision": CORPUS_REVISION,
                "document_revision": "doc-2",
                "chunk_id": "chunk-2",
                "grade": 1,
            },
        ],
        "fixture": "fixtures/corpus.txt",
        "fixture_sha256": _fixture_sha(),
        "allowed_side_effects": [],
        "cleanup_rule": "",
    }


def _agent_case() -> dict:
    return {
        "case_id": "agent-001",
        "input": {"message": "创建一个标记为 done 的任务"},
        "target_kind": "agent_run",
        "slice": "side-effect",
        "expected_output": {"task_status": "done"},
        "rubric_ref": "rubrics/task.md",
        "rubric_sha256": _rubric_sha(),
        "allowed_side_effects": ["task:create"],
        "cleanup_rule": "删除本次会话创建的任务",
    }


def _base_content() -> dict:
    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": "kb-smoke",
        "dataset_version": "v1",
        # 占位：_write_dataset 会按内容重算并覆盖（compute_dataset_hash
        # 忽略 dataset_hash 字段本身）
        "dataset_hash": "",
        "target_kind": "retrieval",
        "cases": [_qrels_case()],
    }


def _write_dataset(
    tmp_path: Path,
    content: dict,
    *,
    write_fixtures: bool = True,
    hash_override: str | None = None,
) -> Path:
    """按 hash 纪律写入数据集目录并返回目录路径。

    hash_override 非空时以错误 hash 写入（供 hash 不一致测试使用）。
    """
    base = tmp_path / "dataset"
    base.mkdir(parents=True, exist_ok=True)
    if write_fixtures:
        fixture_dir = base / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        (fixture_dir / "corpus.txt").write_text(FIXTURE_TEXT, encoding="utf-8")
        rubric_dir = base / "rubrics"
        rubric_dir.mkdir(parents=True, exist_ok=True)
        (rubric_dir / "task.md").write_text("# rubric\n", encoding="utf-8")
    payload = dict(content)
    payload["dataset_hash"] = (
        hash_override
        if hash_override is not None
        else compute_dataset_hash(content)
    )
    (base / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return base


# ---------------------------------------------------------------------------
# manifest 字段完整性
# ---------------------------------------------------------------------------


def test_loads_valid_manifest_into_domain_vo(tmp_path):
    content = _base_content()
    content["cases"] = [_qrels_case(), _agent_case()]
    base = _write_dataset(tmp_path, content)

    manifest = load_dataset_manifest(base)

    assert isinstance(manifest, DatasetManifest)
    assert manifest.schema_version == DATASET_SCHEMA_VERSION
    assert manifest.dataset_id == "kb-smoke"
    assert manifest.dataset_version == "v1"
    assert manifest.target_kind == "retrieval"
    assert len(manifest.cases) == 2

    kb = manifest.cases[0]
    assert kb.case_id == "kb-001"
    assert kb.input == {"query": "如何部署 n-agent"}
    assert kb.target_kind == "retrieval"
    assert kb.slice == "normal"
    assert kb.corpus_revision == CORPUS_REVISION
    assert kb.fixture == "fixtures/corpus.txt"
    assert kb.expected_output is None
    # qrels 以 corpus_revision+document_revision+chunk_id 复合身份为键
    assert kb.qrels == {
        qrels_identity(CORPUS_REVISION, "doc-1", "chunk-1"): 2,
        qrels_identity(CORPUS_REVISION, "doc-2", "chunk-2"): 1,
    }
    assert kb.allowed_side_effects == ()

    agent = manifest.cases[1]
    assert agent.case_id == "agent-001"
    assert agent.expected_output == {"task_status": "done"}
    assert agent.qrels is None
    assert agent.rubric_ref == "rubrics/task.md"
    assert agent.allowed_side_effects == ("task:create",)
    assert agent.cleanup_rule == "删除本次会话创建的任务"
    # 校验通过的 no_answer / metric_applicability 随 VO 返回（T13 依赖）
    assert agent.no_answer is False
    assert agent.metric_applicability is None


@pytest.mark.parametrize(
    "missing_field",
    [
        "schema_version",
        "dataset_id",
        "dataset_version",
        "dataset_hash",
        "target_kind",
        "cases",
    ],
)
def test_manifest_requires_spec_fields(tmp_path, missing_field):
    content = _base_content()
    content.pop(missing_field)
    base = _write_dataset(tmp_path, content, write_fixtures=False)
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(base)


@pytest.mark.parametrize(
    "missing_field",
    ["case_id", "input", "target_kind"],
)
def test_case_requires_spec_fields(tmp_path, missing_field):
    content = _base_content()
    content["cases"] = [_agent_case()]
    content["cases"][0].pop(missing_field)
    content["dataset_hash"] = compute_dataset_hash(content)
    base = _write_dataset(tmp_path, content, write_fixtures=False)
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(base)


def test_rejects_unknown_schema_version(tmp_path):
    content = _base_content()
    content["schema_version"] = "999"
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="schema_version"):
        load_dataset_manifest(base)


def test_rejects_invalid_target_kind(tmp_path):
    content = _base_content()
    content["target_kind"] = "not_a_target"
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="target_kind"):
        load_dataset_manifest(base)


def test_expected_output_or_qrels_required(tmp_path):
    content = _base_content()
    case = _agent_case()
    case.pop("expected_output")
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content, write_fixtures=False)
    with pytest.raises(DatasetManifestError, match="expected_output"):
        load_dataset_manifest(base)


def test_duplicate_case_id_fails_fast(tmp_path):
    content = _base_content()
    content["cases"] = [_agent_case(), dict(_agent_case())]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="case_id"):
        load_dataset_manifest(base)


def test_side_effects_require_cleanup_rule(tmp_path):
    content = _base_content()
    case = _agent_case()
    case["cleanup_rule"] = ""
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="cleanup_rule"):
        load_dataset_manifest(base)


def test_absent_allowed_side_effects_loads_as_empty(tmp_path):
    """缺省 allowed_side_effects 视为空（回归锁定：可选字段缺省路径，
    缺省 tuple 默认值曾被 isinstance(list) 误拒，见 T12/T15 判断记录）。"""
    content = _base_content()
    case = _qrels_case()
    case.pop("allowed_side_effects")
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    manifest = load_dataset_manifest(base)
    assert manifest.cases[0].allowed_side_effects == ()
    assert manifest.cases[0].cleanup_rule == ""


def test_rejects_non_list_allowed_side_effects(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["allowed_side_effects"] = "task:create"
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="allowed_side_effects"):
        load_dataset_manifest(base)


def test_load_from_directory_finds_manifest_file(tmp_path):
    base = _write_dataset(tmp_path, _base_content())
    manifest = load_dataset_manifest(base)
    assert manifest.dataset_id == "kb-smoke"
    # 也支持直接指定 manifest 文件路径
    manifest2 = load_dataset_manifest(base / "manifest.json")
    assert manifest2.dataset_hash == manifest.dataset_hash


def test_missing_manifest_file_fails_fast(tmp_path):
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(tmp_path / "no-such-dataset")


def test_invalid_json_fails_fast(tmp_path):
    base = tmp_path / "broken"
    base.mkdir()
    (base / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(base)


# ---------------------------------------------------------------------------
# hash 校验：fail-fast
# ---------------------------------------------------------------------------


def test_hash_mismatch_fails_fast(tmp_path):
    content = _base_content()
    base = _write_dataset(tmp_path, content, hash_override="0" * 64)
    with pytest.raises(DatasetManifestError, match="dataset_hash"):
        load_dataset_manifest(base)


def test_case_content_drift_breaks_hash(tmp_path):
    """manifest 正文被改但 hash 未重算 → 立即终止。"""
    content = _base_content()
    base = _write_dataset(tmp_path, content)
    manifest_path = base / "manifest.json"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    on_disk["cases"][0]["input"]["query"] = "被篡改的 query"
    manifest_path.write_text(
        json.dumps(on_disk, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(DatasetManifestError, match="dataset_hash"):
        load_dataset_manifest(base)


def test_missing_fixture_file_fails_fast(tmp_path):
    content = _base_content()
    base = _write_dataset(tmp_path, content, write_fixtures=False)
    with pytest.raises(DatasetManifestError, match="fixture"):
        load_dataset_manifest(base)


def test_fixture_hash_mismatch_fails_fast(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["fixture_sha256"] = "1" * 64
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="fixture"):
        load_dataset_manifest(base)


def test_fixture_requires_declared_hash(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case.pop("fixture_sha256")
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="fixture_sha256"):
        load_dataset_manifest(base)


def test_fixture_path_escape_rejected(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["fixture"] = "../../secrets.txt"
    case["fixture_sha256"] = _fixture_sha()
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="fixture"):
        load_dataset_manifest(base)


def test_symlink_escape_rejected(tmp_path):
    """数据集目录内符号链接指向目录外的文件 → 路径逃逸，立即终止。"""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside\n", encoding="utf-8")
    content = _base_content()
    case = _qrels_case()
    case["fixture"] = "fixtures/link.txt"
    case["fixture_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    link = base / "fixtures" / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(DatasetManifestError, match="fixture"):
        load_dataset_manifest(base)


def test_rubric_file_missing_fails_fast(tmp_path):
    content = _base_content()
    content["cases"] = [_agent_case()]
    base = _write_dataset(tmp_path, content, write_fixtures=False)
    with pytest.raises(DatasetManifestError, match="rubric"):
        load_dataset_manifest(base)


def test_rubric_hash_mismatch_fails_fast(tmp_path):
    content = _base_content()
    case = _agent_case()
    case["rubric_sha256"] = "2" * 64
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="rubric"):
        load_dataset_manifest(base)


def test_rubric_requires_declared_hash(tmp_path):
    content = _base_content()
    case = _agent_case()
    case.pop("rubric_sha256")
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="rubric_sha256"):
        load_dataset_manifest(base)


def test_rubric_path_escape_rejected(tmp_path):
    content = _base_content()
    case = _agent_case()
    case["rubric_ref"] = "../../rubrics/task.md"
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="rubric"):
        load_dataset_manifest(base)


def test_loader_has_no_script_execution_touchpoints():
    """loader 不执行数据集内任何脚本：静态 AST 扫描源码，禁止
    exec/eval/compile/__import__ 调用与动态执行/网络/子进程模块导入。"""
    tree = ast.parse(LOADER_PATH.read_text(encoding="utf-8"))
    forbidden_import_roots = {
        "subprocess",
        "importlib",
        "runpy",
        "pty",
        "ctypes",
        "socket",
        "urllib",
        "http",
        "requests",
    }
    forbidden_calls = {"eval", "exec", "compile", "__import__", "system", "popen"}

    def call_name(func: ast.expr) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_import_roots
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden_import_roots
        elif isinstance(node, ast.Call):
            assert call_name(node.func) not in forbidden_calls


# ---------------------------------------------------------------------------
# qrels：身份标注、去重、no_answer 适用性
# ---------------------------------------------------------------------------


def test_qrels_dedup_by_identity(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["qrels"] = case["qrels"] + [dict(case["qrels"][0])]
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)

    manifest = load_dataset_manifest(base)
    assert len(manifest.cases[0].qrels) == 2


def test_qrels_conflicting_grades_fails_fast(tmp_path):
    content = _base_content()
    case = _qrels_case()
    conflict = dict(case["qrels"][0])
    conflict["grade"] = 0
    case["qrels"] = case["qrels"] + [conflict]
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="qrels"):
        load_dataset_manifest(base)


@pytest.mark.parametrize(
    "bad_grade",
    [3, -1, "2", True],
)
def test_qrels_grade_validation(tmp_path, bad_grade):
    content = _base_content()
    case = _qrels_case()
    case["qrels"] = [dict(case["qrels"][0], grade=bad_grade)]
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="qrels"):
        load_dataset_manifest(base)


def test_qrels_identity_key_aligns_with_chunk_identity():
    """qrels 复合身份键可无损还原为 ChunkIdentity 并直接驱动指标函数。"""
    key = qrels_identity(CORPUS_REVISION, "doc-1", "chunk-1")
    corpus_revision, document_revision, chunk_id = key.split(":")  # 分隔符 ":" 契约锁定
    identity = ChunkIdentity(
        corpus_revision=corpus_revision,
        document_revision=document_revision,
        chunk_id=chunk_id,
    )
    qrels = {identity: 2}
    value = kb_recall_at_5([identity], qrels)
    assert value.status == "value"
    assert value.typed_value == 1.0


def test_no_answer_query_requires_no_answer_metric_declaration(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["case_id"] = "kb-noanswer-001"
    case["qrels"] = [
        {
            "corpus_revision": CORPUS_REVISION,
            "document_revision": "doc-3",
            "chunk_id": "chunk-3",
            "grade": 0,
        }
    ]
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    # 未声明 no_answer 适用性 → fail-fast
    with pytest.raises(DatasetManifestError, match="no_answer"):
        load_dataset_manifest(base)

    # 声明 no_answer 且声明对应 no_answer 指标适用 → 通过
    case["no_answer"] = True
    case["metric_applicability"] = {
        "kb.recall_at_5": False,
        "kb.mrr_at_5": False,
        "kb.ndcg_at_5": False,
        "kb.no_answer_task": True,
    }
    base = _write_dataset(tmp_path, content)
    manifest = load_dataset_manifest(base)
    assert manifest.cases[0].case_id == "kb-noanswer-001"
    # no_answer 适用性声明随 VO 返回（T13 的 skipped(no_answer) 语义输入）
    assert manifest.cases[0].no_answer is True
    assert manifest.cases[0].metric_applicability == {
        "kb.recall_at_5": False,
        "kb.mrr_at_5": False,
        "kb.ndcg_at_5": False,
        "kb.no_answer_task": True,
    }


def test_no_answer_declaration_with_relevant_qrels_fails_fast(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["no_answer"] = True
    case["metric_applicability"] = {"kb.no_answer_task": True}
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="no_answer"):
        load_dataset_manifest(base)


def test_no_answer_declaration_requires_applicable_no_answer_metric(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["case_id"] = "kb-noanswer-002"
    case["qrels"] = [
        {
            "corpus_revision": CORPUS_REVISION,
            "document_revision": "doc-3",
            "chunk_id": "chunk-3",
            "grade": 0,
        }
    ]
    case["no_answer"] = True
    # 只把检索指标标为不适用，未声明任何 no_answer 指标适用 → fail-fast
    case["metric_applicability"] = {
        "kb.recall_at_5": False,
        "kb.mrr_at_5": False,
        "kb.ndcg_at_5": False,
    }
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="no_answer"):
        load_dataset_manifest(base)


# ---------------------------------------------------------------------------
# Config Bundle 边界：数据集是 repo 制品，不是运行时配置
# ---------------------------------------------------------------------------


def test_dataset_not_in_config_bundle_sections():
    """数据集不修改 Config Bundle 白名单，也不属于任何可迁移配置节；
    数据集是 evaluations/datasets/ 下受版本管理的 repo 文件，不随
    restart 清空（不进入 Config Bundle 生命周期）。"""
    from app.domain.config_bundle import SECTION_NAMES

    offending = [
        name
        for name in SECTION_NAMES
        if "dataset" in name or "evaluation" in name
    ]
    assert not offending


def test_loader_does_not_import_config_bundle():
    tree = ast.parse(LOADER_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "config_bundle" not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert "config_bundle" not in node.module


# ---------------------------------------------------------------------------
# 补充负例：精确错误分支覆盖
# ---------------------------------------------------------------------------


def test_malformed_dataset_hash_reports_format_error(tmp_path):
    content = _base_content()
    base = _write_dataset(tmp_path, content, hash_override="not-a-hash")
    with pytest.raises(DatasetManifestError, match="十六进制"):
        load_dataset_manifest(base)


def test_corpus_revision_mismatch_fails_fast(tmp_path):
    """case 声明 corpus_revision=A 而 qrels 条目标注 B → fail-fast。"""
    content = _base_content()
    case = _qrels_case()
    case["corpus_revision"] = "corpus-other"
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="corpus_revision"):
        load_dataset_manifest(base)


def test_qrels_requires_case_corpus_revision(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case.pop("corpus_revision")
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="corpus_revision"):
        load_dataset_manifest(base)


def test_no_answer_without_qrels_rejected(tmp_path):
    """no_answer 语义来自检索标注：expected_output-only case 不得声明。"""
    content = _base_content()
    case = _agent_case()
    case["no_answer"] = True
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="no_answer"):
        load_dataset_manifest(base)


def test_non_utf8_manifest_fails_fast(tmp_path):
    base = tmp_path / "bad-bytes"
    base.mkdir()
    (base / "manifest.json").write_bytes(b"\x80\x81\x82 not utf-8")
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(base)


def test_fixture_path_to_directory_fails_fast(tmp_path):
    content = _base_content()
    case = _qrels_case()
    case["fixture"] = "fixtures/dir.txt"
    case["fixture_sha256"] = _fixture_sha()
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    (base / "fixtures" / "dir.txt").mkdir()
    with pytest.raises(DatasetManifestError, match="fixture"):
        load_dataset_manifest(base)


def test_qrels_entry_missing_grade_fails_fast(tmp_path):
    content = _base_content()
    case = _qrels_case()
    entry = dict(case["qrels"][0])
    entry.pop("grade")
    case["qrels"] = [entry]
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="grade"):
        load_dataset_manifest(base)


def test_metric_applicability_non_bool_value_fails_fast(tmp_path):
    content = _base_content()
    case = _agent_case()
    case["metric_applicability"] = {"agent.task_success": "yes"}
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="metric_applicability"):
        load_dataset_manifest(base)


def test_cleanup_rule_non_string_fails_fast(tmp_path):
    content = _base_content()
    case = _agent_case()
    case["cleanup_rule"] = 123
    content["cases"] = [case]
    base = _write_dataset(tmp_path, content)
    with pytest.raises(DatasetManifestError, match="cleanup_rule"):
        load_dataset_manifest(base)


def test_manifest_json_as_directory_fails_fast(tmp_path):
    base = tmp_path / "weird"
    base.mkdir()
    (base / "manifest.json").mkdir()
    with pytest.raises(DatasetManifestError):
        load_dataset_manifest(base)
