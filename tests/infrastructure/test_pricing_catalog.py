# tests/infrastructure/test_pricing_catalog.py
"""版本化价格目录测试。

覆盖三块：
1. JCS（RFC 8785）规范化：官方测试数据向量（cyberphone/json-canonicalization
   testdata，与 RFC 8785 Appendix B 同源）加 ES6 数字样例，独立于实现验证。
2. 目录加载：schema 校验、重复键、非有限数、未知字段、哈希核对、失效降级。
3. resolve 匹配语义：精确键、aliases、conditions、ambiguous/not_found/unsupported。
"""
import hashlib
import json
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.cost import PricingCatalogEntry
from app.infrastructure.usage.pricing_catalog import (
    _DIAGNOSTIC_CODES,
    JcsCanonicalizationError,
    JsonPricingCatalog,
    jcs_canonicalize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_CATALOG_PATH = REPO_ROOT / "docker" / "observability" / "model-pricing.json"
# 锁定当前目录内容的 JCS 内容哈希（内容变更时必须同步更新）
SHIPPED_CATALOG_SHA256 = (
    "3f31783a1b457ce213b8482a23243dbfd00e16e8ed7ff9086ca600d2be1ec19f"
)
SHIPPED_CONTENT_VERSION = "2026-11-10-peak-fx7-v1"

# RFC 8785 官方测试数据：(名称, 输入 JSON 文本, 期望规范化输出文本)
JCS_VECTORS = [
    (
        "arrays",
        r"""[
  56,
  {
    "d": true,
    "10": null,
    "1": [ ]
  }
]""",
        "[56,{\"1\":[],\"10\":null,\"d\":true}]",
    ),
    (
        "french",
        r"""{
  "peach": "This sorting order",
  "péché": "is wrong according to French",
  "pêche": "but canonicalization MUST",
  "sin":   "ignore locale"
}""",
        "{\"peach\":\"This sorting order\","
        "\"péché\":\"is wrong according to French\","
        "\"pêche\":\"but canonicalization MUST\","
        "\"sin\":\"ignore locale\"}",
    ),
    (
        "structures",
        r"""{
  "1": {"f": {"f": "hi","F": 5} ,"\n": 56.0},
  "10": { },
  "": "empty",
  "a": { },
  "111": [ {"e": "yes","E": "no" } ],
  "A": { }
}""",
        "{\"\":\"empty\",\"1\":{\"\\n\":56,\"f\":{\"F\":5,\"f\":\"hi\"}},"
        "\"10\":{},\"111\":[{\"E\":\"no\",\"e\":\"yes\"}],\"A\":{},\"a\":{}}",
    ),
    (
        "unicode",
        r"""{
  "Unnormalized Unicode":"A\u030a"
}""",
        "{\"Unnormalized Unicode\":\"A\u030a\"}",
    ),
    (
        "values",
        r"""{
  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],
  "string": "\u20ac$\u000F\u000aA'\u0042\u0022\u005c\\\"\/",
  "literals": [null, true, false]
}""",
        r"""{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\u000f\nA'B\"\\\\\"/"}""",
    ),
    (
        "weird",
        r"""{
  "\u20ac": "Euro Sign",
  "\r": "Carriage Return",
  "\u000a": "Newline",
  "1": "One",
  "\u0080": "Control\u007f",
  "\ud83d\ude02": "Smiley",
  "\u00f6": "Latin Small Letter O With Diaeresis",
  "\ufb33": "Hebrew Letter Dalet With Dagesh",
  "</script>": "Browser Challenge"
}""",
        "{\"\\n\":\"Newline\",\"\\r\":\"Carriage Return\",\"1\":\"One\","
        "\"</script>\":\"Browser Challenge\","
        "\"\u0080\":\"Control\u007f\","
        "\"ö\":\"Latin Small Letter O With Diaeresis\","
        "\"€\":\"Euro Sign\","
        "\"\U0001F602\":\"Smiley\","
        "\"\ufb33\":\"Hebrew Letter Dalet With Dagesh\"}",
    ),
]

# ES6 数字序列化官方样例（cyberphone testdata README 的 numgen 样例行）：
# (IEEE-754 双精度十六进制位型, 期望序列化文本)
ES6_NUMBER_SAMPLES = [
    ("4340000000000001", "9007199254740994"),
    ("4340000000000002", "9007199254740996"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("8000000000000000", "0"),
    ("0", "0"),
]


@pytest.mark.parametrize("name,source,expected", JCS_VECTORS, ids=[v[0] for v in JCS_VECTORS])
def test_jcs_official_vectors(name, source, expected):
    assert jcs_canonicalize(json.loads(source)) == expected


@pytest.mark.parametrize("hex_bits,expected", ES6_NUMBER_SAMPLES, ids=[s[0] for s in ES6_NUMBER_SAMPLES])
def test_jcs_es6_number_samples(hex_bits, expected):
    value = struct.unpack(">d", bytes.fromhex(hex_bits.zfill(16)))[0]
    assert jcs_canonicalize(value) == expected


def test_jcs_rejects_lone_surrogate():
    with pytest.raises(JcsCanonicalizationError):
        jcs_canonicalize(json.loads(r'"\ud800"'))


def _content_sha(text: str) -> str:
    canonical = jcs_canonicalize(json.loads(text))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_catalog(tmp_path: Path, text: str, name: str = "catalog.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _load(path, expected_sha256: str | None = None) -> JsonPricingCatalog:
    if expected_sha256 is None:
        expected_sha256 = _content_sha(Path(path).read_text(encoding="utf-8"))
    return JsonPricingCatalog.load(path, expected_sha256)


def _entry(**overrides) -> dict:
    entry = {
        "price_id": "p1",
        "billing_provider": "openai",
        "billing_model": "gpt-test",
        "operation": "chat",
        "aliases": [],
        "unit_prices": {"input": "1.00", "output": "2.00"},
        "source_url": "https://example.com/pricing",
        "verified_at": "2026-11-09",
    }
    entry.update(overrides)
    return entry


def _catalog_text(prices: list[dict], content_version: str = "test-v1") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "content_version": content_version,
            "currency": "USD",
            "prices": prices,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 随仓库发布的当前目录
# ---------------------------------------------------------------------------


def test_shipped_catalog_loads_with_locked_hash():
    catalog = JsonPricingCatalog.load(str(SHIPPED_CATALOG_PATH), SHIPPED_CATALOG_SHA256)
    assert catalog.available is True
    assert catalog.diagnostic is None
    assert catalog.content_sha256 == SHIPPED_CATALOG_SHA256
    assert catalog.content_version == SHIPPED_CONTENT_VERSION
    assert catalog.pricing_version == f"{SHIPPED_CONTENT_VERSION}@{SHIPPED_CATALOG_SHA256}"


def test_shipped_catalog_content_hash_ignores_formatting(tmp_path: Path):
    raw = SHIPPED_CATALOG_PATH.read_text(encoding="utf-8")
    document = json.loads(raw)
    pretty = tmp_path / "pretty.json"
    pretty.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    catalog = _load(pretty)
    assert catalog.available is True
    assert catalog.content_sha256 == SHIPPED_CATALOG_SHA256


def test_shipped_catalog_does_not_guess_unconfigured_commercial_prices():
    catalog = _load(SHIPPED_CATALOG_PATH)
    resolution = catalog.resolve(("openai", "gpt-4o"), "chat")
    assert resolution.status == "not_found"
    assert resolution.pricing_version == catalog.pricing_version


def test_shipped_catalog_prices_local_embedding_and_configured_chat_models():
    catalog = _load(SHIPPED_CATALOG_PATH)
    free = catalog.resolve(("ollama", "bge-m3"), "embeddings")
    assert free.status == "unique"
    assert free.entry.unit_prices["input"] == "0"
    alias = catalog.resolve(("ollama", "bge-m3:latest"), "embeddings")
    assert alias.price_id == free.price_id
    for identity in (
        ("deepseek", "deepseek-v4-flash"),
        ("volcengine", "deepseek-v4-1-flash-261017"),
        ("volcengine", "ark-code-latest"),
    ):
        resolution = catalog.resolve(identity, "chat")
        assert resolution.status == "unique"
        assert resolution.entry.unit_prices == {
            "input": "0.2857142857142857142857142857",
            "output": "1.142857142857142857142857143",
            "cache_read": "0.005714285714285714285714285714",
        }


# ---------------------------------------------------------------------------
# 加载失效降级：任何失效都返回 available=False，携带固定诊断码
# ---------------------------------------------------------------------------


def test_load_without_configured_path():
    catalog = JsonPricingCatalog.load("", SHIPPED_CATALOG_SHA256)
    assert catalog.available is False
    assert catalog.diagnostic == "not_configured"


def test_load_without_expected_sha256(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([]))
    catalog = JsonPricingCatalog.load(str(path), "")
    assert catalog.available is False
    assert catalog.diagnostic == "expected_sha256_missing"


def test_load_with_malformed_expected_sha256(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([]))
    for bad in ("A" * 64, "0" * 63, "g" * 64):
        catalog = JsonPricingCatalog.load(str(path), bad)
        assert catalog.available is False
        assert catalog.diagnostic == "expected_sha256_invalid"


def test_load_missing_file(tmp_path: Path):
    catalog = JsonPricingCatalog.load(str(tmp_path / "absent.json"), SHIPPED_CATALOG_SHA256)
    assert catalog.available is False
    assert catalog.diagnostic == "file_unreadable"


def test_load_invalid_json(tmp_path: Path):
    path = _write_catalog(tmp_path, "{not json")
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "invalid_json"


def test_load_rejects_duplicate_json_key(tmp_path: Path):
    text = (
        '{"schema_version":1,"content_version":"test-v1","currency":"USD",'
        '"prices":[],"content_version":"test-v1"}'
    )
    path = _write_catalog(tmp_path, text)
    catalog = _load(path)
    assert catalog.available is False
    assert catalog.diagnostic == "duplicate_json_key"


def test_load_rejects_non_finite_number(tmp_path: Path):
    text = (
        '{"schema_version":1,"content_version":"test-v1","currency":"USD",'
        '"prices":[],"extra":NaN}'
    )
    path = _write_catalog(tmp_path, text)
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "non_finite_number"


def test_load_rejects_lone_surrogate_in_content(tmp_path: Path):
    # 孤立代理项放在 schema 合法的 content_version 内，确保到达规范化阶段
    text = (
        '{"schema_version":1,"content_version":"\\ud800","currency":"USD",'
        '"prices":[]}'
    )
    path = _write_catalog(tmp_path, text)
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "lone_surrogate"


def test_load_hash_mismatch(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([]))
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "hash_mismatch"


# ---------------------------------------------------------------------------
# schema 校验：未知字段、非法版本、非 USD、坏条目都使整个目录失效
# ---------------------------------------------------------------------------


def _assert_schema_invalid(tmp_path: Path, prices: list[dict], **overrides) -> None:
    document = {
        "schema_version": 1,
        "content_version": "test-v1",
        "currency": "USD",
        "prices": prices,
    }
    document.update(overrides)
    path = _write_catalog(tmp_path, json.dumps(document, ensure_ascii=False))
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "schema_invalid"


def test_schema_rejects_unknown_top_level_field(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [], extra_field="x")


def test_schema_rejects_unknown_price_field(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(discount="0.5")])


def test_schema_rejects_missing_required_price_field(tmp_path: Path):
    entry = _entry()
    del entry["verified_at"]
    _assert_schema_invalid(tmp_path, [entry])


def test_schema_rejects_non_usd_currency(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [], currency="EUR")


def test_schema_rejects_wrong_schema_version(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [], schema_version=2)


def test_schema_rejects_duplicate_price_id(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(), _entry(price_id="p1", billing_model="other")])


def test_schema_rejects_same_key_same_conditions_conflict(tmp_path: Path):
    _assert_schema_invalid(
        tmp_path,
        [
            _entry(conditions={"cache_ttl": "5m"}),
            _entry(price_id="p2", conditions={"cache_ttl": "5m"}),
        ],
    )


def test_schema_allows_same_key_different_conditions(tmp_path: Path):
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(conditions={"cache_ttl": "5m"}),
                _entry(price_id="p2", conditions={"cache_ttl": "1h"}),
            ]
        ),
    )
    catalog = _load(path)
    assert catalog.available is True


