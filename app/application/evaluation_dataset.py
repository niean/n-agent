# app/application/evaluation_dataset.py
"""DatasetManifest 加载与校验（spec 9.1，T12）。

数据集是 evaluations/datasets/ 下受版本管理的 JSON manifest + 附件目录，
不是运行时配置：不进入 Config Bundle 白名单（app/domain/config_bundle.py
SECTION_NAMES），不随服务 restart 清空。

安全纪律（spec 9.1，全部 fail-fast，任一失败立即终止、不运行任何 case）：
- manifest 内容 hash 与声明 dataset_hash 一致（canonical JSON 减去
  dataset_hash 字段后的 SHA-256，锚定 domain 的 canonical_json_bytes）；
- 附件/fixture 与 rubric 材料（rubric_ref）路径必须存在于数据集目录内且
  声明的 SHA-256 与文件字节一致，禁止路径逃逸（绝对路径、.. 逃逸、符号
  链接逃逸均拒绝）；
- 不执行数据集携带的任何脚本：本模块只有 stdlib 声明式解析（json/hashlib/
  pathlib），无 exec/eval/compile/__import__/子进程/网络触点（由
  tests/application/test_evaluation_dataset.py 静态 AST 断言锁定）。

qrels 语义对齐 domain/metrics_deterministic.py：检索身份 =
corpus_revision + document_revision + chunk_id（ChunkIdentity），manifest
中按条目标注、按身份去重（同身份同分级去重、同身份冲突分级 fail-fast）；
声明 qrels 的 case 必须声明 corpus_revision 且全部条目与其一致。
无相关项 query（qrels 无 grade>=1 条目）必须声明 no_answer=True 并声明
至少一个 no_answer 指标适用（spec 8/9.2：避免把"不应命中"排除后掩盖
误答）；no_answer=True 反过来要求 case 携带 qrels——no_answer 是检索
标注语义，expected_output-only 的 case 不允许声明。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from app.application.evaluation_io import (
    compute_content_hash,
    read_json_object,
    require_sha256_hex,
)
from app.domain.evaluation.models import (
    TARGET_KINDS,
    DatasetCase,
    DatasetManifest,
    EvaluationValidationError,
)

# manifest schema 版本；字段语义破坏性变化时递增并迁移
DATASET_SCHEMA_VERSION = "1"

# 数据集目录内的 manifest 文件名
MANIFEST_FILENAME = "manifest.json"

# qrels 复合身份键分隔符：corpus_revision:document_revision:chunk_id
# （与 ChunkIdentity 字段顺序一致；三字段均禁止包含 ":"）
QRELS_IDENTITY_SEPARATOR = ":"

# dataset_hash 字段名（必填字段表与 compute_dataset_hash 共用单一常量）
_DATASET_HASH_FIELD = "dataset_hash"


class DatasetManifestError(ValueError):
    """数据集 manifest 加载或校验失败（fail-fast，调用方必须立即终止）。"""


# manifest / case 的必填字段
_MANIFEST_REQUIRED_FIELDS = (
    "schema_version",
    "dataset_id",
    "dataset_version",
    _DATASET_HASH_FIELD,
    "target_kind",
    "cases",
)
_CASE_REQUIRED_FIELDS = ("case_id", "input", "target_kind")

# qrels 相关性分级词汇（spec 8：0/1/2 分级；Recall/MRR 阈值 rel>=1）
_QRELS_GRADES = (0, 1, 2)

# qrels 身份字段（顺序即 qrels_identity 参数顺序）
_QRELS_IDENTITY_FIELDS = ("corpus_revision", "document_revision", "chunk_id")

# no_answer 指标名称标记：metric_applicability 中名称包含该标记且为 True
# 的条目视为"对应的 no_answer 指标适用性声明"
_NO_ANSWER_METRIC_MARKER = "no_answer"


def qrels_identity(
    corpus_revision: str, document_revision: str, chunk_id: str
) -> str:
    """构造 qrels 复合身份键（与 ChunkIdentity 字段顺序一致）。

    与 metrics_deterministic.ChunkIdentity 的对齐契约：键按本分隔符拆分
    后可无损还原为 ChunkIdentity。身份字段校验（非空字符串、不含分隔符）
    以本函数为唯一权威，manifest 解析侧直接复用。
    """
    for label, value in zip(_QRELS_IDENTITY_FIELDS, (corpus_revision, document_revision, chunk_id)):
        if not isinstance(value, str) or not value:
            raise DatasetManifestError(
                f"qrels 身份 {label} 必须是非空字符串: {value!r}"
            )
        if QRELS_IDENTITY_SEPARATOR in value:
            raise DatasetManifestError(
                f"qrels 身份 {label} 不得包含分隔符 "
                f"{QRELS_IDENTITY_SEPARATOR!r}: {value!r}"
            )
    return QRELS_IDENTITY_SEPARATOR.join(
        (corpus_revision, document_revision, chunk_id)
    )


def compute_dataset_hash(content: Mapping[str, Any]) -> str:
    """计算 manifest 内容 hash：canonical JSON（去掉 dataset_hash 字段）
    的 SHA-256。作者侧生成与加载侧校验必须使用同一函数。"""
    return compute_content_hash(content, exclude_field=_DATASET_HASH_FIELD)


def load_dataset_manifest(path: str | Path) -> DatasetManifest:
    """从数据集目录（含 manifest.json）或 manifest 文件路径加载数据集。

    任一校验失败抛 DatasetManifestError，立即终止（不返回部分结果、
    不执行任何 case 脚本）。
    """
    resolved = Path(path)
    if resolved.is_dir():
        resolved = resolved / MANIFEST_FILENAME
    if not resolved.is_file():
        raise DatasetManifestError(
            f"manifest 文件不存在: {resolved}（目录需包含 {MANIFEST_FILENAME}）"
        )
    content = read_json_object(resolved, error=DatasetManifestError)
    return _validate_manifest(content, base_dir=resolved.parent)


# ---------------------------------------------------------------------------
# 内部校验（fail-fast）
# ---------------------------------------------------------------------------


def _validate_manifest(content: Any, base_dir: Path) -> DatasetManifest:
    if not isinstance(content, Mapping):
        raise DatasetManifestError(
            f"manifest 顶层必须是 JSON 对象: {type(content).__name__}"
        )

    for field_name in _MANIFEST_REQUIRED_FIELDS:
        if field_name not in content:
            raise DatasetManifestError(f"manifest 缺少必填字段: {field_name}")

    schema_version = content["schema_version"]
    if schema_version != DATASET_SCHEMA_VERSION:
        raise DatasetManifestError(
            f"不支持的 manifest schema_version: {schema_version!r}，"
            f"仅支持 {DATASET_SCHEMA_VERSION!r}"
        )

    dataset_id = _require_str(content["dataset_id"], "dataset_id")
    dataset_version = _require_str(content["dataset_version"], "dataset_version")
    declared_hash = _require_sha256_hex(content[_DATASET_HASH_FIELD])
    actual_hash = compute_dataset_hash(content)
    if actual_hash != declared_hash:
        raise DatasetManifestError(
            f"dataset_hash 校验失败（立即终止）: 声明 {declared_hash!r} "
            f"与内容重算 {actual_hash!r} 不一致"
        )

    target_kind = content["target_kind"]
    if target_kind not in TARGET_KINDS:
        raise DatasetManifestError(
            f"非法 manifest target_kind: {target_kind!r}，仅允许 {TARGET_KINDS}"
        )

    cases_raw = content["cases"]
    if not isinstance(cases_raw, list) or not cases_raw:
        raise DatasetManifestError("cases 必须是非空数组")

    cases = tuple(
        _validate_case(case, index=index, base_dir=base_dir)
        for index, case in enumerate(cases_raw)
    )
    _require_unique_case_ids(cases)

    try:
        return DatasetManifest(
            schema_version=DATASET_SCHEMA_VERSION,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_hash=declared_hash,
            target_kind=target_kind,
            cases=cases,
        )
    except EvaluationValidationError as exc:
        raise DatasetManifestError(str(exc)) from exc


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DatasetManifestError(f"{label} 必须是非空字符串: {value!r}")
    return value


def _require_sha256_hex(value: Any) -> str:
    return require_sha256_hex(
        value, _DATASET_HASH_FIELD, error=DatasetManifestError
    )


def _require_unique_case_ids(cases: tuple[DatasetCase, ...]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise DatasetManifestError(
                f"case_id 重复（立即终止）: {case.case_id!r}"
            )
        seen.add(case.case_id)


def _validate_case(
    case: Any, index: int, base_dir: Path
) -> DatasetCase:
    where = f"cases[{index}]"
    if not isinstance(case, Mapping):
        raise DatasetManifestError(f"{where} 必须是 JSON 对象: {type(case).__name__}")
    for field_name in _CASE_REQUIRED_FIELDS:
        if field_name not in case:
            raise DatasetManifestError(f"{where} 缺少必填字段: {field_name}")

    case_id, case_input, case_target_kind = _validate_case_identity(case, where)
    qrels = _validate_truth_form(case, case_id)
    no_answer = case.get("no_answer", False)
    if not isinstance(no_answer, bool):
        raise DatasetManifestError(
            f"case {case_id!r} no_answer 必须是 bool: {no_answer!r}"
        )
    applicability = _validate_metric_applicability(
        case.get("metric_applicability"), case_id
    )
    _validate_no_answer_consistency(
        no_answer=no_answer, qrels=qrels, applicability=applicability,
        case_where=case_id,
    )
    corpus_revision = _validate_corpus_revision(case, qrels, case_id)
    fixture = _require_paired_file(
        case, path_field="fixture", sha_field="fixture_sha256",
        base_dir=base_dir, case_where=case_id,
    )
    rubric_ref = _require_paired_file(
        case, path_field="rubric_ref", sha_field="rubric_sha256",
        base_dir=base_dir, case_where=case_id,
    )
    allowed_side_effects, cleanup_rule = _validate_side_effects(case, case_id)
    slice_value = case.get("slice", "")
    if not isinstance(slice_value, str):
        raise DatasetManifestError(
            f"case {case_id!r} slice 必须是字符串: {slice_value!r}"
        )

    try:
        return DatasetCase(
            case_id=case_id,
            input=dict(case_input),
            target_kind=case_target_kind,
            slice=slice_value,
            expected_output=case.get("expected_output"),
            qrels=qrels,
            rubric_ref=rubric_ref,
            fixture=fixture,
            corpus_revision=corpus_revision,
            allowed_side_effects=allowed_side_effects,
            cleanup_rule=cleanup_rule,
            no_answer=no_answer,
            metric_applicability=applicability or None,
        )
    except EvaluationValidationError as exc:
        raise DatasetManifestError(f"case {case_id!r}: {exc}") from exc


def _validate_case_identity(case: Mapping[str, Any], where: str) -> tuple[str, Mapping[str, Any], str]:
    """case_id / input / target_kind 三项身份与载荷校验。"""
    case_id = _require_str(case["case_id"], f"{where}.case_id")
    case_input = case["input"]
    if not isinstance(case_input, Mapping):
        raise DatasetManifestError(
            f"case {case_id!r} input 必须是 JSON 对象"
        )
    case_target_kind = case["target_kind"]
    if case_target_kind not in TARGET_KINDS:
        raise DatasetManifestError(
            f"case {case_id!r} 非法 target_kind: {case_target_kind!r}，"
            f"仅允许 {TARGET_KINDS}"
        )
    return case_id, case_input, case_target_kind


def _validate_truth_form(case: Mapping[str, Any], case_id: str) -> Mapping[str, int] | None:
    """真值形态校验：expected_output 或 qrels 至少其一，返回解析后的
    qrels 复合身份键映射（无 qrels 为 None）。"""
    has_expected = case.get("expected_output") is not None
    qrels = None
    if case.get("qrels") is not None:
        qrels = _validate_qrels(case["qrels"], case_id)
    if not has_expected and qrels is None:
        raise DatasetManifestError(
            f"case {case_id!r} 必须声明 expected_output 或 qrels 之一（真值形态）"
        )
    return qrels


def _validate_qrels(qrels_raw: Any, case_id: str) -> Mapping[str, int]:
    if not isinstance(qrels_raw, list) or not qrels_raw:
        raise DatasetManifestError(f"case {case_id!r} qrels 必须是非空数组")
    deduped: dict[str, int] = {}
    for position, entry in enumerate(qrels_raw):
        where = f"case {case_id!r} qrels[{position}]"
        if not isinstance(entry, Mapping):
            raise DatasetManifestError(f"{where} 必须是 JSON 对象: {entry!r}")
        try:
            identity = qrels_identity(
                *(entry.get(field) for field in _QRELS_IDENTITY_FIELDS)
            )
        except DatasetManifestError as exc:
            raise DatasetManifestError(f"{where}: {exc}") from exc
        grade = entry.get("grade")
        if isinstance(grade, bool) or not isinstance(grade, int):
            raise DatasetManifestError(f"{where} grade 必须是 int 分级: {grade!r}")
        if grade not in _QRELS_GRADES:
            raise DatasetManifestError(
                f"{where} grade 仅允许 {_QRELS_GRADES}: {grade!r}"
            )
        existing = deduped.get(identity)
        if existing is not None and existing != grade:
            raise DatasetManifestError(
                f"case {case_id!r} qrels 身份冲突（立即终止）: {identity!r} "
                f"同时标注 grade={existing} 与 grade={grade}；"
                f"同身份分级必须唯一"
            )
        # 同身份同分级：按身份去重，只保留一份标注
        deduped[identity] = grade
    return deduped


def _validate_corpus_revision(
    case: Mapping[str, Any], qrels: Mapping[str, int] | None, case_id: str
) -> str | None:
    """corpus_revision 校验：声明 qrels 的 case 必须声明 corpus_revision
    且全部 qrels 条目的语料版本与其一致（防止基准语料漂移）。"""
    corpus_revision = case.get("corpus_revision")
    if qrels is not None:
        _require_str(
            corpus_revision, f"case {case_id!r} corpus_revision（声明 qrels 必填）"
        )
        mismatched = [
            key for key in qrels
            if key.split(QRELS_IDENTITY_SEPARATOR)[0] != corpus_revision
        ]
        if mismatched:
            raise DatasetManifestError(
                f"case {case_id!r} qrels 语料版本与 corpus_revision 声明不一致"
                f"（立即终止）: 声明 {corpus_revision!r}，条目 "
                f"{mismatched[0]!r}"
            )
    elif corpus_revision is not None:
        _require_str(corpus_revision, f"case {case_id!r} corpus_revision")
    return corpus_revision or None


def _validate_metric_applicability(
    applicability_raw: Any, case_id: str
) -> Mapping[str, bool]:
    if applicability_raw is None:
        return {}
    if not isinstance(applicability_raw, Mapping):
        raise DatasetManifestError(
            f"case {case_id!r} metric_applicability 必须是 JSON 对象"
            f"（指标名 -> bool）"
        )
    for metric_name, applicable in applicability_raw.items():
        _require_str(metric_name, f"case {case_id!r} metric_applicability 键")
        if not isinstance(applicable, bool):
            raise DatasetManifestError(
                f"case {case_id!r} metric_applicability[{metric_name!r}] "
                f"必须是 bool: {applicable!r}"
            )
    return dict(applicability_raw)


def _validate_no_answer_consistency(
    no_answer: bool,
    qrels: Mapping[str, int] | None,
    applicability: Mapping[str, bool],
    case_where: str,
) -> None:
    # no_answer 是检索标注语义：expected_output-only 的 case 不得声明
    if no_answer and qrels is None:
        raise DatasetManifestError(
            f"case {case_where!r} no_answer=true 必须携带 qrels"
            f"（no_answer 语义来自检索标注，无 qrels 无从判定）"
        )
    has_relevant = bool(qrels) and any(grade >= 1 for grade in qrels.values())
    if qrels is not None and not has_relevant:
        # 无相关项 query：对 Recall/MRR/NDCG 是 skipped(no_answer)，
        # 必须显式声明 no_answer 并声明至少一个 no_answer 指标适用
        # （spec 8/9.2：不能把"不应命中"排除后掩盖误答）
        if not no_answer:
            raise DatasetManifestError(
                f"case {case_where!r} qrels 无相关项（无 grade>=1 条目）必须声明 "
                f"no_answer=true 并声明对应 no_answer 指标适用"
                f"（metric_applicability）"
            )
        applicable_no_answer = [
            name
            for name, applicable in applicability.items()
            if applicable and _NO_ANSWER_METRIC_MARKER in name
        ]
        if not applicable_no_answer:
            raise DatasetManifestError(
                f"case {case_where!r} no_answer query 必须声明至少一个 no_answer 指标"
                f"适用（metric_applicability 中名称含 "
                f"{_NO_ANSWER_METRIC_MARKER!r} 且为 true）"
            )
    if no_answer and has_relevant:
        raise DatasetManifestError(
            f"case {case_where!r} 声明 no_answer=true 但 qrels 存在 grade>=1 相关项，"
            f"声明与标注矛盾"
        )


def _require_paired_file(
    case: Mapping[str, Any],
    path_field: str,
    sha_field: str,
    base_dir: Path,
    case_where: str,
) -> str | None:
    """路径字段与 hash 字段成对校验（fixture / rubric 材料共用）：
    声明路径必须同时声明 hash 并通过文件校验；只声明 hash 拒绝。"""
    path_value = case.get(path_field)
    if path_value is None:
        if case.get(sha_field) is not None:
            raise DatasetManifestError(
                f"case {case_where!r} 声明了 {sha_field} 但未声明 {path_field}"
            )
        return None
    path = _require_str(path_value, f"case {case_where!r} {path_field}")
    declared_sha256 = _require_str(
        case.get(sha_field), f"case {case_where!r} {sha_field}"
    )
    _validate_dataset_file(
        path, declared_sha256, base_dir=base_dir,
        case_where=case_where, label=path_field,
    )
    return path


def _validate_side_effects(
    case: Mapping[str, Any], case_id: str
) -> tuple[tuple[str, ...], str]:
    """副作用与清理规则校验：声明 allowed_side_effects 必须同时声明
    cleanup_rule（单向要求；仅声明 cleanup_rule 允许）。"""
    # 可选字段缺省视为空列表（T12 缺省路径修复：此前默认值 () 为 tuple，
    # 会被下方 isinstance(list) 检查误拒省略该字段的合法 case）
    side_effects_raw = case.get("allowed_side_effects", [])
    if not isinstance(side_effects_raw, list):
        raise DatasetManifestError(
            f"case {case_id!r} allowed_side_effects 必须是字符串数组: "
            f"{side_effects_raw!r}"
        )
    allowed_side_effects = tuple(
        _require_str(item, f"case {case_id!r} allowed_side_effects 条目")
        for item in side_effects_raw
    )
    cleanup_rule = case.get("cleanup_rule", "")
    if not isinstance(cleanup_rule, str):
        raise DatasetManifestError(
            f"case {case_id!r} cleanup_rule 必须是字符串: {cleanup_rule!r}"
        )
    if allowed_side_effects and not cleanup_rule:
        raise DatasetManifestError(
            f"case {case_id!r} 声明了 allowed_side_effects 必须同时声明清理规则 "
            f"cleanup_rule（spec 9.1 副作用与清理成对出现）"
        )
    return allowed_side_effects, cleanup_rule


def _validate_dataset_file(
    relative_path: str,
    declared_sha256: str,
    base_dir: Path,
    case_where: str,
    label: str,
) -> None:
    """数据集内引用文件（fixture / rubric 材料）的统一校验纪律：
    目录内相对路径 + 文件存在 + 字节 SHA-256 与声明一致，任一失败立即终止。"""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise DatasetManifestError(
            f"case {case_where!r} {label} 路径必须是数据集目录内相对路径: "
            f"{relative_path!r}"
        )
    resolved = (base_dir / relative).resolve()
    base_resolved = base_dir.resolve()
    # resolve() 穿透符号链接：软链指向数据集目录外同样被拒绝
    if not resolved.is_relative_to(base_resolved):
        raise DatasetManifestError(
            f"case {case_where!r} {label} 路径逃逸数据集目录（立即终止）: "
            f"{relative_path!r}"
        )
    if not resolved.is_file():
        raise DatasetManifestError(
            f"case {case_where!r} {label} 文件不存在（立即终止）: {relative_path!r}"
        )
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if actual != declared_sha256:
        raise DatasetManifestError(
            f"case {case_where!r} {label} hash 校验失败（立即终止）: "
            f"{relative_path!r} 声明 {declared_sha256!r} 与实际 {actual!r} 不一致"
        )
