"""评估子域 C 类运行时配置（T20；spec 配置章节 / A-B-C 分类约定）。

C 类 = 运行时调参旋钮（非安全边界）：Dashboard 可编辑 + 热重载，经
``EvaluationConfigService.current()`` 在使用点查询；存储为 SQLite
``evaluation_config`` 单行逐字段 JSON 覆盖（只存被编辑字段，未编辑
字段跟随 env 基线），CAS 乐观锁。

字段分类（本文件是字段集与跨字段校验的单一来源）：
- ``evaluation_enabled``：运行开关（C 类允许范围内开关；B 类 env 仍
  是部署级总闸——禁用时整个子域不装配）；
- ``evaluation_sampling_rate`` / ``evaluation_failure_sampling_rate`` /
  ``evaluation_sampling_policy_version``：接 T16 EvaluationSamplingPolicy
  （捕获点 per-capture 查询，热重载即时生效）；
- ``evaluation_worker_concurrency``：worker LLM 并发（不超过 B 类
  ``evaluation_worker_max_concurrency`` 部署上限，跨层约束在本文件
  ``validate_evaluation_config`` 单一来源；当前装配下取值在启动期
  生效，运行期改动的生效边界见组合根装配说明）；
- ``evaluation_daily_call_ceiling``：Judge 日调用预算（0 = 不设上限）。

A 类安全不变量（tombstone 门禁/删除传播/Judge 只读/预算防超限语义）
不在本配置内，不可被任何配置覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class EvaluationConfigStoreError(Exception):
    """evaluation_config 存储读/写/解析失败（损坏必须显式暴露）。"""


class EvaluationConfigValidationError(Exception):
    """C 类配置字段校验失败（update 拒绝 / env 基线非法）。"""


class EvaluationConfigConflictError(Exception):
    """CAS 版本冲突（expected_version 与当前不符）。"""


@dataclass(frozen=True)
class EvaluationConfig:
    """解析后的评估 C 类配置（全部字段为具体值）。"""

    evaluation_enabled: bool = False
    evaluation_sampling_rate: float = 0.0
    evaluation_failure_sampling_rate: float = 0.0
    evaluation_sampling_policy_version: str = "capture-sampling-v1"
    evaluation_worker_concurrency: int = 1
    evaluation_daily_call_ceiling: int = 0


EVALUATION_CONFIG_FIELDS: tuple[str, ...] = (
    "evaluation_enabled",
    "evaluation_sampling_rate",
    "evaluation_failure_sampling_rate",
    "evaluation_sampling_policy_version",
    "evaluation_worker_concurrency",
    "evaluation_daily_call_ceiling",
)


@dataclass(frozen=True)
class EvaluationConfigOverrides:
    """逐字段 DB 覆盖。None = 未覆盖，跟随 env。"""

    evaluation_enabled: bool | None = None
    evaluation_sampling_rate: float | None = None
    evaluation_failure_sampling_rate: float | None = None
    evaluation_sampling_policy_version: str | None = None
    evaluation_worker_concurrency: int | None = None
    evaluation_daily_call_ceiling: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            f: getattr(self, f)
            for f in EVALUATION_CONFIG_FIELDS
            if getattr(self, f) is not None
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "EvaluationConfigOverrides":
        if not isinstance(data, dict):
            raise EvaluationConfigStoreError("overrides 必须是 JSON 对象")
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in EVALUATION_CONFIG_FIELDS:
                raise EvaluationConfigStoreError(f"未知覆盖键: {key}")
            if key == "evaluation_enabled":
                if not isinstance(value, bool):
                    raise EvaluationConfigStoreError(
                        f"覆盖 {key} 必须是 bool，收到 {type(value).__name__}"
                    )
            elif key == "evaluation_sampling_policy_version":
                if not isinstance(value, str) or not value:
                    raise EvaluationConfigStoreError(
                        f"覆盖 {key} 必须是非空字符串"
                    )
            elif key in (
                "evaluation_sampling_rate",
                "evaluation_failure_sampling_rate",
            ):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EvaluationConfigStoreError(
                        f"覆盖 {key} 必须是数值，收到 {type(value).__name__}"
                    )
                kwargs[key] = float(value)
                continue
            else:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise EvaluationConfigStoreError(
                        f"覆盖 {key} 必须是整数，收到 {type(value).__name__}"
                    )
            kwargs[key] = value
        return EvaluationConfigOverrides(**kwargs)

    def overridden_fields(self) -> tuple[str, ...]:
        return tuple(
            f for f in EVALUATION_CONFIG_FIELDS if getattr(self, f) is not None
        )


@dataclass(frozen=True)
class StoredEvaluationConfig:
    """存储行内容；version >= 1 表示行存在。"""

    overrides: EvaluationConfigOverrides
    version: int
    updated_at: str
    updated_by: str


@dataclass(frozen=True)
class ResolvedEvaluationConfig:
    """管理面读取投影；version=0 表示无 DB 行（首次写入 CAS 基点）。"""

    config: EvaluationConfig
    version: int
    overridden_fields: tuple[str, ...] = ()
    updated_at: str | None = None
    updated_by: str | None = None


def merge_overrides(
    base: EvaluationConfig, overrides: EvaluationConfigOverrides
) -> EvaluationConfig:
    """env 基线 + DB 覆盖 -> 解析配置。"""
    kwargs: dict[str, Any] = {}
    for field in EVALUATION_CONFIG_FIELDS:
        value = getattr(overrides, field)
        kwargs[field] = value if value is not None else getattr(base, field)
    return EvaluationConfig(**kwargs)


def validate_evaluation_config(
    cfg: EvaluationConfig,
    *,
    worker_max_concurrency: int,
    judge_configured: bool = False,
) -> None:
    """纯跨字段校验（env 启动与 DB 覆盖两层共用单一来源）。

    - C 并发不超过 B 类部署上限（worker_max_concurrency），且 >= 1；
    - 抽样率在 [0, 1]；
    - policy_version 非空；
    - 日调用预算 >= 0（0 = 不设上限）；Judge Provider 已配置时预算必须
      为正数（24 项三方审阅 #15：运行时把预算改 0 等于关闭 worker 预算
      预留，绕过启动期 B 类保护——热读 0 投影 None 后调用无上限）。
    """
    if isinstance(cfg.evaluation_worker_concurrency, bool) or not isinstance(
        cfg.evaluation_worker_concurrency, int
    ):
        raise EvaluationConfigValidationError(
            "evaluation_worker_concurrency 必须是整数"
        )
    if cfg.evaluation_worker_concurrency < 1:
        raise EvaluationConfigValidationError(
            "evaluation_worker_concurrency 必须 >= 1"
        )
    if cfg.evaluation_worker_concurrency > worker_max_concurrency:
        raise EvaluationConfigValidationError(
            "C 并发不得超过 B 类部署上限 "
            f"evaluation_worker_max_concurrency={worker_max_concurrency}: "
            f"收到 {cfg.evaluation_worker_concurrency}"
        )
    for label in (
        "evaluation_sampling_rate",
        "evaluation_failure_sampling_rate",
    ):
        value = getattr(cfg, label)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationConfigValidationError(f"{label} 必须是数值")
        if not 0.0 <= float(value) <= 1.0:
            raise EvaluationConfigValidationError(
                f"{label} 必须在 [0, 1]: {value}"
            )
    if not isinstance(cfg.evaluation_sampling_policy_version, str) or (
        not cfg.evaluation_sampling_policy_version
    ):
        raise EvaluationConfigValidationError(
            "evaluation_sampling_policy_version 必须是非空字符串"
        )
    if isinstance(cfg.evaluation_daily_call_ceiling, bool) or not isinstance(
        cfg.evaluation_daily_call_ceiling, int
    ):
        raise EvaluationConfigValidationError(
            "evaluation_daily_call_ceiling 必须是整数"
        )
    if cfg.evaluation_daily_call_ceiling < 0:
        raise EvaluationConfigValidationError(
            "evaluation_daily_call_ceiling 必须 >= 0（0 = 不设上限）"
        )
    if judge_configured and cfg.evaluation_daily_call_ceiling <= 0:
        # 24 项三方审阅 #15：运行时把预算改 0 等于关闭 worker 预算预留
        #（热读 0 投影 None 后调用无上限），绕过启动期 B 类保护
        raise EvaluationConfigValidationError(
            "Judge Provider 已配置时 evaluation_daily_call_ceiling 必须"
            f"为正数（0 = 无上限会关闭预算预留）: "
            f"收到 {cfg.evaluation_daily_call_ceiling}"
        )


def overrides_from_payload(
    payload: Mapping[str, Any],
) -> EvaluationConfigOverrides:
    """管理面 patch/replace 载荷 -> 严格覆盖对象（未知键/类型错误抛
    EvaluationConfigValidationError）。"""
    if not isinstance(payload, dict):
        raise EvaluationConfigValidationError("patch 必须是 JSON 对象")
    try:
        return EvaluationConfigOverrides.from_dict(payload)
    except EvaluationConfigStoreError as exc:
        raise EvaluationConfigValidationError(str(exc)) from exc


class EvaluationConfigStore(Protocol):
    """evaluation_config 单行存储端口（CAS 乐观锁）。"""

    async def get(self) -> StoredEvaluationConfig | None: ...

    async def save(
        self,
        overrides: EvaluationConfigOverrides,
        expected_version: int,
        updated_by: str,
    ) -> StoredEvaluationConfig: ...