def test_schema_allows_alias_overlapping_other_entry_model(tmp_path: Path):
    # 条目 B 的 alias 与条目 A 的 billing_model 重叠不是加载期冲突，
    # 运行期解析为 price_ambiguous（见 test_resolve_ambiguous_on_alias_multi_hit）
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(),
                _entry(price_id="p2", billing_model="other", aliases=["gpt-test"]),
            ]
        ),
    )
    catalog = _load(path)
    assert catalog.available is True


def test_schema_rejects_invalid_price_string(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(unit_prices={"input": "-1", "output": "2.00"})])


def test_schema_rejects_chat_entry_missing_output_price(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(unit_prices={"input": "1.00"})])


def test_schema_rejects_condition_key_outside_vocabulary(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(conditions={"user_id": "u1"})])


def test_schema_rejects_non_string_condition_value(tmp_path: Path):
    _assert_schema_invalid(tmp_path, [_entry(conditions={"region": 1})])


def test_load_rejects_pathologically_deep_nesting(tmp_path: Path):
    # 病态深嵌套触发解析递归越界：必须降级为 invalid_json，不得抛出
    path = _write_catalog(tmp_path, "[" * 50000 + "]" * 50000)
    catalog = JsonPricingCatalog.load(str(path), "0" * 64)
    assert catalog.available is False
    assert catalog.diagnostic == "invalid_json"


