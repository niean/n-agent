# tests/domain/test_cost.py
from decimal import Decimal

import pytest

from app.domain.cost import (
    BUCKETS,
    AttemptCost,
    AttemptUsage,
    CatalogResolution,
    CostInvariantError,
    PricingCatalogEntry,
    TokenValue,
    UnknownReason,
    calculate_attempt_cost,
    is_double_representable,
)

NA = "na"
UNK = "unk"

IDENTITY = ("anthropic", "claude-test")
PRICING_VERSION = "v-test-1"


def make_entry(unit_prices=None, price_id="price-test-1"):
    if unit_prices is None:
        unit_prices = {
            "input": "2",
            "output": "10",
            "cache_read": "0.2",
            "cache_write": "2.5",
        }
    return PricingCatalogEntry(
        price_id=price_id,
        billing_provider="anthropic",
        billing_model="claude-test",
        operation="chat",
        aliases=(),
        conditions=None,
        unit_prices=unit_prices,
        source_url="https://example.test/pricing",
        verified_at="2026-11-09",
    )


def make_resolution(unit_prices=None, status="unique"):
    if status == "unique":
        return CatalogResolution.unique(make_entry(unit_prices), pricing_version=PRICING_VERSION)
    if status == "unavailable":
        return CatalogResolution.unavailable(pricing_version=PRICING_VERSION)
    if status == "not_found":
        return CatalogResolution.not_found(pricing_version=PRICING_VERSION)
    if status == "ambiguous":
        return CatalogResolution.ambiguous(("p1", "p2"), pricing_version=PRICING_VERSION)
    if status == "unsupported":
        return CatalogResolution.unsupported(pricing_version=PRICING_VERSION)
    raise ValueError(status)


def make_usage(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0):
    buckets = {}
    for bucket, value in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("cache_read", cache_read_tokens),
        ("cache_write", cache_write_tokens),
    ):
        if value == NA:
            buckets[bucket] = TokenValue.not_applicable()
        elif value == UNK:
            buckets[bucket] = TokenValue.unknown()
        else:
            buckets[bucket] = TokenValue.known(value)
    return AttemptUsage(buckets=buckets)


def test_bucket_vocabulary_is_fixed():
    assert BUCKETS == ("input", "output", "cache_read", "cache_write")


def test_vector_a_full_four_buckets_estimated():
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=10)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.status == "estimated"
    assert cost.reason is None
    assert cost.schema_version == 1
    assert cost.currency == "USD"
    assert cost.amount_usd == Decimal("0.000435")
    assert cost.components["input"] == Decimal("0.0002")
    assert cost.components["output"] == Decimal("0.0002")
    assert cost.components["cache_read"] == Decimal("0.00001")
    assert cost.components["cache_write"] == Decimal("0.000025")
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"


def test_vector_b_openai_cache_write_not_applicable():
    unit_prices = {"input": "2", "output": "10", "cache_read": "0.2", "cache_write": None}
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0.00041")
    assert cost.components["input"] == Decimal("0.0002")
    assert cost.components["output"] == Decimal("0.0002")
    assert cost.components["cache_read"] == Decimal("0.00001")
    assert "cache_write" not in cost.components


def test_vector_c_input_only_kb_style():
    unit_prices = {"input": "0.1", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=200, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, ("n-kb", "kb-model"), make_resolution(unit_prices))
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0.00002")
    assert cost.components == {"input": Decimal("0.00002")}


def test_zero_tokens_and_zero_price_estimated_zero():
    unit_prices = {"input": "0", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=0, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0")
    assert cost.components == {"input": Decimal("0")}


def test_reason_identity_unresolved():
    usage = make_usage(input_tokens=100, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, None, make_resolution())
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.identity_unresolved
    assert cost.amount_usd is None
    assert not cost.components


def test_reason_invalid_usage():
    usage = make_usage(input_tokens=-1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.invalid_usage


def test_reason_missing_usage():
    usage = make_usage(input_tokens=100, output_tokens=UNK, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.missing_usage


def test_reason_pricing_unavailable():
    usage = make_usage(input_tokens=100, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="unavailable"))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_reason_price_not_found():
    usage = make_usage(input_tokens=100, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="not_found"))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.price_not_found


def test_reason_price_ambiguous():
    usage = make_usage(input_tokens=100, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="ambiguous"))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.price_ambiguous


