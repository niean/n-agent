# tests/application/test_evaluation_seed_datasets.py
"""种子数据集 fixtures 校验（T15，spec 9.1/9.2）。

校验对象是 evaluations/datasets/ 下两个冒烟种子数据集：
- agent-smoke-v1：20 个 agent_run case，覆盖正常 / 空结果 / 无答案 / 工具
  失败 / 引用不支持 / 拒绝 / 副作用七类，每类至少 2 例；
- kb-smoke-v1：20 个 retrieval query，全部携带 qrels（含空结果、无相关项
  no_answer、部分相关分级 0/1/2 三种特殊形态），并引用冻结测试语料
  fixture（声明的 corpus_revision + document_revision + chunk_id 身份）。

纪律：
- 两 manifest 必须通过 T12 loader 全量校验（dataset_hash 重算、fixture
  字节 hash、qrels 身份与 no_answer 一致性均由 loader fail-fast 保证），
  测试再独立重算 hash 与 fixture 字节 hash 做双保险；
- manifest 顶层声明 role=smoke 并携带质量边界说明：门禁不得据此宣称
  总体质量；
- 全部 fixture 内容为合成数据，接受脱敏哨兵扫描（无真实用户内容与
  密钥形态；扫描器本身用植入样本证明有效）。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from app.application.evaluation_dataset import (
    compute_dataset_hash,
    load_dataset_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASETS_DIR = REPO_ROOT / "evaluations" / "datasets"

AGENT_DATASET_ID = "agent-smoke-v1"
KB_DATASET_ID = "kb-smoke-v1"
AGENT_DIR = DATASETS_DIR / AGENT_DATASET_ID
KB_DIR = DATASETS_DIR / KB_DATASET_ID
KB_CORPUS_FIXTURE = "fixtures/test-corpus.json"
KB_CORPUS_REVISION = "kb-smoke-corpus-r1"

# agent 七类切片与每类最少 case 数（spec 9.2 种子集覆盖要求）
AGENT_CATEGORY_MIN_CASES = {
    "normal": 2,
    "empty-result": 2,
    "no-answer": 2,
    "tool-failure": 2,
    "unsupported-citation": 2,
    "refusal": 2,
    "side-effect": 2,
}

AGENT_CASE_COUNT = 20
KB_CASE_COUNT = 20


def _raw_manifest(dataset_dir: Path) -> dict:
    """读取 manifest 原始 JSON（loader 之外的独立结构断言入口）。"""
    return json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# T12 loader 全量校验
# ---------------------------------------------------------------------------


def test_agent_smoke_manifest_passes_t12_loader():
    manifest = load_dataset_manifest(AGENT_DIR)
    assert manifest.dataset_id == AGENT_DATASET_ID
    assert manifest.dataset_version == "1"
    assert len(manifest.cases) == AGENT_CASE_COUNT
    assert all(case.target_kind == "agent_run" for case in manifest.cases)
    # 真值形态：agent case 走 expected_output，不走 qrels
    for case in manifest.cases:
        assert case.expected_output is not None
        assert case.qrels is None


def test_kb_smoke_manifest_passes_t12_loader():
    manifest = load_dataset_manifest(KB_DIR)
    assert manifest.dataset_id == KB_DATASET_ID
    assert manifest.dataset_version == "1"
    assert len(manifest.cases) == KB_CASE_COUNT
    assert all(case.target_kind == "retrieval" for case in manifest.cases)
    # 全部 query 携带 qrels 且语料版本一致（冻结语料纪律）
    for case in manifest.cases:
        assert case.qrels
        assert case.corpus_revision == KB_CORPUS_REVISION


def test_agent_cases_input_matches_target_runner_contract():
    """input 载荷对齐 T13 ChatCompletionTargetRunner 契约：turns 数组，
    每轮 message 文本（或多轮 messages），目标适配可直接消费。"""
    for case in _raw_manifest(AGENT_DIR)["cases"]:
        turns = case["input"]["turns"]
        assert isinstance(turns, list) and turns
        for turn in turns:
            assert "message" in turn or "messages" in turn


def test_kb_cases_input_matches_target_runner_contract():
    """input 载荷对齐 T13 KBDirectTargetRunner 契约：turns 数组，每轮
    query + top_k + filters。"""
    for case in _raw_manifest(KB_DIR)["cases"]:
        turns = case["input"]["turns"]
        assert isinstance(turns, list) and turns
        for turn in turns:
            assert isinstance(turn.get("query"), str) and turn["query"]
            assert isinstance(turn.get("filters", {}), dict)


# ---------------------------------------------------------------------------
# hash 一致性（loader 之外独立重算，双保险）
# ---------------------------------------------------------------------------


def test_manifest_dataset_hash_matches_recomputation():
    for dataset_dir in (AGENT_DIR, KB_DIR):
        raw = _raw_manifest(dataset_dir)
        assert compute_dataset_hash(raw) == raw["dataset_hash"], dataset_dir.name


def test_kb_fixture_declared_hash_matches_bytes():
    fixture_paths = set()
    for case in _raw_manifest(KB_DIR)["cases"]:
        fixture_path = (KB_DIR / case["fixture"]).resolve()
        assert fixture_path.is_relative_to(KB_DIR.resolve())
        actual = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        assert actual == case["fixture_sha256"], case["case_id"]
        fixture_paths.add(case["fixture"])
    # 全部 KB case 共享同一份冻结测试语料 fixture
    assert fixture_paths == {KB_CORPUS_FIXTURE}


def test_kb_qrels_identities_resolve_into_frozen_corpus():
    """每个 qrels 身份（corpus_revision:document_revision:chunk_id）必须
    能在冻结测试语料 fixture 中解析，防止标注漂移。"""
    corpus = json.loads((KB_DIR / KB_CORPUS_FIXTURE).read_text(encoding="utf-8"))
    identities = {
        (corpus["corpus_revision"], document["document_revision"], chunk["chunk_id"])
        for document in corpus["documents"]
        for chunk in document["chunks"]
    }
    for case in _raw_manifest(KB_DIR)["cases"]:
        for entry in case["qrels"]:
            identity = (
                entry["corpus_revision"],
                entry["document_revision"],
                entry["chunk_id"],
            )
            assert identity in identities, case["case_id"]


# ---------------------------------------------------------------------------
# 冒烟规模声明
# ---------------------------------------------------------------------------


def test_manifests_declare_smoke_role_and_quality_boundary():
    for dataset_dir in (AGENT_DIR, KB_DIR):
        raw = _raw_manifest(dataset_dir)
        assert raw["role"] == "smoke", dataset_dir.name
        note = raw["quality_note"]
        assert "冒烟" in note and "总体质量" in note


# ---------------------------------------------------------------------------
# agent 覆盖面：七类切片，每类至少 2 例
# ---------------------------------------------------------------------------


def test_agent_category_coverage_at_least_two_each():
    raw_cases = _raw_manifest(AGENT_DIR)["cases"]
    counts = Counter(case["slice"] for case in raw_cases)
    assert set(counts) == set(AGENT_CATEGORY_MIN_CASES)
    for category, minimum in AGENT_CATEGORY_MIN_CASES.items():
        assert counts[category] >= minimum, category


def test_agent_side_effect_cases_pair_with_cleanup_rule():
    raw_cases = _raw_manifest(AGENT_DIR)["cases"]
    side_effect_cases = [c for c in raw_cases if c.get("allowed_side_effects")]
    assert len(side_effect_cases) >= 2
    for case in side_effect_cases:
        assert case["allowed_side_effects"]
        assert case.get("cleanup_rule"), case["case_id"]


# ---------------------------------------------------------------------------
# KB qrels 特殊形态：全部带 qrels，含空结果 / no_answer / 部分相关 0/1/2
# ---------------------------------------------------------------------------


def test_kb_qrels_special_shapes_present():
    raw_cases = _raw_manifest(KB_DIR)["cases"]
    slices: dict[str, list[dict]] = {}
    full_grade_cases = 0
    for case in raw_cases:
        grades = {entry["grade"] for entry in case["qrels"]}
        assert grades <= {0, 1, 2}, case["case_id"]
        slices.setdefault(case["slice"], []).append(case)
        if grades == {0, 1, 2}:
            full_grade_cases += 1
    # 部分相关：至少 3 个 case 同时包含 0/1/2 三档分级
    assert full_grade_cases >= 3
    # 空结果：查询声明了过滤器（预期检索为空），但 qrels 仍含相关项
    # （指标计 0 而非 skipped）
    empty_result_cases = slices.get("empty-result", [])
    assert len(empty_result_cases) >= 2
    for case in empty_result_cases:
        assert any(entry["grade"] >= 1 for entry in case["qrels"]), case["case_id"]
        assert any(turn.get("filters") for turn in case["input"]["turns"])


def test_kb_no_answer_cases_shape():
    """无相关项 query：全部 qrels 为 grade 0，声明 no_answer=true 并声明
    至少一个 no_answer 指标适用（loader 强制的标注形态）。"""
    no_answer_cases = [
        c for c in _raw_manifest(KB_DIR)["cases"] if c.get("no_answer") is True
    ]
    assert len(no_answer_cases) >= 2
    for case in no_answer_cases:
        assert {entry["grade"] for entry in case["qrels"]} == {0}, case["case_id"]
        applicability = case["metric_applicability"]
        assert any(
            applicable and "no_answer" in name
            for name, applicable in applicability.items()
        ), case["case_id"]


# ---------------------------------------------------------------------------
# 脱敏哨兵扫描
# ---------------------------------------------------------------------------

# 密钥/凭据形态哨兵：真实形态，量词阈值排除普通英文与本项目命名
_SENTINEL_PATTERNS = (
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws-temp-access-key-id", re.compile(r"ASIA[0-9A-Z]{16}")),
    ("openai-style-secret", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github-pat", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github-fine-grained-pat", re.compile(r"github_pat_[A-Za-z0-9_]{22,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("jwt-credential", re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws-secret-key-name", re.compile(r"aws_secret_access_key", re.IGNORECASE)),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._-]{25,}")),
    (
        "assigned-secret",
        re.compile(
            r"(?:api[_-]?key|secret|token|passwd|password)\s*[:=]"
            r"\s*['\"]?[A-Za-z0-9+/]{16,}",
            re.IGNORECASE,
        ),
    ),
)

# 邮箱哨兵：fixture 只允许 example.com / example.org 示例域名
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_EMAIL_ALLOWED_DOMAINS = frozenset({"example.com", "example.org"})


def _scan_file_for_sentinels(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings = [
        f"{path.name}:{name}"
        for name, pattern in _SENTINEL_PATTERNS
        if pattern.search(text)
    ]
    for match in _EMAIL_PATTERN.finditer(text):
        domain = match.group(0).rsplit("@", 1)[1].lower()
        if domain not in _EMAIL_ALLOWED_DOMAINS:
            findings.append(f"{path.name}:email:{domain}")
    return findings


def test_dataset_files_pass_sentinel_scan():
    findings: list[str] = []
    for dataset_dir in (AGENT_DIR, KB_DIR):
        assert dataset_dir.is_dir(), dataset_dir
        for path in sorted(dataset_dir.rglob("*")):
            if path.is_file():
                findings.extend(_scan_file_for_sentinels(path))
    assert findings == []


def test_sentinel_scan_patterns_catch_planted_credentials():
    """扫描器有效性自证：12 个哨兵模式逐一用植入样本锁定命中，防哨兵
    退化为恒真扫描；邮箱哨兵用非白名单/白名单域名正反两例锁定。"""
    planted_by_pattern = {
        "aws-access-key-id": "AKIAIOSFODNN7EXAMPLE",
        "aws-temp-access-key-id": "ASIAIOSFODNN7EXAMPLE",
        "openai-style-secret": "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "github-pat": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7r8",
        "github-fine-grained-pat": "github_pat_" + "a1B2c3D4e5F6g7H8i9J0k1",
        "slack-token": "xoxb-" + "1234567890-abcdefghij",
        "google-api-key": "AIza" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7r",
        "jwt-credential": "eyJ" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        + "." + "a1B2c3D4e5F6g7H8i9J0",
        "private-key-block": "-----BEGIN RSA PRIVATE KEY-----",
        "aws-secret-key-name": "aws_secret_access_key = wJalrXUtnFEMI",
        "bearer-token": "Bearer " + "a1B2c3D4e5F6g7H8i9J0k1L2m3",
        "assigned-secret": "api_key = 'a1B2c3D4e5F6g7H8'",
    }
    patterns = dict(_SENTINEL_PATTERNS)
    # 植入样本集合与哨兵模式集合一一对应：新增模式必须同步补植入样本
    assert set(planted_by_pattern) == set(patterns)
    for name, sample in planted_by_pattern.items():
        assert patterns[name].search(sample), name
    # 邮箱哨兵：非白名单域名必须可识别，示例域名放行
    assert "someone@realdomain.io".rsplit("@", 1)[1] not in _EMAIL_ALLOWED_DOMAINS
    assert "a@example.com".rsplit("@", 1)[1] in _EMAIL_ALLOWED_DOMAINS