def test_all_load_diagnostics_inside_fixed_enum(tmp_path: Path):
    # load 的全部失效出口都必须落在固定诊断码枚举内
    text = _catalog_text([])
    path = _write_catalog(tmp_path, text)
    duplicate_key = _write_catalog(
        tmp_path,
        '{"schema_version":1,"content_version":"v","currency":"USD",'
        '"prices":[],"content_version":"v"}',
        name="dup.json",
    )
    surrogate = _write_catalog(
        tmp_path,
        '{"schema_version":1,"content_version":"\\ud800","currency":"USD",'
        '"prices":[]}',
        name="surrogate.json",
    )
    schema_bad = _write_catalog(tmp_path, _catalog_text([_entry(unit_prices={})]), name="bad.json")
    catalogs = [
        JsonPricingCatalog.load("", "0" * 64),
        JsonPricingCatalog.load(str(path), ""),
        JsonPricingCatalog.load(str(path), "A" * 64),
        JsonPricingCatalog.load(str(tmp_path / "absent.json"), "0" * 64),
        JsonPricingCatalog.load(str(_write_catalog(tmp_path, "{oops", name="broken.json")), "0" * 64),
        JsonPricingCatalog.load(str(duplicate_key), "0" * 64),
        JsonPricingCatalog.load(
            str(_write_catalog(tmp_path, '{"schema_version":1,"prices":[],"x":NaN}', name="nan.json")),
            "0" * 64,
        ),
        JsonPricingCatalog.load(str(surrogate), "0" * 64),
        JsonPricingCatalog.load(str(schema_bad), "0" * 64),
        JsonPricingCatalog.load(str(path), "0" * 64),
    ]
    expected_codes = [
        "not_configured",
        "expected_sha256_missing",
        "expected_sha256_invalid",
        "file_unreadable",
        "invalid_json",
        "duplicate_json_key",
        "non_finite_number",
        "lone_surrogate",
        "schema_invalid",
        "hash_mismatch",
    ]
    assert len(_DIAGNOSTIC_CODES) == len(expected_codes)
    for catalog, code in zip(catalogs, expected_codes):
        assert catalog.available is False
        assert catalog.diagnostic == code
        assert catalog.diagnostic in _DIAGNOSTIC_CODES


