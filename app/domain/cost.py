# app/domain/cost.py
"""Attempt 成本事实与纯计算器。

本模块是成本语义的契约基础：AttemptUsage 声明一次尝试的四桶 Token 事实，
CatalogResolution 声明定价目录的解析结论，calculate_attempt_cost 按固定判定
顺序产出 AttemptCost（estimated 或 unknown）。全程 Decimal 计算，零 IO、零
SDK 依赖，供目录、生命周期与观测出口各层复用。

桶词汇固定为 input、output、cache_read、cache_write。not_applicable 只能由
Provider 协议适配器声明（如 OpenAI 无 cache_write 概念、embeddings 无
output/cache 桶），计算器不代为推断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, MAX_EMAX, MIN_EMIN, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

# 成本事实 schema 版本；字段语义变化时递增
SCHEMA_VERSION: int = 1

# 固定桶词汇
BUCKETS: tuple[str, ...] = ("input", "output", "cache_read", "cache_write")

# C_bucket = T * P / 1_000_000（P 为 USD/百万 Token 单价）
_TOKENS_PER_MILLION_EXPONENT = -6

# 单价只接受非负合法十进制字符串（不允许正负号前缀；科学计数法指数可带符号）
_PRICE_PATTERN = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")

# 目录解析状态
_RESOLUTION_STATUSES: tuple[str, ...] = (
    "unavailable", "not_found", "ambiguous", "unsupported", "unique",
)

# 金额可表示性判定的容差基准
_DOUBLE_ABS_TOLERANCE = Decimal("1e-12")
_DOUBLE_REL_TOLERANCE = Decimal("1e-9")


class CostInvariantError(ValueError):
    """成本事实不变量被违反。"""


class UnknownReason(Enum):
    """成本无法估算的八类原因，判定顺序见 calculate_attempt_cost。"""

    missing_usage = "missing_usage"
    invalid_usage = "invalid_usage"
    price_not_found = "price_not_found"
    price_ambiguous = "price_ambiguous"
    unsupported_pricing = "unsupported_pricing"
    identity_unresolved = "identity_unresolved"
    pricing_unavailable = "pricing_unavailable"
    amount_unrepresentable = "amount_unrepresentable"


def _parse_price(value: Any, label: str) -> None:
    """校验单价字符串为非负有限十进制，非法时抛 CostInvariantError。

    价格非负：拒绝负值与带正号前缀的形式，fail-fast 于条目构造期。
    """
    if not isinstance(value, str) or not _PRICE_PATTERN.match(value):
        raise CostInvariantError(f"{label} 不是合法非负十进制字符串: {value!r}")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise CostInvariantError(f"{label} 必须是有限十进制: {value!r}")


@dataclass(frozen=True)
class TokenValue:
    """单桶 Token 事实：known 携带原始值，unknown 表示协议未上报，
    not_applicable 表示该桶在 Provider 协议中不存在。known 的原始值合法性
    由 calculate_attempt_cost 校验（拒绝 bool/浮点/字符串/负数等）。"""

    kind: str  # "known" | "unknown" | "not_applicable"
    value: Any = None

    @classmethod
    def known(cls, value: Any) -> "TokenValue":
        return cls(kind="known", value=value)

    @classmethod
    def unknown(cls) -> "TokenValue":
        return cls(kind="unknown", value=None)

    @classmethod
    def not_applicable(cls) -> "TokenValue":
        return cls(kind="not_applicable", value=None)

    def __post_init__(self) -> None:
        if self.kind not in ("known", "unknown", "not_applicable"):
            raise CostInvariantError(f"非法 TokenValue kind: {self.kind!r}")
        if self.kind == "known" and self.value is None:
            raise CostInvariantError("known TokenValue 必须携带 value")


@dataclass(frozen=True)
class AttemptUsage:
    """一次尝试的四桶 Token 事实。必须显式声明全部四桶，缺失语义由
    TokenValue.unknown / not_applicable 表达，不允许省略键。"""

    buckets: Mapping[str, TokenValue]

    def __post_init__(self) -> None:
        buckets = dict(self.buckets)
        if set(buckets.keys()) != set(BUCKETS):
            raise CostInvariantError(
                f"AttemptUsage 必须恰好声明四桶 {BUCKETS}，实际: {sorted(buckets.keys())}"
            )
        for bucket, token_value in buckets.items():
            if not isinstance(token_value, TokenValue):
                raise CostInvariantError(f"桶 {bucket} 的值必须是 TokenValue")
        object.__setattr__(self, "buckets", MappingProxyType(buckets))


@dataclass(frozen=True)
class PricingCatalogEntry:
    """定价目录条目。每桶单价为 USD/百万 Token 的十进制字符串，None 表示
    该桶不适用（不可计费）。单价只从合法十进制字符串读取。"""

    price_id: str
    billing_provider: str
    billing_model: str
    operation: str  # "chat" | "embeddings"
    aliases: tuple[str, ...] = ()
    conditions: Mapping[str, str] | None = None
    unit_prices: Mapping[str, str | None] = field(default_factory=dict)
    source_url: str = ""
    verified_at: str = ""

    def __post_init__(self) -> None:
        for label, value in (
            ("price_id", self.price_id),
            ("billing_provider", self.billing_provider),
            ("billing_model", self.billing_model),
        ):
            if not isinstance(value, str) or not value:
                raise CostInvariantError(f"{label} 必须是非空字符串")
        if self.operation not in ("chat", "embeddings"):
            raise CostInvariantError(f"非法 operation: {self.operation!r}")
        aliases = tuple(self.aliases)
        if not all(isinstance(a, str) and a for a in aliases):
            raise CostInvariantError("aliases 必须是非空字符串列表")
        object.__setattr__(self, "aliases", aliases)
        if self.conditions is not None:
            object.__setattr__(
                self, "conditions", MappingProxyType(dict(self.conditions))
            )
        prices = dict(self.unit_prices)
        for bucket, price in prices.items():
            if bucket not in BUCKETS:
                raise CostInvariantError(f"非法单价桶: {bucket!r}")
            if price is not None:
                _parse_price(price, f"桶 {bucket} 单价")
        if not any(price is not None for price in prices.values()):
            raise CostInvariantError("条目必须至少声明一个适用桶单价")
        object.__setattr__(self, "unit_prices", MappingProxyType(prices))


@dataclass(frozen=True)
class CatalogResolution:
    """定价目录解析结论。显式区分目录失效（unavailable）、零匹配
    （not_found）、歧义（ambiguous）、条件不可判定（unsupported）与唯一
    命中（unique），避免以 entry_or_none 混淆不同失效模式。"""

    status: str
    entry: PricingCatalogEntry | None = None
    pricing_version: str | None = None
    price_id: str | None = None
    matched_price_ids: tuple[str, ...] = ()

    @classmethod
    def unavailable(cls, pricing_version: str | None = None) -> "CatalogResolution":
        return cls(status="unavailable", pricing_version=pricing_version)

    @classmethod
    def not_found(cls, pricing_version: str | None = None) -> "CatalogResolution":
        return cls(status="not_found", pricing_version=pricing_version)

    @classmethod
    def ambiguous(
        cls, matched_price_ids: tuple[str, ...], pricing_version: str | None = None,
    ) -> "CatalogResolution":
        return cls(
            status="ambiguous",
            pricing_version=pricing_version,
            matched_price_ids=tuple(matched_price_ids),
        )

    @classmethod
    def unsupported(cls, pricing_version: str | None = None) -> "CatalogResolution":
        return cls(status="unsupported", pricing_version=pricing_version)

    @classmethod
    def unique(
        cls, entry: PricingCatalogEntry, pricing_version: str | None = None,
    ) -> "CatalogResolution":
        if not isinstance(entry, PricingCatalogEntry):
            raise CostInvariantError("unique 状态必须携带 entry")
        return cls(
            status="unique",
            entry=entry,
            price_id=entry.price_id,
            pricing_version=pricing_version,
        )

    def __post_init__(self) -> None:
        if self.status not in _RESOLUTION_STATUSES:
            raise CostInvariantError(f"非法目录解析状态: {self.status!r}")
        if self.status == "unique":
            if not isinstance(self.entry, PricingCatalogEntry):
                raise CostInvariantError("unique 状态必须携带 entry")
            if self.price_id != self.entry.price_id or not self.price_id:
                raise CostInvariantError("unique 状态必须携带唯一 price_id")
            if self.matched_price_ids:
                raise CostInvariantError("unique 状态不得携带 matched_price_ids")
        elif self.status == "ambiguous":
            if self.entry is not None or self.price_id is not None:
                raise CostInvariantError("ambiguous 状态不得携带 entry 或 price_id")
            if len(self.matched_price_ids) < 2:
                raise CostInvariantError("ambiguous 状态必须携带至少两个匹配 price_id")
        else:
            if self.entry is not None or self.price_id is not None:
                raise CostInvariantError(f"{self.status} 状态不得携带 entry 或 price_id")
            if self.matched_price_ids:
                raise CostInvariantError(f"{self.status} 状态不得携带 matched_price_ids")


def _sum_decimals(values) -> Decimal:
    """在足够精度的局部 context 中求和，避免默认 28 位舍入。"""
    with localcontext() as ctx:
        ctx.prec = 200
        total = Decimal(0)
        for value in values:
            total += value
        return total


@dataclass(frozen=True)
class AttemptCost:
    """一次尝试的成本事实。estimated 携带精确 Decimal 金额与分项；unknown
    携带 UnknownReason，金额与分项为空。不变量在构造时校验。"""

    status: str  # "estimated" | "unknown"
    schema_version: int = SCHEMA_VERSION
    currency: str = "USD"
    amount_usd: Decimal | None = None
    components: Mapping[str, Decimal] = field(default_factory=dict)
    pricing_version: str | None = None
    price_id: str | None = None
    reason: UnknownReason | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise CostInvariantError(f"非法 schema_version: {self.schema_version!r}")
        if self.currency != "USD":
            raise CostInvariantError(f"非法 currency: {self.currency!r}")
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))
        if self.status == "estimated":
            self._validate_estimated()
        elif self.status == "unknown":
            self._validate_unknown()
        else:
            raise CostInvariantError(f"非法 status: {self.status!r}")

    def _validate_estimated(self) -> None:
        if self.reason is not None:
            raise CostInvariantError("estimated 状态不得携带 reason")
        if not isinstance(self.pricing_version, str) or not self.pricing_version:
            raise CostInvariantError("estimated 状态必须携带非空 pricing_version")
        if not isinstance(self.price_id, str) or not self.price_id:
            raise CostInvariantError("estimated 状态必须携带非空 price_id")
        amount = self.amount_usd
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
            raise CostInvariantError("estimated 状态必须携带有限非负 amount_usd")
        components = dict(self.components)
        if not components:
            raise CostInvariantError("estimated 状态必须携带至少一个分项")
        for bucket, value in components.items():
            if bucket not in BUCKETS:
                raise CostInvariantError(f"非法分项桶: {bucket!r}")
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise CostInvariantError(f"分项 {bucket} 必须是有限非负 Decimal")
        total = _sum_decimals(components.values())
        if total != amount:
            raise CostInvariantError(
                f"分项之和 {total} 不等于总额 {amount}，拒绝 estimated"
            )

    def _validate_unknown(self) -> None:
        if not isinstance(self.reason, UnknownReason):
            raise CostInvariantError("unknown 状态必须携带 UnknownReason")
        if self.amount_usd is not None or self.components:
            raise CostInvariantError("unknown 状态不得携带金额或分项")


def is_double_representable(amount: Decimal) -> bool:
    """判定 Decimal 金额转 double 后是否仍然可信。

    规则：非有限金额不可表示；金额为 0 恒可表示；转 double 下溢为 0 而原值
    非零则不可表示；其余按绝对误差是否超过 max(1e-12, |amount| * 1e-9) 判定。
    注意本函数不检查符号（负数按绝对值规则判定），调用方（如观测出口）如需
    拒绝负金额须自行校验。
    供观测出口在 Decimal 转 double 序列化前复用。
    """
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return False
    if amount == 0:
        return True
    try:
        as_double = float(amount)
    except (OverflowError, ValueError):
        # 超出 double 指数范围视为不可表示
        return False
    if as_double == 0.0:
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            ctx.Emax = MAX_EMAX
            ctx.Emin = MIN_EMIN
            abs_error = abs(Decimal(as_double) - amount)
            tolerance = max(_DOUBLE_ABS_TOLERANCE, abs(amount) * _DOUBLE_REL_TOLERANCE)
            return abs_error <= tolerance
    except DecimalException:
        # 金额量级超出 Decimal 上下文可运算范围，必然不可表示
        return False


# cost_decimal 规范十进制字符串投影上限：非指数十进制，整数位与小数位各
# 340 位（与观测契约、Collector 护栏一致）。Domain 是唯一守门点——超过上限
# 的 estimated 金额无法在 Span/完成日志/Langfuse 完整投影（下游整组拒绝，
# 禁止静默丢字段），必须在源头按 amount_unrepresentable 收口。
COST_DECIMAL_MAX_SIDE_DIGITS: int = 340


def canonical_decimal_string(value: Decimal) -> str:
    """规范十进制字符串（无指数、无冗余尾零），供 cost_decimal 三后端
    逐字节一致。金额已通过 double 可表示性判定（有效数字有限，局部高精度
    安全）。"""
    with localcontext() as ctx:
        ctx.prec = 60
        return format(Decimal(value).normalize(), "f")


def _exceeds_decimal_projection_bound(amount: Decimal) -> bool:
    """判定金额的规范十进制字符串是否超出 cost_decimal 投影上限。

    非负有限金额的规范字符串形如 ``<整数>[.<小数>]``；任一侧位数超过
    COST_DECIMAL_MAX_SIDE_DIGITS 即视为无法完整投影。观测出口以同一
    canonical_decimal_string 形式序列化，本判定与下游护栏逐字节对齐。"""
    text = canonical_decimal_string(amount)
    integer_part, _, fraction_part = text.partition(".")
    return (
        len(integer_part) > COST_DECIMAL_MAX_SIDE_DIGITS
        or len(fraction_part) > COST_DECIMAL_MAX_SIDE_DIGITS
    )


def _unknown_cost(reason: UnknownReason) -> AttemptCost:
    return AttemptCost(status="unknown", reason=reason)


def _required_precision(usage: AttemptUsage, entry: PricingCatalogEntry) -> int:
    """按 Token 位数与单价字符串长度估算所需局部精度，避免默认 28 位舍入。"""
    precision = 60
    for bucket in BUCKETS:
        price = entry.unit_prices.get(bucket)
        token_value = usage.buckets[bucket]
        if price is None or token_value.kind != "known":
            continue
        digits = len(str(abs(token_value.value))) + len(price) + 10
        precision = max(precision, digits)
    return precision


def calculate_attempt_cost(
    usage: AttemptUsage,
    identity: tuple[str, str] | None,
    catalog_resolution: CatalogResolution,
) -> AttemptCost:
    """纯函数：由 Token 事实、计费身份与目录解析结论计算 AttemptCost。

    判定顺序固定为 identity_unresolved、invalid_usage、missing_usage、
    pricing_unavailable、price_not_found、price_ambiguous、
    unsupported_pricing、amount_unrepresentable；多因同时存在时取顺序靠前者。
    """
    if identity is None:
        return _unknown_cost(UnknownReason.identity_unresolved)

    # 第一遍：全量校验所有 known 桶的合法性，invalid_usage 优先于 missing_usage
    # 且与桶位置无关；第二遍才判定 unknown 桶
    for bucket in BUCKETS:
        token_value = usage.buckets[bucket]
        if token_value.kind == "known":
            value = token_value.value
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return _unknown_cost(UnknownReason.invalid_usage)
    for bucket in BUCKETS:
        if usage.buckets[bucket].kind == "unknown":
            return _unknown_cost(UnknownReason.missing_usage)

    status = catalog_resolution.status
    if status == "unavailable":
        return _unknown_cost(UnknownReason.pricing_unavailable)
    if status == "not_found":
        return _unknown_cost(UnknownReason.price_not_found)
    if status == "ambiguous":
        return _unknown_cost(UnknownReason.price_ambiguous)
    if status == "unsupported":
        return _unknown_cost(UnknownReason.unsupported_pricing)
    entry = catalog_resolution.entry
    if entry is None:
        return _unknown_cost(UnknownReason.pricing_unavailable)

    # 唯一命中项已定位：此后的 unknown 保留计价身份（spec：price_id 在已
    # 唯一定位价格项的 unknown 上保留，供追溯；版本随目录解析结论携带）。
    # 未唯一定位价格项的 unknown（判定顺序前段）不保留 price_id。
    def _resolved_unknown(reason: UnknownReason) -> AttemptCost:
        return AttemptCost(
            status="unknown", reason=reason,
            pricing_version=catalog_resolution.pricing_version,
            price_id=entry.price_id,
        )

    components: dict[str, Decimal] = {}
    try:
        with localcontext() as ctx:
            ctx.prec = _required_precision(usage, entry)
            for bucket in BUCKETS:
                price = entry.unit_prices.get(bucket)
                token_value = usage.buckets[bucket]
                if token_value.kind == "not_applicable":
                    # 协议适配器声明该桶不适用：按适用性先判定（评审修复
                    # 第三轮），无论目录是否为该桶提供单价，该桶不参与计价
                    # （贡献零、分项省略）；目录计价与协议声明不构成矛盾
                    continue
                if price is None:
                    # known 桶需要单价而命中条目自身缺单价属于条目级配置
                    # 缺口（无效目录），按 pricing_unavailable 收口，不得
                    # 归咎响应、不得按零价或部分成本放行
                    return _resolved_unknown(UnknownReason.pricing_unavailable)
                component = (
                    Decimal(token_value.value) * Decimal(price)
                ).scaleb(_TOKENS_PER_MILLION_EXPONENT)
                if not is_double_representable(component):
                    return _resolved_unknown(UnknownReason.amount_unrepresentable)
                components[bucket] = component

            if not components:
                # 防御分支：条目构造已保证至少一个适用桶，正常流不可达
                return _resolved_unknown(UnknownReason.invalid_usage)
            total = _sum_decimals(components.values())
            if not is_double_representable(total):
                return _resolved_unknown(UnknownReason.amount_unrepresentable)
            if _exceeds_decimal_projection_bound(total):
                # 金额可转 double 但规范十进制字符串超出 cost_decimal 投影
                # 上限：下游整组拒绝、禁止静默丢字段，按同一原因收口
                return _resolved_unknown(UnknownReason.amount_unrepresentable)
    except DecimalException:
        # 目录条目合法但金额计算越界（如指数超出 Decimal 上下文范围）：
        # 纯函数契约要求有效条目不得抛出异常，降级为 amount_unrepresentable
        return _resolved_unknown(UnknownReason.amount_unrepresentable)

    try:
        return AttemptCost(
            status="estimated",
            amount_usd=total,
            components=components,
            pricing_version=catalog_resolution.pricing_version,
            price_id=entry.price_id,
        )
    except CostInvariantError:
        # 分项之和与总额不一致等不变量失败：拒绝 estimated
        return _resolved_unknown(UnknownReason.amount_unrepresentable)
