"""评估待复核队列与审核后受控导出（plan T21，spec 15 管理面扩展）。

管理用例：

- review_queue：管理页待复核列表。三类原因（不能用 status 过滤代替）：
  - ``low_score``：数值结果低于阈值，或 boolean 结果 passed=False；
  - ``disputed``：同一 (metric_name, target subject) 上自动结果与人评
    结果值冲突（store.list_disputed_results 权威查询）；
  - ``anomalous``：status=error/skipped 的异常结果。
- list_datasets/get_dataset/import_dataset：数据集版本列表、校验后详情、内联标注导入；导入复用审核导出及审计。
- export_reviewed_dataset：审核后脱敏导出新 manifest 的受控路径。
  金标只能来自审核人提供的 truth（expected_output/qrels），禁止从
  Judge 结果自动生成；导出记录审批来源（export audit，含
  source_result_ids，只留审计不留 manifest）；case 载荷递归拒绝身份
  键（session_id/trace_id 等，原始身份不外带）；manifest 组装后经
  T12 权威加载器 ``load_dataset_manifest`` fail-fast 校验（fixture/
  rubric 配对文件存在 + sha 匹配 + dataset_hash 重算一致），落盘失败
  即终止，不产出半截数据集。

路由层（T21 管理面）异常映射契约：
- EvaluationValidationError / DatasetManifestError -> 422
- EvaluationConflictError -> 409
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import uuid
from pathlib import Path
from typing import Any, Mapping

from app.application.evaluation_dataset import (
    DatasetManifestError,
    compute_dataset_hash,
    load_dataset_manifest,
)
from app.domain.evaluation.errors import EvaluationConflictError, EvaluationNotFoundError
from app.domain.evaluation.models import EvaluationValidationError

logger = logging.getLogger(__name__)


class EvaluationIdentityLeakError(EvaluationValidationError):
    """导出 case 载荷含身份键（路由层映射 422/identity_leak，与一般
    导出形态错误区分，便于管理页精确提示）。"""


# 待复核原因封闭集（管理页筛选；status 过滤无法表达 low_score/disputed）
REVIEW_REASONS: tuple[str, ...] = ("low_score", "disputed", "anomalous")

# low_score 默认阈值（numeric 结果的 lower-is-worse 分界；管理面可用
# threshold 查询参数覆盖）
DEFAULT_LOW_SCORE_THRESHOLD = 0.5

# 身份键黑名单：case 载荷（递归）出现任一键即拒绝导出（422
# identity_leak）。这些字段是会话/结果级原始身份，导出数据集必须解除
# 原始身份（审核来源只记录在 export audit，不进 manifest）。
# M2：匹配前对键做小写并去下划线/连字符归一化，Session_ID/sessionId/
# traceId 等大小写与分隔符变体同被拦截（camelCase/kebab-case 无需逐
# 别名枚举）。
_IDENTITY_KEYS: frozenset[str] = frozenset({
    "session_id",
    "trace_id",
    "span_id",
    "run_id",
    "retrieval_id",
    "result_id",
    "human_result_id",
    "feedback_id",
    "job_id",
    "producer_event_id",
    "actor_ref",
})


def _normalize_identity_key(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


# 归一化后的身份键集（定义处先收集原键，再统一小写去分隔符，供
# _reject_identity_keys 做归一化匹配）
_IDENTITY_KEYS_NORMALIZED: frozenset[str] = frozenset(
    _normalize_identity_key(key) for key in _IDENTITY_KEYS
)


# 导出 case 必填字段（T12 manifest case 契约的导出侧子集；input 是
# JSON 对象、case_id 是路径安全字符串、fixture/rubric 双模式各自分支
# 校验，完整性由 load_dataset_manifest fail-fast 兜底）
_EXPORT_CASE_REQUIRED_FIELDS: tuple[str, ...] = ("target_kind",)

# 单次复核扫描的结果页大小与总上限（防止单请求无界分页）
_QUEUE_PAGE_SIZE = 200
_QUEUE_MAX_RESULTS = 1000


class EvaluationReviewService:
    """待复核队列与审核后受控导出（管理面专用应用服务）。

    ``evidence_reader`` 可选（LocalEvidenceStore 形状：load_snapshot），
    提供时待复核条目附样本真实 query（金标仍只来自审核人，query 是
    证据输入不是金标）；未提供时条目无 query 字段。"""

    def __init__(
        self,
        *,
        store: Any,
        datasets_root: str | Path,
        evidence_reader: Any = None,
    ) -> None:
        self._store = store
        self._datasets_root = Path(datasets_root)
        self._evidence_reader = evidence_reader
        self._dataset_write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 待复核队列
    # ------------------------------------------------------------------

    async def review_queue(
        self,
        *,
        reason: str,
        threshold: float = DEFAULT_LOW_SCORE_THRESHOLD,
        limit: int = 50,
    ) -> list[Mapping[str, Any]]:
        """按原因返回待复核结果列表（每项附加 ``reason`` 字段）。

        口径（Mi1）：条目保留 result 行的 explanation（Judge 解释全文）——
        审核人复核争议/低分时需要解释全文作判断依据；results 列表端点
        才剥离正文（管理页浏览面不默认返回正文）。"""
        if reason not in REVIEW_REASONS:
            raise EvaluationValidationError(
                f"非法 review 原因: {reason!r}，仅允许 {REVIEW_REASONS}"
            )
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise EvaluationValidationError("threshold 必须是数值")
        # Mi4：NaN/inf 阈值会使全部数值比较为 False（静默空列表），显式拒绝
        if not math.isfinite(threshold):
            raise EvaluationValidationError("threshold 必须是有限数值")
        if limit <= 0:
            raise EvaluationValidationError("limit 必须是正整数")
        limit = min(limit, 200)
        if reason == "disputed":
            rows = await self._store.list_disputed_results(limit=limit)
            return [await self._attach_query(dict(row, reason="disputed")) for row in rows]
        if reason == "anomalous":
            # N3：error/skipped 轮转合并（顺序扫描会让 error 打满 limit
            # 时 skipped 永不展示）；仍按 limit 封顶
            error_rows = (await self._store.query_results_page(
                status="error", limit=limit
            ))[0]
            skipped_rows = (await self._store.query_results_page(
                status="skipped", limit=limit
            ))[0]
            merged: list[Mapping[str, Any]] = []
            for pair in itertools.zip_longest(error_rows, skipped_rows):
                for row in pair:
                    if row is not None and len(merged) < limit:
                        merged.append(
                            await self._attach_query(
                                dict(row, reason="anomalous")
                            )
                        )
            return merged
        # low_score：扫描 current 视图 value 结果（分页，封顶
        # _QUEUE_MAX_RESULTS），numeric < threshold 或 boolean passed=False
        collected: list[Mapping[str, Any]] = []
        cursor: str | None = None
        scanned = 0
        while scanned < _QUEUE_MAX_RESULTS and len(collected) < limit:
            page_size = min(_QUEUE_PAGE_SIZE, _QUEUE_MAX_RESULTS - scanned)
            rows, next_token = await self._store.query_results_page(
                status="value", cursor=cursor, limit=page_size
            )
            for row in rows:
                if self._is_low_score(row, threshold):
                    enriched = await self._attach_query(
                        dict(row, reason="low_score")
                    )
                    collected.append(enriched)
                    if len(collected) >= limit:
                        break
            scanned += len(rows)
            cursor = next_token
            if next_token is None:
                break
        return collected

    async def _attach_query(self, item: dict[str, Any]) -> dict[str, Any]:
        """附样本真实 query（证据读取失败不阻塞队列展示，query 置空）。"""
        job_id = item.get("job_id")
        if self._evidence_reader is None or not job_id:
            return item
        try:
            job = await self._store.get_job(job_id)
            sample = await self._store.load_sample(job["sample_id"])
            ref = sample.get("snapshot_ref") or ""
            if not ref:
                return item
            snapshot = await self._evidence_reader.load_snapshot(ref)
            # M3（质量复审）：EvidenceSnapshot.content 构造期冻结为
            # MappingProxyType，直接按 Mapping 读取（json.loads 只接受
            # str/bytes，对 Mapping 必然 TypeError 且被吞致 query 恒空）
            content = snapshot.content
            query = content.get("query") if isinstance(content, Mapping) else None
            item["query"] = str(query) if query is not None else ""
        except Exception:
            logger.info("review queue evidence read failed; query omitted")
            item["query"] = ""
        return item

    @staticmethod
    def _is_low_score(row: Mapping[str, Any], threshold: float) -> bool:
        passed = row.get("passed")
        if passed is False:
            return True
        value = row.get("typed_value")
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value < threshold
        )

    # ------------------------------------------------------------------
    # 审核后受控导出
    # ------------------------------------------------------------------

    def _dataset_directory(self, dataset_id: str, version: str) -> Path:
        for value in (dataset_id, version):
            if (not isinstance(value, str) or not value.strip()
                    or value in (".", "..") or any(c in value for c in ("/", "\\", "\0"))):
                raise EvaluationValidationError("数据集 ID 和版本必须是不含路径分隔符的非空名称")
        root = self._datasets_root.resolve()
        directory = root / dataset_id / version
        if ((root / dataset_id).is_symlink() or directory.is_symlink()
                or not directory.resolve().is_relative_to(root)):
            raise EvaluationValidationError("数据集目录不可越界或使用符号链接")
        return directory

    def _read_dataset(self, dataset_id: str, version: str) -> dict[str, Any]:
        directory = self._dataset_directory(dataset_id, version)
        path = directory / "manifest.json"
        if path.is_symlink():
            raise EvaluationValidationError("数据集 manifest 不可使用符号链接")
        if not path.is_file():
            raise EvaluationNotFoundError("dataset not found")
        try:
            load_dataset_manifest(path)
            content = json.loads(path.read_text(encoding="utf-8"))
        except (DatasetManifestError, OSError, ValueError):
            raise EvaluationValidationError("数据集校验失败，请检查 manifest、内容哈希及配对文件") from None
        if content['dataset_id'] != dataset_id or content['dataset_version'] != version:
            raise EvaluationValidationError("数据集目录与 manifest 身份不一致")
        return content

    async def get_dataset(self, dataset_id: str, version: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._read_dataset, dataset_id, version)

    async def list_datasets(self) -> list[dict[str, Any]]:
        def read():
            root = self._datasets_root
            if not root.exists():
                return []
            items = []
            for dataset in sorted(root.iterdir()):
                if dataset.is_symlink() or not dataset.is_dir():
                    continue
                for version in sorted(dataset.iterdir()):
                    if version.is_symlink() or not version.is_dir():
                        continue
                    if not (version / "manifest.json").exists():
                        continue
                    item = {"dataset_id": dataset.name, "dataset_version": version.name}
                    try:
                        content = self._read_dataset(dataset.name, version.name)
                        item.update(target_kind=content['target_kind'],
                                    dataset_hash=content['dataset_hash'],
                                    case_count=len(content['cases']), status='valid')
                    except (EvaluationValidationError, EvaluationNotFoundError):
                        item.update(status='invalid', case_count=None)
                    items.append(item)
            return items
        return await asyncio.to_thread(read)

    async def import_dataset(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._dataset_directory(payload.get('dataset_id'), payload.get('dataset_version'))
        cases = payload.get('cases')
        if not isinstance(cases, list) or not cases:
            raise EvaluationValidationError("cases 必须是非空数组")
        # Browser imports carry inline data, never server-side file references.
        for case in cases:
            if (not isinstance(case, dict) or 'fixture_content' not in case
                    or any(key in case for key in ('fixture', 'rubric_ref'))):
                raise EvaluationValidationError("导入用例必须提供 fixture_content，不接受服务端文件引用")
        result = dict(await self.export_reviewed_dataset(payload))
        result.pop('manifest_path', None)
        return result

    async def export_reviewed_dataset(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        # Imports and review exports share the same immutable version namespace.
        async with self._dataset_write_lock:
            return await self._export_reviewed_dataset(payload)

    async def _export_reviewed_dataset(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """审核后导出新 manifest（受控路径）。

        - 金标只来自审核人提供的 truth；本服务不读取任何 Judge 结果，
          不自动生成 expected_output/qrels；
        - dataset_id/version/target_kind/approved_by/cases 必填；
          cases 非空、递归无身份键、truth 与 fixture/rubric 配对齐备；
        - fixture/rubric 双模式：case 可带内联 ``fixture_content`` /
          ``rubric_content``（管理页导出：服务端计算 sha、落盘文件，
          之后走同一配对校验 + T12 fail-fast 回读，语义不变），或引用
          数据集目录内已落盘文件（fixture/rubric_ref + sha）；
        - manifest 写入 ``datasets_root/{dataset_id}/{dataset_version}/``
          ；同 (dataset_id, dataset_version) 已存在 manifest 时 409；
        - 落盘后立即用 T12 权威加载器回读校验（fail-fast），任一失败
          清理本次写入的文件并终止，不产出半截数据集；随后记录
          export audit（审批来源 + source_result_ids）；
        - 同步完成语义（Mi10）：导出在本次调用内完成落盘+校验+审计，
          无后台任务需要 202 Accepted + 轮询，HTTP 侧返回 200 与导出
          结果字段（spec 中 202 属计划期笔误，已记录于 T21 判断记录）。
        """
        dataset_id = self._require_str(payload, "dataset_id")
        dataset_version = self._require_str(payload, "dataset_version")
        target_kind = self._require_str(payload, "target_kind")
        approved_by = self._require_str(payload, "approved_by")
        # 路径卫生：dataset_id/version 用于目录拼接，不得含路径分隔符
        # 或相对段（防目录逃逸出 datasets_root）
        for name, value in (
            ("dataset_id", dataset_id),
            ("dataset_version", dataset_version),
        ):
            if value in (".", "..") or "/" in value or "\\" in value:
                raise EvaluationValidationError(
                    f"{name} 不得包含路径分隔符或相对段"
                )
        source_result_ids = payload.get("source_result_ids", [])
        if not isinstance(source_result_ids, list) or any(
            not isinstance(item, str) or not item
            for item in source_result_ids
        ):
            raise EvaluationValidationError(
                "source_result_ids 必须是非空字符串数组（可为空数组）"
            )
        cases = payload.get("cases")
        if not isinstance(cases, list) or not cases:
            raise EvaluationValidationError("cases 必须是非空数组")

        dataset_dir = self._datasets_root / dataset_id / dataset_version
        manifest_path = dataset_dir / "manifest.json"
        if manifest_path.exists():
            raise EvaluationConflictError(
                f"导出数据集版本已存在: {dataset_id}/{dataset_version}"
            )

        # 先做形态校验（无副作用），再统一落盘：任一 case 非法时不出
        # 现半截文件
        prepared = [
            self._prepare_export_case(case, index=index, base_dir=dataset_dir)
            for index, case in enumerate(cases)
        ]

        # M3：落盘与 T12 全量回读是同步文件 I/O（每 case 可达 MiB 级，
        # 20 case 数百毫秒），整体入工作线程执行，对齐 store 层
        # asyncio.to_thread 模式，不阻塞事件循环
        content, written = await asyncio.to_thread(
            self._write_and_validate_dataset,
            dataset_dir,
            manifest_path,
            prepared,
            dataset_id,
            dataset_version,
            target_kind,
        )
        # M1：审计写入纳入回滚保护——失败时清理本次写入的 manifest 与
        # 配对文件，避免"有数据集无审计"且同版本重试 409 永不可恢复
        audit_id = uuid.uuid4().hex
        try:
            await self._store.save_export_audit({
                "audit_id": audit_id,
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "dataset_hash": content["dataset_hash"],
                "manifest_path": str(manifest_path),
                "approved_by": approved_by,
                "source_result_ids_json": json.dumps(
                    source_result_ids, ensure_ascii=False
                ),
                "case_count": len(prepared),
            })
        except Exception:
            await asyncio.to_thread(self._cleanup_written, written)
            raise
        logger.info(
            "evaluation export: dataset=%s version=%s cases=%d approved_by=%s",
            dataset_id, dataset_version, len(prepared), approved_by,
        )
        return {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_hash": content["dataset_hash"],
            "manifest_path": str(manifest_path),
            "case_count": len(prepared),
            "audit_id": audit_id,
        }

    def _write_and_validate_dataset(
        self,
        dataset_dir: Path,
        manifest_path: Path,
        prepared: list[dict[str, Any]],
        dataset_id: str,
        dataset_version: str,
        target_kind: str,
    ) -> tuple[dict[str, Any], list[Path]]:
        """同步落盘 + T12 权威加载器回读校验（工作线程内执行）。

        fail-fast：fixture/rubric 存在性、sha、dataset_hash 重算任一失败
        即清理本次写入并终止，不产出半截数据集。"""
        written: list[Path] = []
        dataset_dir.mkdir(parents=True, exist_ok=True)
        try:
            for case in prepared:
                for rel_path, content_bytes in case.pop("_write_files"):
                    target = dataset_dir / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content_bytes)
                    written.append(target)
            manifest_cases = prepared
            content: dict[str, Any] = {
                "schema_version": "1",
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "target_kind": target_kind,
                "cases": manifest_cases,
            }
            content["dataset_hash"] = compute_dataset_hash(content)
            manifest_path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written.append(manifest_path)
            load_dataset_manifest(manifest_path)
            return content, written
        except DatasetManifestError as exc:
            self._cleanup_written(written)
            raise EvaluationValidationError(
                "导出数据集校验失败：manifest 或配对文件未通过 T12 加载"
                "校验，已回滚本次写入"
            ) from exc
        except Exception:
            self._cleanup_written(written)
            raise

    @staticmethod
    def _cleanup_written(written: list[Path]) -> None:
        """回滚本次写入的文件（只删本次创建的路径，不动既有数据集）。

        同步移除因此变空的目录（fixtures/rubrics 子目录与数据集目录），
        避免留下空壳目录干扰同版本重试的观感；rmdir 只作用于两层父目录
        且仅在目录已空时生效，不会触碰 datasets_root 及其上的既有内容。"""
        for path in written:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("export rollback: 无法删除 %s", path)
            for parent in (path.parent, path.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    pass

    async def list_export_audits(
        self, *, dataset_id: str | None = None, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        """导出审计列表（审批来源溯源）。"""
        return await self._store.list_export_audits(
            dataset_id=dataset_id, limit=limit
        )

    # ------------------------------------------------------------------
    # 内部校验
    # ------------------------------------------------------------------

    @staticmethod
    def _require_str(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise EvaluationValidationError(
                f"{key} 必须是非空字符串"
            )
        return value

    # 内联导出的 fixture/rubric 落盘路径（相对数据集目录）；文件名取
    # case_id（已做路径卫生），避免在 manifest 中出现任何原始身份
    _INLINE_FIXTURE_DIR = "fixtures"
    _INLINE_RUBRIC_DIR = "rubrics"
    # 无审核人 rubric 时的默认评阅说明（评阅指引不是金标，金标只来自
    # 审核人提供的 expected_output/qrels）
    _DEFAULT_RUBRIC = {
        "criteria": "按审核人提供的金标（expected_output）判定回答正确性",
    }

    @classmethod
    def _prepare_export_case(
        cls, case: Any, *, index: int, base_dir: Path
    ) -> dict[str, Any]:
        """case 形态校验与内联内容物化准备（无副作用）。

        返回 manifest case dict（身份键已拒、金标/配对齐备）；内联
        ``fixture_content`` / ``rubric_content`` 转为 ``_write_files``
        待写清单（(相对路径, 字节)），由调用方统一落盘。"""
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            raise EvaluationValidationError(f"{where} 必须是 JSON 对象")
        cls._reject_identity_keys(case, where)
        input_value = case.get("input")
        if not isinstance(input_value, dict) or not input_value:
            raise EvaluationValidationError(
                f"{where} input 必须是非空 JSON 对象"
            )
        case = dict(case)
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in (".", "..")
            or "/" in case_id
            or "\\" in case_id
        ):
            # case_id 用作内联文件名，必须可安全拼路径
            raise EvaluationValidationError(
                f"{where} case_id 必须是不含路径分隔符的非空字符串"
            )
        for field in _EXPORT_CASE_REQUIRED_FIELDS:
            value = case.get(field)
            if not isinstance(value, str) or not value:
                raise EvaluationValidationError(
                    f"{where} 缺少必填字段: {field}"
                )
        # 金标只来自审核人提供的 truth：expected_output 或 qrels 至少其一
        if "expected_output" not in case and "qrels" not in case:
            raise EvaluationValidationError(
                f"{where} 缺少金标: 需要 expected_output 或 qrels"
                "（禁止自动生成，由审核人提供）"
            )
        write_files: list[tuple[str, bytes]] = []
        if "fixture_content" in case:
            fixture_bytes = json.dumps(
                case.pop("fixture_content"),
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
            case["fixture"] = (
                f"{cls._INLINE_FIXTURE_DIR}/{case_id}.json"
            )
            case["fixture_sha256"] = hashlib.sha256(fixture_bytes).hexdigest()
            write_files.append((case["fixture"], fixture_bytes))
        else:
            if not isinstance(case.get("fixture"), str) or not case["fixture"]:
                raise EvaluationValidationError(
                    f"{where} 缺少 fixture：提供 fixture_content 或已落盘"
                    " 文件引用"
                )
            # 预落盘模式：存在性 + sha 快速校验（T12 回读仍兜底）
            cls._require_paired_file(
                case, path_field="fixture", sha_field="fixture_sha256",
                base_dir=base_dir, where=where,
            )
        if "rubric_content" in case:
            rubric_raw = case.pop("rubric_content")
            rubric_bytes = (
                rubric_raw.encode("utf-8")
                if isinstance(rubric_raw, str)
                else json.dumps(
                    rubric_raw, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            )
            case["rubric_ref"] = (
                f"{cls._INLINE_RUBRIC_DIR}/{case_id}.json"
            )
            case["rubric_sha256"] = hashlib.sha256(rubric_bytes).hexdigest()
            write_files.append((case["rubric_ref"], rubric_bytes))
        elif not isinstance(case.get("rubric_ref"), str) or not case["rubric_ref"]:
            # 可选 rubric：未提供时服务端写默认评阅说明（非金标）
            rubric_bytes = json.dumps(
                cls._DEFAULT_RUBRIC, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            case["rubric_ref"] = (
                f"{cls._INLINE_RUBRIC_DIR}/{case_id}.json"
            )
            case["rubric_sha256"] = hashlib.sha256(rubric_bytes).hexdigest()
            write_files.append((case["rubric_ref"], rubric_bytes))
        else:
            cls._require_paired_file(
                case, path_field="rubric_ref", sha_field="rubric_sha256",
                base_dir=base_dir, where=where,
            )
        case["_write_files"] = write_files
        return case

    @staticmethod
    def _reject_identity_keys(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                # M2：键归一化（小写、去下划线/连字符）后再比对，拦截
                # Session_ID/sessionId/traceId 等大小写与分隔符变体
                if (
                    isinstance(key, str)
                    and _normalize_identity_key(key) in _IDENTITY_KEYS_NORMALIZED
                ):
                    raise EvaluationIdentityLeakError(
                        f"{where} 含身份键 {key!r}（导出必须解除原始身份，"
                        "审批来源只记录在 export audit）"
                    )
                EvaluationReviewService._reject_identity_keys(value, where)
        elif isinstance(node, list):
            for item in node:
                EvaluationReviewService._reject_identity_keys(item, where)

    @staticmethod
    def _require_paired_file(
        case: Mapping[str, Any],
        *,
        path_field: str,
        sha_field: str,
        base_dir: Path,
        where: str,
    ) -> None:
        """预落盘文件引用校验（m2：错误文案不含服务器路径与 sha 值）。"""
        raw = case[path_field]
        raw_path = Path(raw)
        # 路径卫生：fixture/rubric 引用必须是相对路径且不得含相对段
        # （防目录逃逸出数据集目录）
        # 边界声明（N4）：不做 symlink 检查——datasets_root 为本地管理
        # 员可控目录（相对 workspace_root，C 类配置），若管理员自建
        # symlink 指向目录外属于运维层信任边界，不在导出校验范围
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise EvaluationValidationError(
                f"{where} 的 {path_field} 引用必须是数据集目录内相对路径"
            )
        path = base_dir / raw_path
        if not path.is_file():
            raise EvaluationValidationError(
                f"{where} 的 {path_field} 文件不存在或不可读"
            )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != case[sha_field]:
            raise EvaluationValidationError(
                f"{where} 的 {path_field} 内容与声明 sha 不一致"
            )