# ---------------------------------------------------------------------------
# resolve 匹配语义
# ---------------------------------------------------------------------------


def _conditional_catalog(tmp_path: Path) -> JsonPricingCatalog:
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(price_id="p-cond", conditions={"cache_ttl": "5m"}),
            ]
        ),
    )
    return _load(path)


def test_resolve_unique_by_exact_key(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([_entry()]))
    catalog = _load(path)
    resolution = catalog.resolve(("openai", "gpt-test"), "chat")
    assert resolution.status == "unique"
    assert resolution.price_id == "p1"
    assert resolution.pricing_version == catalog.pricing_version
    entry = resolution.entry
    assert isinstance(entry, PricingCatalogEntry)
    assert entry.billing_provider == "openai"
    assert entry.unit_prices["input"] == "1.00"


def test_resolve_unique_via_alias(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([_entry(aliases=["gpt-test-2024"])]))
    catalog = _load(path)
    resolution = catalog.resolve(("openai", "gpt-test-2024"), "chat")
    assert resolution.status == "unique"
    assert resolution.price_id == "p1"


def test_resolve_is_case_sensitive(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([_entry()]))
    catalog = _load(path)
    assert catalog.resolve(("openai", "GPT-Test"), "chat").status == "not_found"
    assert catalog.resolve(("OpenAI", "gpt-test"), "chat").status == "not_found"


def test_resolve_alias_requires_exact_match(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([_entry(aliases=["gpt-test-2024"])]))
    catalog = _load(path)
    assert catalog.resolve(("openai", "gpt-test-2024-extra"), "chat").status == "not_found"


def test_resolve_ambiguous_on_alias_multi_hit(tmp_path: Path):
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(price_id="p1"),
                _entry(price_id="p2", billing_model="other", aliases=["gpt-test"]),
            ]
        ),
    )
    catalog = _load(path)
    resolution = catalog.resolve(("openai", "gpt-test"), "chat")
    assert resolution.status == "ambiguous"
    assert set(resolution.matched_price_ids) == {"p1", "p2"}