def test_reason_unsupported_pricing():
    usage = make_usage(input_tokens=100, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="unsupported"))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.unsupported_pricing


def test_reason_amount_unrepresentable_underflow():
    # 1 token * 1e-330 USD/百万 = 1e-336，double 下溢为 0
    unit_prices = {"input": "1e-330", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.amount_unrepresentable


def test_identity_unresolved_wins_over_invalid_usage():
    usage = make_usage(input_tokens=-1, output_tokens=UNK, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, None, make_resolution())
    assert cost.reason is UnknownReason.identity_unresolved


def test_invalid_usage_wins_over_missing_usage():
    usage = make_usage(input_tokens=-1, output_tokens=UNK, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.reason is UnknownReason.invalid_usage


def test_invalid_usage_wins_over_missing_usage_reversed_positions():
    # invalid 桶位于 unknown 桶之后，仍必须优先报 invalid_usage
    usage = make_usage(input_tokens=UNK, output_tokens=True, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.reason is UnknownReason.invalid_usage

    usage = make_usage(input_tokens=UNK, output_tokens=UNK, cache_read_tokens="5", cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.reason is UnknownReason.invalid_usage


def test_huge_exponent_price_returns_amount_unrepresentable():
    # 合法十进制字符串但数值越界：有效目录条目不得抛异常
    unit_prices = {
        "input": "1e999999999",
        "output": None,
        "cache_read": None,
        "cache_write": None,
    }
    usage = make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.amount_unrepresentable

    # adjusted 指数超出默认 Emax，乘法触发 Overflow，同样不得抛异常
    unit_prices = {
        "input": "1e1000000000",
        "output": None,
        "cache_read": None,
        "cache_write": None,
    }
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.amount_unrepresentable


def test_is_double_representable_rejects_out_of_range_magnitude():
    assert is_double_representable(Decimal("1e999999999")) is False
    assert is_double_representable(Decimal("-1e999999999")) is False


def test_missing_usage_wins_over_pricing_unavailable():
    usage = make_usage(input_tokens=100, output_tokens=UNK, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="unavailable"))
    assert cost.reason is UnknownReason.missing_usage


def test_unsupported_pricing_wins_over_amount_unrepresentable():
    unit_prices = {"input": "1e-330", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices, status="unsupported"))
    assert cost.reason is UnknownReason.unsupported_pricing


@pytest.mark.parametrize("bad_value", [True, False, 1.5, "5", float("nan"), float("inf"), -1, -100])
def test_reject_invalid_known_tokens(bad_value):
    usage = make_usage(input_tokens=bad_value, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.invalid_usage


def test_entry_missing_needed_price_is_pricing_unavailable():
    """命中条目自身缺单价属于条目级配置缺口（无效目录），按
    pricing_unavailable 收口，不得归咎响应（invalid_usage）；
    not_applicable 桶不参与计价，不受影响。"""
    unit_prices = {"input": "2", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_entry_missing_cache_read_price_with_cache_read_usage_pricing_unavailable():
    """缺陷场景锁定：条目缺 cache_read 单价仍可加载（加载只强制 chat
    input/output），合法 OpenAI 用量带 cache_read 命中该条目时必须报
    pricing_unavailable（条目级配置缺口），而非 invalid_usage。"""
    unit_prices = {"input": "2", "output": "10", "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.pricing_unavailable


def test_components_sum_mismatch_rejects_estimated():
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            amount_usd=Decimal("1"),
            components={"input": Decimal("2")},
            pricing_version=PRICING_VERSION,
            price_id="price-test-1",
        )


def test_estimated_requires_amount_components_and_pricing_identity():
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            components={"input": Decimal("1")},
            pricing_version=PRICING_VERSION,
            price_id="price-test-1",
        )
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            amount_usd=Decimal("1"),
            components={"input": Decimal("1")},
        )
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            amount_usd=Decimal("1"),
            components={"input": Decimal("1")},
            pricing_version=PRICING_VERSION,
            price_id="price-test-1",
            reason=UnknownReason.missing_usage,
        )


def test_estimated_rejects_negative_or_invalid_components():
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            amount_usd=Decimal("-1"),
            components={"input": Decimal("-1")},
            pricing_version=PRICING_VERSION,
            price_id="price-test-1",
        )
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="estimated",
            amount_usd=Decimal("1"),
            components={"unknown_bucket": Decimal("1")},
            pricing_version=PRICING_VERSION,
            price_id="price-test-1",
        )


