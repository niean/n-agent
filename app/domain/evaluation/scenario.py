# app/domain/evaluation/scenario.py
"""schema 2 场景数据契约（spec-261114-expand-e2e-experiments 4.1）。

声明式场景契约的领域值对象：profile/step/assertion/options/bindings/
lifecycle/source/required_metrics/comparison_scope 全部为封闭集合 +
构造期校验的 frozen dataclass。本模块零 IO、零网络、零业务初始化，
加载本身不执行数据携带的任何表达式。

准入纪律（全部 fail-fast，拒绝即终止）：
- profile 是 agent/dashboard/cli/acp/task_api 闭集；target_kind 不扩展
  （仍为 models.TARGET_KINDS 内的 agent_run），新执行形态由 profile 承载；
- step 是带稳定 step_id 的有序 chat | task_create 动作；task_create
  载荷仅接受 title/body/goal_mode/priority 白名单字段；
- assertion 只接受服务端已注册的解析/谓词类型，参数按键白名单 +
  JSON 标量约束，拒绝嵌套程序载荷（Python/JS/SQL/shell/正则程序/
  动态 import/任意路径访问表达式）；
- options 首版只接受 external_memory_enabled 逻辑别名；run_id、来源
  身份、批准令牌或任意 Provider 参数不在白名单，注入即拒绝；
- bindings 记录逻辑别名与不可变材料摘要（SHA-256），不包含服务器路径
  或凭据；source.repo_path 必须是仓库相对路径，拒绝绝对路径与 .. 逃逸。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

# schema 2 场景契约版本（与 manifest schema_version 独立承载的数据契约代）
SCENARIO_SCHEMA_VERSION = "2"

# execution.profile 闭集（spec 3/5.1）
SCENARIO_PROFILES: tuple[str, ...] = (
    "agent",
    "dashboard",
    "cli",
    "acp",
    "task_api",
)

# step.action 闭集
SCENARIO_STEP_ACTIONS: tuple[str, ...] = ("chat", "task_create")

# 注册断言谓词闭集（spec 4.1：仅服务端已注册解析/谓词，禁止程序表达式）
SCENARIO_ASSERTION_TYPES: tuple[str, ...] = (
    "tool_succeeded",
    "tool_absent",
    "result_contains",
    "kb_source_and_quote",
    "task_terminal",
    "artifact_set",
    "file_content",
    "browser_state",
    "protocol_sequence",
    # T11：CLI 通道真实入口核验
    "cli_channel",
)

# comparison_scope 闭集（spec 4.1：禁止自动混成同一可比总体）
SCENARIO_COMPARISON_SCOPES: tuple[str, ...] = (
    "fixed_fixture",
    "live_smoke",
    "channel_smoke",
)

# options 白名单（首版唯一逻辑别名）
SCENARIO_OPTION_KEYS: tuple[str, ...] = ("external_memory_enabled",)

# task_create 载荷白名单（spec 5.1：仅调用现有 Task 服务白名单字段）
TASK_CREATE_PAYLOAD_FIELDS: tuple[str, ...] = ("title", "body", "goal_mode", "priority")

# 整个场景级（非单步）断言/预期使用的保留 step 键
SCENARIO_WHOLE_SCENARIO_KEY = "*"

# 稳定标识符形态：step_id / 逻辑别名 / 生命周期规则 ID 共用
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

# 工具名 / 能力名形态（无路径分隔符、无空白、无 shell 元字符）
_CAPABILITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 断言参数键白名单 + 值类型约束（type 用 JSON 类型语义；bool 先于 int 检查）
_ASSERTION_PARAM_CONTRACTS: Mapping[str, Mapping[str, tuple[type, ...]]] = {
    "tool_succeeded": {"tool": (str,)},
    "tool_absent": {"tool": (str,)},
    "result_contains": {"text": (str,)},
    "kb_source_and_quote": {"kb_id": (str,), "min_results": (int,), "require_quote": (bool,)},
    "task_terminal": {"status": (str,), "within_seconds": (int,)},
    "artifact_set": {
        "min_count": (int,),
        "artifact_kind": (str,),
        # T10：storage_ref 实际核对（前缀 + workspace 文件基名目标集）
        "storage_ref_prefix": (str,),
        "workspace_files": (list,),
    },
    "file_content": {"filename": (str,), "contains": (str,)},
    "browser_state": {"state": (str,)},
    "protocol_sequence": {"entries": (list,)},
    # T11：CLI 通道真实入口核验（来源 + 会话绑定 + 步骤退出/输出）
    "cli_channel": {},
}


class ScenarioValidationError(ValueError):
    """场景契约不变量被违反（fail-fast，调用方必须终止）。"""


def _require_non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScenarioValidationError(f"{label} 必须是非空字符串: {value!r}")
    return value


def _require_identifier(value: Any, label: str) -> str:
    value = _require_non_empty_str(value, label)
    if not _IDENTIFIER_RE.match(value):
        raise ScenarioValidationError(
            f"{label} 必须匹配稳定标识符形态 {_IDENTIFIER_RE.pattern}: {value!r}"
        )
    return value


def _require_capability(value: Any, label: str) -> str:
    value = _require_non_empty_str(value, label)
    if not _CAPABILITY_RE.match(value) or ".." in value:
        raise ScenarioValidationError(
            f"{label} 不得包含路径/命令/空白等危险字符: {value!r}"
        )
    return value


def _require_sha256_hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise ScenarioValidationError(
            f"{label} 必须是 64 位小写十六进制 SHA-256 摘要: {value!r}"
        )
    return value


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioValidationError(f"{label} 必须是 JSON 对象: {value!r}")
    return MappingProxyType(dict(value))


def _freeze_str_tuple(values: Any, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ScenarioValidationError(f"{label} 必须是字符串数组: {values!r}")
    frozen = tuple(_require_non_empty_str(item, f"{label} 条目") for item in values)
    if len(set(frozen)) != len(frozen):
        raise ScenarioValidationError(f"{label} 不得有重复条目")
    return frozen


@dataclass(frozen=True)
class ScenarioStep:
    """单步动作：稳定 step_id + chat | task_create 载荷。"""

    step_id: str
    action: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_identifier(self.step_id, "step_id")
        if self.action not in SCENARIO_STEP_ACTIONS:
            raise ScenarioValidationError(
                f"非法 step action: {self.action!r}，仅允许 {SCENARIO_STEP_ACTIONS}"
            )
        if not isinstance(self.payload, Mapping):
            raise ScenarioValidationError(
                f"step {self.step_id!r} payload 必须是 JSON 对象"
            )
        payload = dict(self.payload)
        if self.action == "task_create":
            unknown = sorted(set(payload) - set(TASK_CREATE_PAYLOAD_FIELDS))
            if unknown:
                raise ScenarioValidationError(
                    f"step {self.step_id!r} task_create 载荷仅允许 "
                    f"{TASK_CREATE_PAYLOAD_FIELDS}，拒绝额外字段: {unknown}"
                )
            _require_non_empty_str(payload.get("title"), "task_create.title")
            _require_non_empty_str(payload.get("body"), "task_create.body")
            if not isinstance(payload.get("goal_mode"), bool):
                raise ScenarioValidationError("task_create.goal_mode 必须是 bool")
            priority = payload.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int) or priority <= 0:
                raise ScenarioValidationError("task_create.priority 必须是正整数")
        object.__setattr__(self, "payload", MappingProxyType(payload))


@dataclass(frozen=True)
class ScenarioExecution:
    """场景执行契约：profile 闭集 + 有序步骤 + 明确的预算分层。"""

    profile: str
    steps: tuple[ScenarioStep, ...]
    timeout_seconds: int
    step_timeouts: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if self.profile not in SCENARIO_PROFILES:
            raise ScenarioValidationError(
                f"非法 profile: {self.profile!r}，仅允许 {SCENARIO_PROFILES}"
            )
        steps = tuple(self.steps)
        if not steps:
            raise ScenarioValidationError("execution.steps 不得为空")
        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, ScenarioStep):
                raise ScenarioValidationError("steps 条目必须是 ScenarioStep")
            if step.step_id in seen:
                raise ScenarioValidationError(
                    f"step_id 重复（必须稳定唯一）: {step.step_id!r}"
                )
            seen.add(step.step_id)
        object.__setattr__(self, "steps", steps)
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ScenarioValidationError(
                f"timeout_seconds 必须是正整数: {self.timeout_seconds!r}"
            )
        if self.step_timeouts is not None:
            frozen = _freeze_mapping(self.step_timeouts, "step_timeouts")
            for step_id, budget in frozen.items():
                if step_id not in seen:
                    raise ScenarioValidationError(
                        f"step_timeouts 引用未知 step_id: {step_id!r}"
                    )
                if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
                    raise ScenarioValidationError(
                        f"step_timeouts[{step_id!r}] 必须是正整数: {budget!r}"
                    )
                if budget > self.timeout_seconds:
                    raise ScenarioValidationError(
                        f"step_timeouts[{step_id!r}]={budget} 不得超过 "
                        f"execution.timeout_seconds={self.timeout_seconds}"
                    )
            object.__setattr__(self, "step_timeouts", frozen)


@dataclass(frozen=True)
class ScenarioRequirements:
    """能力需求：所需工具、fixture 逻辑绑定与证据能力（不含路径/凭据/命令）。"""

    required_tools: tuple[str, ...] = ()
    fixture_binding: str | None = None
    evidence_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        tools = _freeze_str_tuple(self.required_tools, "required_tools")
        object.__setattr__(
            self,
            "required_tools",
            tuple(_require_capability(tool, "required_tools 条目") for tool in tools),
        )
        evidence = _freeze_str_tuple(self.evidence_capabilities, "evidence_capabilities")
        object.__setattr__(
            self,
            "evidence_capabilities",
            tuple(_require_capability(item, "evidence_capabilities 条目") for item in evidence),
        )
        if self.fixture_binding is not None:
            _require_identifier(self.fixture_binding, "fixture_binding")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ScenarioRequirements":
        if not isinstance(mapping, Mapping):
            raise ScenarioValidationError(
                f"requirements 必须是 JSON 对象: {mapping!r}"
            )
        allowed = {"required_tools", "fixture_binding", "evidence_capabilities"}
        unknown = sorted(set(mapping) - allowed)
        if unknown:
            raise ScenarioValidationError(
                f"requirements 存在未注册字段: {unknown}"
            )
        return cls(
            required_tools=mapping.get("required_tools", ()),
            fixture_binding=mapping.get("fixture_binding"),
            evidence_capabilities=mapping.get("evidence_capabilities", ()),
        )


@dataclass(frozen=True)
class ScenarioOptions:
    """场景选项：首版仅接受 external_memory_enabled 逻辑别名。"""

    external_memory_enabled: str | bool | None = None

    def __post_init__(self) -> None:
        if self.external_memory_enabled is None:
            return
        if isinstance(self.external_memory_enabled, bool):
            return
        _require_identifier(self.external_memory_enabled, "external_memory_enabled")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ScenarioOptions":
        if not isinstance(mapping, Mapping):
            raise ScenarioValidationError(f"options 必须是 JSON 对象: {mapping!r}")
        unknown = sorted(set(mapping) - set(SCENARIO_OPTION_KEYS))
        if unknown:
            raise ScenarioValidationError(
                f"options 仅允许 {SCENARIO_OPTION_KEYS}（逻辑别名，"
                f"不接受 run_id/来源身份/批准令牌/Provider 参数）: {unknown}"
            )
        return cls(external_memory_enabled=mapping.get("external_memory_enabled"))


@dataclass(frozen=True)
class ScenarioAssertion:
    """注册断言：类型闭集 + 参数键白名单 + JSON 标量约束。"""

    assertion_type: str
    params: Mapping[str, Any]
    step_id: str | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if self.assertion_type not in SCENARIO_ASSERTION_TYPES:
            raise ScenarioValidationError(
                f"非法 assertion_type: {self.assertion_type!r}，"
                f"仅允许注册谓词 {SCENARIO_ASSERTION_TYPES}"
            )
        params = dict(_freeze_mapping(self.params, "assertion.params"))
        contract = _ASSERTION_PARAM_CONTRACTS[self.assertion_type]
        unknown = sorted(set(params) - set(contract))
        if unknown:
            raise ScenarioValidationError(
                f"assertion {self.assertion_type!r} 存在未注册参数键（拒绝任意"
                f"表达式/正则/动态路径）: {unknown}"
            )
        missing = sorted(
            key for key in contract
            if key not in params and key in ("tool", "text", "status", "filename", "entries")
        )
        if missing:
            raise ScenarioValidationError(
                f"assertion {self.assertion_type!r} 缺少必需参数: {missing}"
            )
        for key, value in params.items():
            allowed_types = contract[key]
            if isinstance(value, bool) and bool not in allowed_types:
                raise ScenarioValidationError(
                    f"assertion {self.assertion_type!r} 参数 {key!r} 类型非法: {value!r}"
                )
            if not isinstance(value, allowed_types):
                raise ScenarioValidationError(
                    f"assertion {self.assertion_type!r} 参数 {key!r} 类型非法: {value!r}"
                )
            if isinstance(value, list):
                if not value or not all(
                    isinstance(item, str) and item for item in value
                ):
                    raise ScenarioValidationError(
                        f"assertion {self.assertion_type!r} 参数 {key!r} "
                        f"必须是非空字符串数组"
                    )
            elif isinstance(value, str):
                _require_non_empty_str(value, f"assertion 参数 {key!r}")
            elif isinstance(value, int) and not isinstance(value, bool) and value < 0:
                raise ScenarioValidationError(
                    f"assertion {self.assertion_type!r} 参数 {key!r} 不得为负"
                )
            elif isinstance(value, Mapping):
                raise ScenarioValidationError(
                    f"assertion {self.assertion_type!r} 参数 {key!r} 不得携带"
                    f"嵌套程序载荷"
                )
        if "filename" in params:
            filename = params["filename"]
            if "/" in filename or "\\" in filename or ".." in filename:
                raise ScenarioValidationError(
                    f"file_content.filename 只接受基名（拒绝路径访问）: {filename!r}"
                )
        if self.step_id is not None:
            _require_identifier(self.step_id, "assertion.step_id")
        if self.evidence_ref is not None:
            _require_identifier(self.evidence_ref, "assertion.evidence_ref")


@dataclass(frozen=True)
class ScenarioBinding:
    """逻辑绑定别名的不可变材料摘要。"""

    material: str
    sha256: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.material, "binding.material")
        if "/" in self.material or "\\" in self.material or ".." in self.material:
            raise ScenarioValidationError(
                f"binding.material 是逻辑材料标识，不得包含路径: {self.material!r}"
            )
        _require_sha256_hex(self.sha256, "binding.sha256")


@dataclass(frozen=True)
class ScenarioLifecycle:
    """生命周期：释放与保留策略的注册规则 ID（非脚本/自由命令）。"""

    release_rule: str
    retention_rule: str

    def __post_init__(self) -> None:
        _require_identifier(self.release_rule, "release_rule")
        _require_identifier(self.retention_rule, "retention_rule")


@dataclass(frozen=True)
class ScenarioSource:
    """源样例溯源：仓库相对路径 + 源 hash + 转换规则版本 + 变换清单。

    source_captured_at：live_smoke 样例的采集时间（ISO 8601 UTC），
    标识其时效边界；fixed_fixture/channel_smoke 可缺省。"""

    repo_path: str
    source_case_id: str
    source_sha256: str
    rules_version: str
    transformations: tuple[str, ...] = ()
    derived_from: str | None = None
    source_captured_at: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.repo_path, "source.repo_path")
        path = self.repo_path
        if path.startswith("/") or "\\" in path or ".." in path.split("/"):
            raise ScenarioValidationError(
                f"source.repo_path 必须是仓库相对路径，拒绝绝对路径与 .. 逃逸: "
                f"{path!r}"
            )
        _require_non_empty_str(self.source_case_id, "source.source_case_id")
        _require_sha256_hex(self.source_sha256, "source.source_sha256")
        _require_identifier(self.rules_version, "source.rules_version")
        object.__setattr__(
            self,
            "transformations",
            _freeze_str_tuple(self.transformations, "source.transformations"),
        )
        if self.derived_from is not None:
            _require_non_empty_str(self.derived_from, "source.derived_from")
        if self.source_captured_at is not None:
            value = self.source_captured_at
            if not isinstance(value, str) or not value:
                raise ScenarioValidationError(
                    "source.source_captured_at 必须是非空 ISO 8601 字符串"
                )
            try:
                datetime.fromisoformat(value)
            except ValueError:
                raise ScenarioValidationError(
                    f"source.source_captured_at 不是合法 ISO 8601: {value!r}"
                ) from None


@dataclass(frozen=True)
class ScenarioRequiredMetrics:
    """原要求覆盖清单：rubric 条款 -> 覆盖它的指标名。"""

    coverages: Mapping[str, str]

    def __post_init__(self) -> None:
        coverages = dict(_freeze_mapping(self.coverages, "required_metrics"))
        if not coverages:
            raise ScenarioValidationError("required_metrics 不得为空")
        for clause, metric in coverages.items():
            _require_non_empty_str(clause, "required_metrics 条款")
            _require_non_empty_str(metric, f"required_metrics[{clause!r}] 指标名")
        object.__setattr__(self, "coverages", MappingProxyType(coverages))


@dataclass(frozen=True)
class ScenarioExpectedOutput:
    """按 step_id 组织的原 rubric 与显式参考（"*" 表示整个场景）。"""

    steps: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        steps = dict(_freeze_mapping(self.steps, "expected_output.steps"))
        if not steps:
            raise ScenarioValidationError("expected_output.steps 不得为空")
        for step_id, payload in steps.items():
            if step_id != SCENARIO_WHOLE_SCENARIO_KEY:
                _require_identifier(step_id, "expected_output step_id")
            if not isinstance(payload, Mapping) or not payload:
                raise ScenarioValidationError(
                    f"expected_output[{step_id!r}] 必须是非空 JSON 对象"
                )
        frozen_steps = {key: MappingProxyType(dict(value)) for key, value in steps.items()}
        object.__setattr__(self, "steps", MappingProxyType(frozen_steps))


@dataclass(frozen=True)
class ScenarioCase:
    """schema 2 场景 case 扩展的聚合 VO（全部字段必填、未知字段拒绝）。"""

    execution: ScenarioExecution
    requirements: ScenarioRequirements
    options: ScenarioOptions
    assertions: tuple[ScenarioAssertion, ...]
    expected_output: ScenarioExpectedOutput
    bindings: Mapping[str, ScenarioBinding]
    lifecycle: ScenarioLifecycle
    required_metrics: ScenarioRequiredMetrics
    source: ScenarioSource
    comparison_scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, ScenarioExecution):
            raise ScenarioValidationError("execution 必须是 ScenarioExecution")
        if not isinstance(self.requirements, ScenarioRequirements):
            raise ScenarioValidationError("requirements 必须是 ScenarioRequirements")
        if not isinstance(self.options, ScenarioOptions):
            raise ScenarioValidationError("options 必须是 ScenarioOptions")
        assertions = tuple(self.assertions)
        if not assertions:
            raise ScenarioValidationError("assertions 不得为空")
        step_ids = {step.step_id for step in self.execution.steps}
        for assertion in assertions:
            if not isinstance(assertion, ScenarioAssertion):
                raise ScenarioValidationError("assertions 条目必须是 ScenarioAssertion")
            if (
                assertion.step_id is not None
                and assertion.step_id != SCENARIO_WHOLE_SCENARIO_KEY
                and assertion.step_id not in step_ids
            ):
                raise ScenarioValidationError(
                    f"assertion 引用未知 step_id: {assertion.step_id!r}，"
                    f"已知 {sorted(step_ids)} 或 {SCENARIO_WHOLE_SCENARIO_KEY!r}"
                )
        object.__setattr__(self, "assertions", assertions)
        if not isinstance(self.expected_output, ScenarioExpectedOutput):
            raise ScenarioValidationError(
                "expected_output 必须是 ScenarioExpectedOutput"
            )
        unknown_steps = sorted(
            set(self.expected_output.steps)
            - step_ids
            - {SCENARIO_WHOLE_SCENARIO_KEY}
        )
        if unknown_steps:
            raise ScenarioValidationError(
                f"expected_output 引用未知 step_id: {unknown_steps}，"
                f"已知 {sorted(step_ids)} 或 {SCENARIO_WHOLE_SCENARIO_KEY!r}"
            )
        bindings = dict(_freeze_mapping(self.bindings, "bindings"))
        for alias, binding in bindings.items():
            _require_identifier(alias, "binding 别名")
            if not isinstance(binding, ScenarioBinding):
                raise ScenarioValidationError(
                    f"binding {alias!r} 必须是 ScenarioBinding"
                )
        object.__setattr__(self, "bindings", MappingProxyType(bindings))
        if not isinstance(self.lifecycle, ScenarioLifecycle):
            raise ScenarioValidationError("lifecycle 必须是 ScenarioLifecycle")
        if not isinstance(self.required_metrics, ScenarioRequiredMetrics):
            raise ScenarioValidationError(
                "required_metrics 必须是 ScenarioRequiredMetrics"
            )
        if not isinstance(self.source, ScenarioSource):
            raise ScenarioValidationError("source 必须是 ScenarioSource")
        if self.comparison_scope not in SCENARIO_COMPARISON_SCOPES:
            raise ScenarioValidationError(
                f"非法 comparison_scope: {self.comparison_scope!r}，"
                f"仅允许 {SCENARIO_COMPARISON_SCOPES}"
            )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "ScenarioCase":
        """从 manifest case.scenario 解析：字段集合精确（未知/缺失均拒绝）。"""
        if not isinstance(mapping, Mapping):
            raise ScenarioValidationError(
                f"scenario 必须是 JSON 对象: {type(mapping).__name__}"
            )
        required = (
            "execution",
            "requirements",
            "options",
            "assertions",
            "expected_output",
            "bindings",
            "lifecycle",
            "required_metrics",
            "source",
            "comparison_scope",
        )
        missing = [key for key in required if key not in mapping]
        if missing:
            raise ScenarioValidationError(f"scenario 缺少必填字段: {missing}")
        unknown = sorted(set(mapping) - set(required))
        if unknown:
            raise ScenarioValidationError(f"scenario 存在未注册字段: {unknown}")

        execution_raw = mapping["execution"]
        if not isinstance(execution_raw, Mapping):
            raise ScenarioValidationError("execution 必须是 JSON 对象")
        execution_allowed = {"profile", "steps", "timeout_seconds", "step_timeouts"}
        execution_unknown = sorted(set(execution_raw) - execution_allowed)
        if execution_unknown:
            raise ScenarioValidationError(
                f"execution 存在未注册字段: {execution_unknown}"
            )
        steps_raw = execution_raw.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ScenarioValidationError("execution.steps 必须是非空数组")
        steps = []
        for position, step_raw in enumerate(steps_raw):
            if not isinstance(step_raw, Mapping):
                raise ScenarioValidationError(
                    f"execution.steps[{position}] 必须是 JSON 对象"
                )
            step_unknown = sorted(set(step_raw) - {"step_id", "action", "payload"})
            if step_unknown:
                raise ScenarioValidationError(
                    f"execution.steps[{position}] 存在未注册字段: {step_unknown}"
                )
            for field in ("step_id", "action", "payload"):
                if field not in step_raw:
                    raise ScenarioValidationError(
                        f"execution.steps[{position}] 缺少字段: {field}"
                    )
            steps.append(
                ScenarioStep(
                    step_id=step_raw["step_id"],
                    action=step_raw["action"],
                    payload=step_raw["payload"],
                )
            )
        execution = ScenarioExecution(
            profile=execution_raw["profile"],
            steps=tuple(steps),
            timeout_seconds=execution_raw["timeout_seconds"],
            step_timeouts=execution_raw.get("step_timeouts"),
        )

        assertions_raw = mapping["assertions"]
        if not isinstance(assertions_raw, list) or not assertions_raw:
            raise ScenarioValidationError("assertions 必须是非空数组")
        assertions = []
        for position, assertion_raw in enumerate(assertions_raw):
            if not isinstance(assertion_raw, Mapping):
                raise ScenarioValidationError(
                    f"assertions[{position}] 必须是 JSON 对象"
                )
            assertion_unknown = sorted(
                set(assertion_raw) - {"assertion_type", "params", "step_id", "evidence_ref"}
            )
            if assertion_unknown:
                raise ScenarioValidationError(
                    f"assertions[{position}] 存在未注册字段: {assertion_unknown}"
                )
            if "params" not in assertion_raw:
                raise ScenarioValidationError(
                    f"assertions[{position}] 缺少字段: params"
                )
            assertions.append(
                ScenarioAssertion(
                    assertion_type=assertion_raw["assertion_type"],
                    params=assertion_raw["params"],
                    step_id=assertion_raw.get("step_id"),
                    evidence_ref=assertion_raw.get("evidence_ref"),
                )
            )

        bindings_raw = mapping["bindings"]
        if not isinstance(bindings_raw, Mapping):
            raise ScenarioValidationError("bindings 必须是 JSON 对象")
        bindings = {}
        for alias, binding_raw in bindings_raw.items():
            if not isinstance(binding_raw, Mapping):
                raise ScenarioValidationError(f"bindings[{alias!r}] 必须是 JSON 对象")
            binding_unknown = sorted(set(binding_raw) - {"material", "sha256"})
            if binding_unknown:
                raise ScenarioValidationError(
                    f"bindings[{alias!r}] 存在未注册字段: {binding_unknown}"
                )
            bindings[alias] = ScenarioBinding(
                material=binding_raw.get("material"),
                sha256=binding_raw.get("sha256"),
            )

        lifecycle_raw = mapping["lifecycle"]
        if not isinstance(lifecycle_raw, Mapping):
            raise ScenarioValidationError("lifecycle 必须是 JSON 对象")
        lifecycle_unknown = sorted(set(lifecycle_raw) - {"release_rule", "retention_rule"})
        if lifecycle_unknown:
            raise ScenarioValidationError(
                f"lifecycle 存在未注册字段: {lifecycle_unknown}"
            )
        lifecycle = ScenarioLifecycle(
            release_rule=lifecycle_raw.get("release_rule"),
            retention_rule=lifecycle_raw.get("retention_rule"),
        )

        source_raw = mapping["source"]
        if not isinstance(source_raw, Mapping):
            raise ScenarioValidationError("source 必须是 JSON 对象")
        source_unknown = sorted(
            set(source_raw)
            - {
                "repo_path",
                "source_case_id",
                "source_sha256",
                "rules_version",
                "transformations",
                "derived_from",
                "source_captured_at",
            }
        )
        if source_unknown:
            raise ScenarioValidationError(
                f"source 存在未注册字段: {source_unknown}"
            )
        source = ScenarioSource(
            repo_path=source_raw.get("repo_path"),
            source_case_id=source_raw.get("source_case_id"),
            source_sha256=source_raw.get("source_sha256"),
            rules_version=source_raw.get("rules_version"),
            transformations=source_raw.get("transformations", ()),
            derived_from=source_raw.get("derived_from"),
            source_captured_at=source_raw.get("source_captured_at"),
        )

        expected_raw = mapping["expected_output"]
        if not isinstance(expected_raw, Mapping):
            raise ScenarioValidationError("expected_output 必须是 JSON 对象")
        expected_unknown = sorted(set(expected_raw) - {"steps"})
        if expected_unknown or "steps" not in expected_raw:
            raise ScenarioValidationError(
                f"expected_output 仅允许 steps 字段: {expected_unknown or '缺少 steps'}"
            )
        expected_output = ScenarioExpectedOutput(steps=expected_raw["steps"])

        return cls(
            execution=execution,
            requirements=ScenarioRequirements.from_mapping(mapping["requirements"]),
            options=ScenarioOptions.from_mapping(mapping["options"]),
            assertions=tuple(assertions),
            expected_output=expected_output,
            bindings=bindings,
            lifecycle=lifecycle,
            required_metrics=ScenarioRequiredMetrics(
                coverages=mapping["required_metrics"]
            ),
            source=source,
            comparison_scope=mapping["comparison_scope"],
        )