def test_resolve_not_found_on_zero_hit(tmp_path: Path):
    path = _write_catalog(tmp_path, _catalog_text([_entry()]))
    catalog = _load(path)
    assert catalog.resolve(("openai", "absent-model"), "chat").status == "not_found"
    assert catalog.resolve(("openai", "gpt-test"), "embeddings").status == "not_found"


def test_resolve_condition_fact_missing_is_unsupported(tmp_path: Path):
    catalog = _conditional_catalog(tmp_path)
    resolution = catalog.resolve(("openai", "gpt-test"), "chat", facts={})
    assert resolution.status == "unsupported"


def test_resolve_condition_fact_known_but_unsatisfied_is_not_found(tmp_path: Path):
    catalog = _conditional_catalog(tmp_path)
    resolution = catalog.resolve(
        ("openai", "gpt-test"), "chat", facts={"cache_ttl": "1h"}
    )
    assert resolution.status == "not_found"


def test_resolve_condition_satisfied_is_unique(tmp_path: Path):
    catalog = _conditional_catalog(tmp_path)
    resolution = catalog.resolve(
        ("openai", "gpt-test"), "chat", facts={"cache_ttl": "5m"}
    )
    assert resolution.status == "unique"
    assert resolution.price_id == "p-cond"


def test_resolve_fact_missing_never_collapses_to_zero_match(tmp_path: Path):
    # 两个条目同键不同条件：一个条件可判定且满足，另一个条件事实缺失。
    # 优先 unsupported，不得误判为零匹配
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(price_id="p-a", conditions={"cache_ttl": "5m"}),
                _entry(price_id="p-b", conditions={"region": "us"}),
            ]
        ),
    )
    catalog = _load(path)
    resolution = catalog.resolve(("openai", "gpt-test"), "chat", facts={"cache_ttl": "5m"})
    assert resolution.status == "unsupported"


def test_resolve_unconditional_plus_conditional_same_key(tmp_path: Path):
    # 锁定刻意保守的读法，防止 T6 镜像实现偏离：
    # 同键下无条件条目与条件条目并存时，条件事实缺失返回 unsupported
    # （尽管无条件条目本可单独命中）；事实齐全时两者同中返回 ambiguous
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(price_id="p-free"),
                _entry(price_id="p-cond", conditions={"cache_ttl": "5m"}),
            ]
        ),
    )
    catalog = _load(path)
    missing = catalog.resolve(("openai", "gpt-test"), "chat", facts={})
    assert missing.status == "unsupported"
    satisfied = catalog.resolve(
        ("openai", "gpt-test"), "chat", facts={"cache_ttl": "5m"}
    )
    assert satisfied.status == "ambiguous"
    assert set(satisfied.matched_price_ids) == {"p-free", "p-cond"}


def test_resolve_on_unavailable_catalog(tmp_path: Path):
    catalog = JsonPricingCatalog.load(str(tmp_path / "absent.json"), "0" * 64)
    resolution = catalog.resolve(("openai", "gpt-test"), "chat")
    assert resolution.status == "unavailable"
    assert resolution.entry is None


def test_resolve_embeddings_entry(tmp_path: Path):
    path = _write_catalog(
        tmp_path,
        _catalog_text(
            [
                _entry(
                    price_id="p-embed",
                    operation="embeddings",
                    unit_prices={"input": "0.01"},
                )
            ]
        ),
    )
    catalog = _load(path)
    resolution = catalog.resolve(("openai", "gpt-test"), "embeddings")
    assert resolution.status == "unique"
    assert resolution.price_id == "p-embed"


# ---------------------------------------------------------------------------
# config：B 类 env-only 字段
# ---------------------------------------------------------------------------


def test_settings_pricing_catalog_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("N_AGENT_PRICING_CATALOG_PATH", raising=False)
    monkeypatch.delenv("N_AGENT_PRICING_CATALOG_EXPECTED_SHA256", raising=False)
    settings = Settings()
    assert settings.pricing_catalog_path == ""
    assert settings.pricing_catalog_expected_sha256 == ""


def test_settings_pricing_catalog_env_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("N_AGENT_PRICING_CATALOG_PATH", str(tmp_path / "catalog.json"))
    monkeypatch.setenv("N_AGENT_PRICING_CATALOG_EXPECTED_SHA256", "a" * 64)
    settings = Settings()
    assert settings.pricing_catalog_path == str(tmp_path / "catalog.json")
    assert settings.pricing_catalog_expected_sha256 == "a" * 64


def test_settings_rejects_malformed_expected_sha256():
    with pytest.raises(ValidationError):
        Settings(pricing_catalog_expected_sha256="A" * 64)
    with pytest.raises(ValidationError):
        Settings(pricing_catalog_expected_sha256="0" * 63)