def test_unknown_requires_reason_and_empty_amount_components():
    cost = AttemptCost(status="unknown", reason=UnknownReason.missing_usage)
    assert cost.status == "unknown"
    assert cost.amount_usd is None
    assert not cost.components
    with pytest.raises(CostInvariantError):
        AttemptCost(status="unknown")
    with pytest.raises(CostInvariantError):
        AttemptCost(status="unknown", reason=UnknownReason.missing_usage, amount_usd=Decimal("1"))
    with pytest.raises(CostInvariantError):
        AttemptCost(
            status="unknown",
            reason=UnknownReason.missing_usage,
            components={"input": Decimal("1")},
        )


def test_precision_beyond_28_significant_digits():
    # 33 位有效数字单价，默认 28 位 context 会舍入，必须精确
    price = "0.123456789012345678901234567890123"
    unit_prices = {"input": price, "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=1_000_000, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "estimated"
    expected = Decimal(price)
    assert cost.components["input"] == expected
    assert cost.amount_usd == expected


def test_is_double_representable_helper():
    assert is_double_representable(Decimal("0.000435")) is True
    assert is_double_representable(Decimal("0")) is True
    assert is_double_representable(Decimal("1e-330")) is False
    assert is_double_representable(Decimal("NaN")) is False
    assert is_double_representable(Decimal("Infinity")) is False


def test_catalog_resolution_validation():
    with pytest.raises(CostInvariantError):
        CatalogResolution.ambiguous(("only-one",))
    with pytest.raises(CostInvariantError):
        CatalogResolution.unique(None)
    resolution = CatalogResolution.unique(make_entry(), pricing_version=PRICING_VERSION)
    assert resolution.status == "unique"
    assert resolution.price_id == "price-test-1"
    assert resolution.entry is not None


def test_usage_must_declare_all_four_buckets():
    with pytest.raises(CostInvariantError):
        AttemptUsage(buckets={"input": TokenValue.known(1)})


def test_pricing_entry_rejects_non_decimal_price_strings():
    with pytest.raises(CostInvariantError):
        make_entry(unit_prices={"input": "abc", "output": None})
    with pytest.raises(CostInvariantError):
        make_entry(unit_prices={"input": "NaN", "output": None})
    with pytest.raises(CostInvariantError):
        make_entry(unit_prices={"input": "Infinity", "output": None})


def test_pricing_entry_rejects_signed_price_strings():
    # 价格非负：负值与带正号前缀的形式必须在条目构造期 fail-fast 拒绝
    for bad_price in ("-2", "+2", "-0", "-0.2", "+0.2", "-1e-3"):
        with pytest.raises(CostInvariantError):
            make_entry(unit_prices={"input": bad_price, "output": None})


def test_priced_bucket_with_not_applicable_tokens_skipped():
    """评审修复（第三轮）：协议声明 not_applicable 的桶按适用性先判定，
    无论目录是否为该桶提供单价都跳过计价（贡献零、分项省略）。全部桶均被
    声明不适用时无任何适用分项，防御分支仍按 invalid_usage 拒绝（不得产出
    无分项的 estimated）。"""
    unit_prices = {"input": "2", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=NA, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.invalid_usage


def test_not_applicable_bucket_skipped_even_when_catalog_prices_it():
    """评审修复（第三轮）：桶适用性先于单价判定。协议声明 not_applicable
    的桶无论目录是否提供单价都不参与计价（贡献零、分项省略，spec：不适用
    分项省略）；缺失单价 -> pricing_unavailable 只适用于 known 桶。"""
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution())
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("0.00041")
    assert set(cost.components) == {"input", "output", "cache_read"}
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"


def test_invalid_usage_wins_over_pricing_unavailable():
    # Token 非法与目录失效同时存在：invalid_usage 优先
    usage = make_usage(input_tokens=-1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status="unavailable"))
    assert cost.reason is UnknownReason.invalid_usage


# ---------------------------------------------------------------------------
# 评审修复（第二轮）：unknown 在唯一定位价格项后保留计价身份。
# spec：price_id "unknown 仅在已唯一定位价格项时保留，否则省略"；
# 唯一命中后的 unknown（amount_unrepresentable / 命中项缺适用桶单价）保留
# version + price_id 供追溯。评审修复（第三轮）：not_applicable 桶按适用性
# 先判定跳过计价，"协议不适用桶被计价" 不再构成 unknown 场景。
# ---------------------------------------------------------------------------


