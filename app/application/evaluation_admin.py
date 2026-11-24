# app/application/evaluation_admin.py
"""评估器管理用例（plan-261112 Task 5，spec 4.2 / 4.3）。

EvaluationAdminService 是评估器管理页（Dashboard 评估器列表/详情/版本创
建/在线启停）的应用层用例入口：

- capabilities：服务端能力目录——只宣称当前执行器真实支持的
  (metric_name, method, target_kind) 组合，不能仅因 Domain VO 能构造就
  宣称指标可执行；llm_judge 条目按 judge_ready() 门控执行就绪状态。
- create_definition：服务端构造不可变 EvaluatorDefinition（字段白名单、
  未知参数拒绝、material_hash 服务端按既有公式生成，客户端不传），经
  EvaluationService.register_definition 同事务写入默认 disabled 的使用
  状态（spec 4.3：新增定义不自动用于线上）。
- get_definition_detail：完整契约 + usage + 只读校准摘要（复用
  calibration_status_from_store 与账本最新报告查询；未校准返回 None，
  advisory 不显示"已校准"）。
- set_definition_usage：单版本在线启停（human 禁启用、CAS 经 store）。

依赖纪律（DDD 依赖方向，knowledge/03-conventions）：只依赖 Domain 与
EvaluationLedgerStore / EvaluationService（Application 层内），不 import
app.infrastructure。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping

from app.application.evaluation_calibration import (
    calibration_definition_hash,
    calibration_status_from_store,
)
from app.application.evaluation_service import (
    RESULTS_PAGE_LIMIT_CAP,
    EvaluationLedgerStore,
    EvaluationService,
    definition_from_mapping,
)
from app.domain.evaluation.config import (
    EvaluationConfigValidationError,
    validate_model_name_string,
)
from app.domain.evaluation.models import (
    EVALUATOR_DIRECTIONS,
    EVALUATOR_METHODS,
    EvaluatorDefinition,
    OUTPUT_TYPES,
    TARGET_KINDS,
    EvaluationValidationError,
    UnsupportedCapabilityError,
)

# 服务端能力目录：从 EvaluationService 正式指标预设派生（单一权威源，禁止
# 手工拷贝两份）。preset 的 definition_id 即正式 metric 目录名；deterministic
# 目标适配以 app/domain/evaluation/metrics_deterministic.py 实际函数为准
# （kb.* 为检索目标、agent.* 为 agent_run 目标）
_DETERMINISTIC_CAPABILITIES: tuple[dict[str, str], ...] = tuple(
    {
        "metric_name": item["definition_id"],
        "target_kind": item["target_kind"],
        "output_type": item["output_type"],
        "description": item["description"],
    }
    for item in EvaluationService._PRESET_DETERMINISTIC
)

# 服务端能力目录：llm_judge 正式指标（派生自 EvaluationService._PRESET_JUDGE；
# judge 适配器按 metric_name 决定 rubric/schema/证据槽）
_LLM_JUDGE_CAPABILITIES: tuple[dict[str, str], ...] = tuple(
    {
        "metric_name": item["definition_id"],
        "target_kind": item["target_kind"],
        "description": item["description"],
    }
    for item in EvaluationService._PRESET_JUDGE
)

# human 定义：metric_name 由创建者提供（目录条目按 output_type 声明），
# 不参与线上自动作业（online_forbidden）
_HUMAN_OUTPUT_TYPES: tuple[str, ...] = OUTPUT_TYPES
_HUMAN_TARGET_KINDS: tuple[str, ...] = ("agent_run", "retrieval")

# llm_judge judge_params 白名单（judge_provider 适配器实际消费的键：
# temperature 采样温度 [0,2]、rubric 自定义评审标准、self_judge 自评声明）
_JUDGE_PARAM_KEYS: tuple[str, ...] = ("temperature", "rubric", "self_judge")

# create_definition 载荷白名单（其余键一律拒绝，含 material_hash/code/
# prompt——材料 hash 由服务端按既有公式生成，客户端不能伪造）
_PAYLOAD_KEYS: tuple[str, ...] = (
    "definition_id",
    "version",
    "metric_name",
    "target_kind",
    "method",
    "required_fields",
    "evidence_policy",
    "output_type",
    "range_or_categories",
    "direction",
    "judge_model",
    "judge_params",
    "timeout_seconds",
    "budget",
    "ci_admissible",
)

# list_definitions 分页上限（与 results 分页封顶共用同一常量，单一权威源）
_LIST_LIMIT_CAP = RESULTS_PAGE_LIMIT_CAP

# 能力目录展示排序权重：①方法（LLM Judge > 确定性公式 > 人工评分）
# ②指标语义维度（正确 > 完整 > 相关 > 忠实），未收录 metric 归"其它"
_CAPABILITY_METHOD_ORDER: dict[str, int] = {
    "llm_judge": 0,
    "deterministic": 1,
    "human": 2,
}
_CAPABILITY_METRIC_ORDER: dict[str, int] = {
    # 正确：任务是否做成、回答是否正确
    "agent.task_success": 0,
    "agent.answer_correctness": 0,
    # 完整：回答要点齐全、契约/断言全部满足、相关项是否找全
    "agent.answer_completeness": 1,
    "agent.tool_contract": 1,
    "agent.scenario_assertions": 1,
    "kb.recall_at_5": 1,
    # 相关：内容与请求的相关程度、相关项排序质量
    "agent.answer_relevance": 2,
    "kb.ndcg_at_5": 2,
    "kb.mrr_at_5": 2,
    # 忠实：回答是否忠于证据
    "agent.answer_faithfulness": 3,
    # kb.context_relevance 固定置于 LLM Judge 组末尾（产品排序要求，
    # 权重排在"其它"之后）
    "kb.context_relevance": 5,
}


def _version_natural_key(version: str) -> tuple[tuple[int, int, str], ...]:
    """版本号自然序排序键：数字段按数值比较（v10 > v2），文本段按字典
    序（experiment-v* 排在 v* 之前）；与前端数据集版本比较同规则。"""
    key: list[tuple[int, int, str]] = []
    for token in re.findall(r"\d+|\D+", version):
        if token.isdigit():
            key.append((1, int(token), ""))
        else:
            key.append((0, 0, token))
    return tuple(key)


class EvaluationAdminService:
    """评估器管理用例（spec 4.2 能力目录/版本创建/详情，4.3 在线启停）。"""

    def __init__(
        self,
        store: EvaluationLedgerStore,
        *,
        judge_ready: Callable[[], Awaitable[bool]],
        service: str = "n-agent",
        evaluation_service: EvaluationService | None = None,
    ) -> None:
        self._store = store
        # T4：judge_ready 为 async callable——组合根供给判定含 Provider
        # secret 异步读取（registry.get_secret 无同步变体）
        self._judge_ready = judge_ready
        # usage 审计身份（updated_by）；注册路径委托正式注册用例
        self._service = service
        self._evaluation_service = evaluation_service or EvaluationService(store)

    # ------------------------------------------------------------------
    # 能力目录
    # ------------------------------------------------------------------

    async def capabilities(self) -> dict[str, Any]:
        """当前执行器真实支持的评估能力目录（spec 4.2）。

        - deterministic：六指标固定内置公式组合，始终可执行；
        - llm_judge：judge_required=True，Judge 未配置（judge_ready()
          为 False）时 executable=False；
        - human：online_forbidden=True（人工评估不进在线统计，spec 6.2），
          不可执行（结果由人工提交）。
        未注册的组合不出现在目录中（禁止宣称可执行）。"""
        judge_ready = bool(await self._judge_ready())
        entries: list[dict[str, Any]] = []
        for item in _DETERMINISTIC_CAPABILITIES:
            entries.append(
                {
                    "metric_name": item["metric_name"],
                    "method": "deterministic",
                    "target_kind": item["target_kind"],
                    "output_type": item["output_type"],
                    "description": item["description"],
                    "executable": True,
                    "judge_required": False,
                    "online_forbidden": False,
                }
            )
        for item in _LLM_JUDGE_CAPABILITIES:
            entries.append(
                {
                    "metric_name": item["metric_name"],
                    "method": "llm_judge",
                    "target_kind": item["target_kind"],
                    "output_type": "number",
                    "description": item["description"],
                    "executable": judge_ready,
                    "judge_required": True,
                    "online_forbidden": False,
                }
            )
        for target_kind in _HUMAN_TARGET_KINDS:
            for output_type in _HUMAN_OUTPUT_TYPES:
                entries.append(
                    {
                        # human metric_name 由创建者提供，目录按形状声明
                        "metric_name": "*",
                        "method": "human",
                        "target_kind": target_kind,
                        "output_type": output_type,
                        "description": "自行定义指标，由人工打分",
                        "executable": False,
                        "judge_required": False,
                        "online_forbidden": True,
                    }
                )
        # 目录列表展示排序（①方法 ②指标语义维度，未收录指标归入"其它"）：
        # 方法 LLM Judge > 确定性公式 > 人工评分；
        # 指标维度 正确 > 完整 > 相关 > 忠实 > 其它。维度按指标实际判定
        # 语义划分：recall@5 是"找全了没"归完整，ndcg/mrr 衡量相关项的
        # 排序质量归相关。同维度内按指标名 asc、目标类型 asc 保持确定性
        entries.sort(key=lambda e: (
            _CAPABILITY_METHOD_ORDER.get(str(e.get("method") or ""), 99),
            _CAPABILITY_METRIC_ORDER.get(str(e.get("metric_name") or ""), 4),
            str(e.get("metric_name") or ""),
            str(e.get("target_kind") or ""),
        ))
        return {"entries": entries, "judge_configured": judge_ready}

    # ------------------------------------------------------------------
    # 版本创建
    # ------------------------------------------------------------------

    async def create_definition(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """创建不可变评估器版本 + 默认 disabled 使用状态（spec 4.2/4.3）。

        - 字段白名单校验、未知参数拒绝（含 material_hash/code/prompt——
          材料hash 服务端按既有公式生成，客户端不能伪造）；
        - deterministic 仅固定内置公式组合（不接收 judge 材料、
          required_fields）；llm_judge 校验 judge_model/judge_params 与
          rubric（未知参数拒绝）；human 允许三种 output_type；
        - ci_admissible 原样登记，不改变门禁资格（spec 4.2）；
        - 经 register_definition 同事务写 usage enabled=False；
        - 同键同版本同内容幂等返回既有（usage 不被复活）；同键同版本
          异内容映射 EvaluationConflictError。"""
        if not isinstance(payload, Mapping):
            raise EvaluationValidationError(
                f"payload 必须是 Mapping: {payload!r}"
            )
        unknown = sorted(set(payload.keys()) - set(_PAYLOAD_KEYS))
        if unknown:
            raise EvaluationValidationError(
                f"未知参数: {unknown}（允许字段: {list(_PAYLOAD_KEYS)}）"
            )
        for name in (
            "definition_id",
            "version",
            "metric_name",
            "target_kind",
            "method",
            "output_type",
        ):
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise EvaluationValidationError(f"{name} 必须是非空字符串")
        method = payload["method"]
        if method not in EVALUATOR_METHODS:
            raise UnsupportedCapabilityError(
                f"非法 method: {method!r}，仅允许 {EVALUATOR_METHODS}"
            )
        if payload["target_kind"] not in TARGET_KINDS:
            raise UnsupportedCapabilityError(
                f"非法 target_kind: {payload['target_kind']!r}，"
                f"仅允许 {TARGET_KINDS}"
            )
        if payload["output_type"] not in OUTPUT_TYPES:
            raise EvaluationValidationError(
                f"非法 output_type: {payload['output_type']!r}，"
                f"仅允许 {OUTPUT_TYPES}"
            )

        builders = {
            "deterministic": self._build_deterministic,
            "llm_judge": self._build_llm_judge,
            "human": self._build_human,
        }
        definition = builders[method](payload)
        row = await self._evaluation_service.register_definition(
            definition, updated_by=self._service
        )
        usage = await self._store.get_definition_usage(
            definition.definition_id, definition.version
        )
        definition_mapping = row.get("definition")
        if not isinstance(definition_mapping, Mapping):
            definition_mapping = {
                key: value
                for key, value in row.items()
                if key != "definition_json"
            }
        return {"definition": dict(definition_mapping), "usage": usage}

    def _build_deterministic(self, payload: Mapping[str, Any]):
        """deterministic：仅固定内置公式组合（能力目录白名单），无外部
        材料（material_hash 恒为空）；evidence_policy/required_fields/
        judge 材料非空传入显式拒绝。"""
        combo = (
            payload["metric_name"],
            payload["target_kind"],
            payload["output_type"],
        )
        allowed = {
            (
                item["metric_name"],
                item["target_kind"],
                item["output_type"],
            )
            for item in _DETERMINISTIC_CAPABILITIES
        }
        if combo not in allowed:
            raise UnsupportedCapabilityError(
                "能力目录不含的组合，deterministic 仅支持内置公式指标: "
                f"{combo}"
            )
        for name in (
            "judge_model",
            "judge_params",
            "required_fields",
            "evidence_policy",
        ):
            value = payload.get(name)
            if value:
                raise EvaluationValidationError(
                    f"deterministic 不接收 {name}（仅固定内置公式，"
                    "不接收 judge 材料/额外字段/证据策略）"
                )
        return EvaluatorDefinition(
            definition_id=payload["definition_id"],
            version=payload["version"],
            metric_name=payload["metric_name"],
            target_kind=payload["target_kind"],
            method="deterministic",
            required_fields=(),
            evidence_policy="snapshot",
            output_type=payload["output_type"],
            range_or_categories=self._range_or_categories(payload),
            direction=self._direction(payload),
            material_hash={},
            judge_model=None,
            judge_params={},
            timeout_seconds=self._timeout_seconds(payload, default=30),
            budget=self._budget(payload),
            ci_admissible=self._ci_admissible(payload),
        )

    def _build_llm_judge(self, payload: Mapping[str, Any]):
        """llm_judge：现有 judge 适配器支持的固定输出 schema + 白名单参数；
        rubric/模型名可改，未知参数拒绝；evidence_policy/range_or_categories
        非空传入显式拒绝（固定 snapshot / number）；material_hash 服务端按
        既有公式（SHA-256 over rubric 文本）生成。"""
        combo = (payload["metric_name"], payload["target_kind"])
        allowed = {
            (item["metric_name"], item["target_kind"])
            for item in _LLM_JUDGE_CAPABILITIES
        }
        if combo not in allowed:
            raise UnsupportedCapabilityError(
                "能力目录不含的组合，llm_judge 仅支持现有 Judge 适配器"
                f"已注册指标: {combo}"
            )
        if payload["output_type"] != "number":
            raise UnsupportedCapabilityError(
                "llm_judge 仅支持现有适配器固定输出 schema（number）: "
                f"{payload['output_type']!r}"
            )
        # 证据策略固定 snapshot、输出取值范围固定 None：非空传入显式拒绝，
        # 不静默忽略（与 deterministic/human 的显式拒绝纪律一致）
        for name in ("evidence_policy", "range_or_categories"):
            if payload.get(name):
                raise EvaluationValidationError(
                    f"llm_judge 不接收 {name}（证据策略固定 snapshot，"
                    "输出 schema 固定 number，不接受外部取值范围）"
                )
        judge_model = payload.get("judge_model")
        if judge_model is None:
            # T4：None（载荷缺省）= 跟随组合根默认供给——定义材料不锁定
            # 模型，后续 Provider 热切换不影响该定义（spec 未冻结语义）
            pass
        elif not isinstance(judge_model, str) or not judge_model.strip():
            # 显式空串/空白/非字符串仍拒绝（空串 = 未声明，不是"跟随
            # 默认"——只有 None 表示跟随默认供给）
            raise EvaluationValidationError(
                "llm_judge judge_model 必须是非空字符串或 None（跟随默认）"
            )
        else:
            # 非空值经共用模型名校验（T5 单一来源，与 C 类配置覆盖同规则：
            # 长度/控制字符/首尾空白，不静默 trim）；显式字符串冻结该模型
            try:
                validate_model_name_string(judge_model, allow_empty=False)
            except EvaluationConfigValidationError as exc:
                raise EvaluationValidationError(str(exc)) from exc
        judge_params = self._judge_params(payload.get("judge_params"))
        rubric = judge_params.get("rubric")
        material_hash: dict[str, str] = {}
        if isinstance(rubric, str) and rubric.strip():
            material_hash["rubric"] = hashlib.sha256(
                rubric.encode("utf-8")
            ).hexdigest()
        return EvaluatorDefinition(
            definition_id=payload["definition_id"],
            version=payload["version"],
            metric_name=payload["metric_name"],
            target_kind=payload["target_kind"],
            method="llm_judge",
            required_fields=self._required_fields(payload),
            evidence_policy="snapshot",
            output_type="number",
            range_or_categories=None,
            direction=self._direction(payload),
            material_hash=material_hash,
            judge_model=judge_model,
            judge_params=judge_params,
            timeout_seconds=self._timeout_seconds(payload, default=60),
            budget=self._budget(payload),
            ci_admissible=self._ci_admissible(payload),
        )

    def _build_human(self, payload: Mapping[str, Any]):
        """human：三种 output_type；值校验复用 validate_evaluation_value
        （Domain VO 构造期 + 提交期）；不接收 judge 材料。"""
        if (
            payload["target_kind"] not in _HUMAN_TARGET_KINDS
            or payload["output_type"] not in _HUMAN_OUTPUT_TYPES
        ):
            raise UnsupportedCapabilityError(
                "能力目录不含的组合，human 仅支持 "
                f"target_kind={_HUMAN_TARGET_KINDS} x "
                f"output_type={_HUMAN_OUTPUT_TYPES}"
            )
        for name in ("judge_model", "judge_params"):
            if payload.get(name):
                raise EvaluationValidationError(
                    f"human 不接收 {name}（人工评估无 Judge）"
                )
        # evidence_policy：缺省为 actor_submission；显式传入时必须是非空
        # 字符串（拒绝非字符串强转与空串静默映射默认）
        evidence_policy = payload.get("evidence_policy")
        if evidence_policy is None:
            evidence_policy = "actor_submission"
        elif (
            not isinstance(evidence_policy, str) or not evidence_policy.strip()
        ):
            raise EvaluationValidationError(
                f"evidence_policy 必须是非空字符串: {evidence_policy!r}"
            )
        return EvaluatorDefinition(
            definition_id=payload["definition_id"],
            version=payload["version"],
            metric_name=payload["metric_name"],
            target_kind=payload["target_kind"],
            method="human",
            required_fields=self._required_fields(payload),
            evidence_policy=evidence_policy,
            output_type=payload["output_type"],
            range_or_categories=self._range_or_categories(payload),
            direction=self._direction(payload),
            material_hash={},
            judge_model=None,
            judge_params={},
            timeout_seconds=self._timeout_seconds(payload, default=60),
            budget=self._budget(payload),
            ci_admissible=self._ci_admissible(payload),
        )

    # -- 载荷字段归一（域 VO 构造期仍会复核） --------------------------------

    @staticmethod
    def _range_or_categories(payload: Mapping[str, Any]) -> Any:
        value = payload.get("range_or_categories")
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise EvaluationValidationError(
            f"range_or_categories 必须是列表或二元组: {value!r}"
        )

    @staticmethod
    def _direction(payload: Mapping[str, Any]) -> str:
        direction = payload.get("direction") or "higher_is_better"
        if direction not in EVALUATOR_DIRECTIONS:
            raise EvaluationValidationError(
                f"非法 direction: {direction!r}，仅允许 {EVALUATOR_DIRECTIONS}"
            )
        return direction

    @staticmethod
    def _timeout_seconds(payload: Mapping[str, Any], *, default: int) -> int:
        value = payload.get("timeout_seconds")
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvaluationValidationError(
                f"timeout_seconds 必须是正整数: {value!r}"
            )
        return value

    @staticmethod
    def _budget(payload: Mapping[str, Any]):
        value = payload.get("budget")
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise EvaluationValidationError(
                f"budget 必须是合法数值: {value!r}"
            ) from exc

    @staticmethod
    def _ci_admissible(payload: Mapping[str, Any]) -> bool:
        value = payload.get("ci_admissible")
        if value is None:
            return False
        if not isinstance(value, bool):
            raise EvaluationValidationError(
                f"ci_admissible 必须是 bool: {value!r}"
            )
        return value

    @staticmethod
    def _required_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
        value = payload.get("required_fields")
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise EvaluationValidationError(
                f"required_fields 必须是非空字符串列表: {value!r}"
            )
        return tuple(value)

    @staticmethod
    def _judge_params(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise EvaluationValidationError(
                f"judge_params 必须是 Mapping: {value!r}"
            )
        unknown = sorted(set(value.keys()) - set(_JUDGE_PARAM_KEYS))
        if unknown:
            raise EvaluationValidationError(
                f"judge_params 未知参数: {unknown}"
                f"（支持: {list(_JUDGE_PARAM_KEYS)}）"
            )
        params: dict[str, Any] = dict(value)
        temperature = params.get("temperature")
        if temperature is not None:
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or not 0 <= float(temperature) <= 2
            ):
                raise EvaluationValidationError(
                    f"judge_params.temperature 必须是 [0, 2] 数值: "
                    f"{temperature!r}"
                )
        rubric = params.get("rubric")
        if rubric is not None and (
            not isinstance(rubric, str) or not rubric.strip()
        ):
            raise EvaluationValidationError(
                f"judge_params.rubric 必须是非空字符串: {rubric!r}"
            )
        self_judge = params.get("self_judge")
        if self_judge is not None and not isinstance(self_judge, bool):
            raise EvaluationValidationError(
                f"judge_params.self_judge 必须是 bool: {self_judge!r}"
            )
        return params

    # ------------------------------------------------------------------
    # 列表 / 详情
    # ------------------------------------------------------------------

    async def list_definitions(
        self,
        *,
        target_kind: str | None = None,
        method: str | None = None,
        definition_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """定义 + usage 联合投影（脱敏：最小投影，不含连接密钥/内部路径）。

        默认按 (definition_id, target_kind) 聚合：每个评估器只返回版本
        自然序最新一行，附 version_count 供前端展示「历史」入口（实验
        镜像 variant 与线上定义同 ID 不同 target_kind，聚合键必须含
        target_kind，避免 variant 被并成线上版本历史）。definition_id
        给定时返回该评估器全部版本（自然序新到旧，不分页），供历史
        modal 使用。按 target_kind / method / definition_id 过滤；cursor
        为不透明偏移分页标记（按聚合后行数偏移）。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise EvaluationValidationError(
                f"limit 必须是正整数: {limit!r}"
            )
        limit = min(limit, _LIST_LIMIT_CAP)
        if definition_id is not None:
            if not isinstance(definition_id, str) or not definition_id.strip():
                raise EvaluationValidationError(
                    f"definition_id 必须是非空字符串: {definition_id!r}"
                )
            rows = await self._store.list_definitions_with_usage(
                target_kind=target_kind
            )
            rows = [
                row for row in rows
                if row.get("definition_id") == definition_id
            ]
            if method is not None:
                rows = [
                    row for row in rows if row.get("method") == method
                ]
            # 历史 modal 展示顺序：版本自然序新到旧（对齐数据集历史）
            rows.sort(
                key=lambda row: _version_natural_key(
                    str(row.get("version") or "")
                ),
                reverse=True,
            )
            items = [self._definition_list_item(row) for row in rows]
            return {"items": items, "next_cursor": None,
                    "total_count": len(items),
                    "total_pages": max(1, (len(items) + limit - 1) // limit)}
        offset = self._decode_list_cursor(cursor)
        rows = await self._store.list_definitions_with_usage(
            target_kind=target_kind
        )
        if method is not None:
            rows = [row for row in rows if row.get("method") == method]
        # 聚合：组内按版本自然序升序取最新；实验镜像 variant 因
        # target_kind 不同自成一组，不参与线上评估器的版本历史
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for row in rows:
            key = (
                str(row.get("definition_id") or ""),
                str(row.get("target_kind") or ""),
            )
            grouped.setdefault(key, []).append(row)
        grouped_rows: list[dict[str, Any]] = []
        for versions in grouped.values():
            versions.sort(
                key=lambda row: _version_natural_key(
                    str(row.get("version") or "")
                )
            )
            grouped_rows.append(
                {**self._definition_list_item(versions[-1]),
                 "version_count": len(versions)}
            )
        window = grouped_rows[offset : offset + limit]
        next_cursor = (
            self._encode_list_cursor(offset + limit)
            if offset + limit < len(grouped_rows)
            else None
        )
        total_count = len(grouped_rows)
        return {"items": window, "next_cursor": next_cursor,
                "total_count": total_count,
                "total_pages": max(1, (total_count + limit - 1) // limit)}

    @staticmethod
    def _definition_list_item(row: Mapping[str, Any]) -> dict[str, Any]:
        """列表最小投影（不含连接密钥/内部路径）。"""
        return {
            "definition_id": row.get("definition_id"),
            "version": row.get("version"),
            "metric_name": row.get("metric_name"),
            "target_kind": row.get("target_kind"),
            "method": row.get("method"),
            "usage": row.get("usage"),
        }

    @staticmethod
    def _encode_list_cursor(offset: int) -> str:
        raw = json.dumps({"o": offset}).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _decode_list_cursor(cursor: str | None) -> int:
        if cursor is None or cursor == "":
            return 0
        try:
            raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            offset = raw["o"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ValueError(offset)
            return offset
        except Exception as exc:
            raise EvaluationValidationError(
                f"非法列表游标: {exc}"
            ) from exc

    async def get_definition_detail(
        self, definition_id: str, version: str
    ) -> dict[str, Any]:
        """完整契约 + usage + 只读校准摘要（spec 4.2）。

        校准摘要核验校准/回归材料 hash（报告按当前 definition hash 取
        最新；hash 变化即缓存键失效）：返回时间、有效性及失效原因；
        未校准（无匹配报告）返回 calibration: None；advisory 附
        "未校准或材料已变更"文案，不显示"已校准"。"""
        row = await self._store.get_definition(definition_id, version)
        definition = definition_from_mapping(row["definition"])
        usage = await self._store.get_definition_usage(definition_id, version)
        calibration = await self._calibration_summary(definition)
        return {
            "definition": row["definition"],
            "usage": usage,
            "calibration": calibration,
        }

    async def _calibration_summary(
        self, definition: EvaluatorDefinition
    ) -> dict[str, Any] | None:
        definition_hash = calibration_definition_hash(definition)
        report = await self._store.get_latest_calibration_report(
            definition.definition_id, definition_hash
        )
        if report is None:
            # 无报告 = 未校准（或定义材料变更后重校准前的旧报告不适用）
            return None
        status = await calibration_status_from_store(self._store, definition)
        effective_state = "passed" if status == "passed" else "advisory"
        summary: dict[str, Any] = {
            "definition_hash": definition_hash,
            "calibration_set_hash": report.get("calibration_set_hash", ""),
            "regression_set_hash": report.get("regression_set_hash", ""),
            "created_at": report.get("created_at", ""),
            "status": report.get("status"),
            "effective_state": effective_state,
            "failures": list(report.get("failures") or []),
        }
        if effective_state == "advisory":
            summary["message"] = "未校准或材料已变更，校准结果仅供参考"
        return summary

    # ------------------------------------------------------------------
    # 在线使用状态（spec 4.3）
    # ------------------------------------------------------------------

    async def set_definition_usage(
        self,
        definition_id: str,
        version: str,
        *,
        online_enabled: bool,
        expected_revision: int,
    ) -> Mapping[str, Any]:
        """单版本在线启停（CAS 经 store；human 禁启用，spec 6.2/4.3）。

        更改只影响之后捕获的样本，已冻结作业与实验不变。返回新 usage。"""
        if not isinstance(online_enabled, bool):
            raise EvaluationValidationError(
                f"online_enabled 必须是 bool: {online_enabled!r}"
            )
        row = await self._store.get_definition(definition_id, version)
        definition = definition_from_mapping(row["definition"])
        if definition.method == "human" and online_enabled:
            raise EvaluationValidationError(
                "human 方法禁止线上启用（人工评估不进在线统计）"
            )
        return await self._store.upsert_definition_usage(
            definition_id,
            version,
            online_enabled=online_enabled,
            expected_revision=expected_revision,
            updated_by=self._service,
        )

    # ------------------------------------------------------------------
    # 一次性 usage 回填（spec 4.3 回填时序）
    # ------------------------------------------------------------------

    async def backfill_definition_usage(
        self, *, actor: str = "startup-migration"
    ) -> int:
        """一次性 usage 回填（组合根 lifespan 在预设注册完成后调用）。

        幂等：检查标记、回填与记录标记同事务提交，标记记录在既有
        migrations 账本（evaluation_schema_migrations 独立数据回填版本），
        重启后跳过（用户停用状态不复活，之后新注册版本始终 disabled）；
        仅自动定义生成/启用 enabled=1 行，human（含 feedback.helpful）
        永不启用；首次启动预设注册产生的 disabled 自动 usage 也在本次
        回填中启用。返回本次回填影响行数（标记已存在时为 0）。"""
        return await self._store.backfill_definition_usage_once(
            updated_by=actor
        )