def test_unknown_after_unique_resolution_retains_price_identity():
    # amount_unrepresentable（唯一命中项已定位）：保留 version + price_id
    unit_prices = {"input": "1e-330", "output": None, "cache_read": None, "cache_write": None}
    cost = calculate_attempt_cost(
        make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA),
        IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.amount_unrepresentable
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"

    # 唯一命中项自身缺适用桶单价 => pricing_unavailable，同样保留
    unit_prices = {"input": None, "output": "10", "cache_read": None, "cache_write": None}
    cost = calculate_attempt_cost(
        make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=NA, cache_write_tokens=NA),
        IDENTITY, make_resolution(unit_prices))
    assert cost.reason is UnknownReason.pricing_unavailable
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"

    # 全部桶声明不适用：无适用分项，防御分支保留身份
    unit_prices = {"input": "2", "output": "10", "cache_read": "0.2", "cache_write": "2.5"}
    cost = calculate_attempt_cost(
        make_usage(input_tokens=NA, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA),
        IDENTITY, make_resolution(unit_prices))
    assert cost.reason is UnknownReason.invalid_usage
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"

    # 合法条目但指数越界（DecimalException 防御分支）同样保留
    unit_prices = {"input": "1e1000000000", "output": None, "cache_read": None, "cache_write": None}
    cost = calculate_attempt_cost(
        make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA),
        IDENTITY, make_resolution(unit_prices))
    assert cost.reason is UnknownReason.amount_unrepresentable
    assert cost.pricing_version == PRICING_VERSION
    assert cost.price_id == "price-test-1"


def test_unknown_before_unique_resolution_has_no_price_id():
    # 未唯一定位价格项的 unknown 不保留 price_id（spec：否则省略）；
    # 版本在 Domain 层不携带，由组合根按目录加载状态统一补齐。
    usage = make_usage(input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=10)
    for status in ("not_found", "ambiguous", "unsupported", "unavailable"):
        cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(status=status))
        assert cost.status == "unknown"
        assert cost.price_id is None, status
        assert cost.pricing_version is None, status
    cost = calculate_attempt_cost(usage, None, make_resolution())
    assert cost.reason is UnknownReason.identity_unresolved
    assert cost.price_id is None
    assert cost.pricing_version is None


# ---------------------------------------------------------------------------
# cost_decimal 投影上限（用户评审修复 第五轮-nkb）：规范十进制字符串逐侧
# 340 位；超界金额无法在 Span/完成日志/Langfuse 完整投影（下游整组拒绝、
# 禁止静默丢字段），必须在 Domain 按 amount_unrepresentable 收口
# ---------------------------------------------------------------------------


def test_tiny_price_estimated_within_projection_bound():
    # 1e-70 单价 x 1 Token = 1e-76：有限 double 可表示，规范字符串
    # "0." + 75 个 0 + "1"（78 字符）在逐侧 340 位上限内，保持 estimated
    unit_prices = {"input": "1e-70", "output": None, "cache_read": None, "cache_write": None}
    usage = make_usage(input_tokens=1, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "estimated"
    expected = "0." + "0" * 75 + "1"
    assert format(cost.amount_usd.normalize(), "f") == expected
    assert len(expected) == 78


def test_over_bound_decimal_string_amount_unrepresentable():
    # 金额 3.0000000000000000003e-324：double 可表示（亚正常数取整在绝对
    # 容差 1e-12 内），但规范十进制字符串小数位 344 > 340，超出投影上限，
    # 按 amount_unrepresentable 收口并保留唯一命中价格身份
    unit_prices = {
        "input": "1.0000000000000000001e-318",
        "output": None,
        "cache_read": None,
        "cache_write": None,
    }
    usage = make_usage(input_tokens=3, output_tokens=NA, cache_read_tokens=NA, cache_write_tokens=NA)
    cost = calculate_attempt_cost(usage, IDENTITY, make_resolution(unit_prices))
    assert cost.status == "unknown"
    assert cost.reason is UnknownReason.amount_unrepresentable
    assert cost.amount_usd is None
    assert not cost.components
    assert cost.price_id == "price-test-1"
    assert cost.pricing_version == PRICING_VERSION
