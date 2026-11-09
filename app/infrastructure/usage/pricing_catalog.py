# app/infrastructure/usage/pricing_catalog.py
"""版本化价格目录（Infrastructure 层）。

职责：从磁盘加载 JSON 定价目录，按 RFC 8785（JCS）规范化后计算 SHA-256
内容哈希，与配置的预期哈希核对，并在内存中提供 resolve 匹配入口，产出
app.domain.cost.CatalogResolution。目录失效（未配置、文件缺失、JSON 非法、
schema 校验失败、哈希不符）一律降级为 CatalogResolution.unavailable，业务
继续、成本 unknown（pricing_unavailable），诊断以固定模板日志输出。

设计要点：
- 内容哈希基于规范化后的语义内容，与空白、键顺序和转义写法无关；
  pricing_version = content_version@content_sha256，content_version 字段与
  实际哈希共同构成版本标识。
- 键 (billing_provider, billing_model, operation) 与 aliases 均为大小写敏感
  精确匹配；conditions 只能引用服务端受信请求事实（cache_ttl、region），
  条件事实缺失优先 unsupported，条件已知但不满足才不命中。
- 加载阶段隔离：重复 price_id、同键同条件冲突（含 alias 展开后的键）、任一
  坏条目都使整个目录失效。
- 加载是启动期一次，resolve 是纯内存查询，不做 IO。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from app.domain.cost import (
    BUCKETS,
    CatalogResolution,
    CostInvariantError,
    PricingCatalogEntry,
)

logger = logging.getLogger(__name__)

# 固定诊断码集合（日志与调用方只消费这些码，不拼接自由文本）
_DIAGNOSTIC_CODES: tuple[str, ...] = (
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
)

# 目录 schema（schema_version 1）
_TOP_LEVEL_FIELDS: frozenset[str] = frozenset(
    {"schema_version", "content_version", "currency", "prices"}
)
_PRICE_FIELDS: frozenset[str] = frozenset(
    {
        "price_id",
        "billing_provider",
        "billing_model",
        "operation",
        "aliases",
        "unit_prices",
        "conditions",
        "source_url",
        "verified_at",
    }
)
_PRICE_REQUIRED_FIELDS: frozenset[str] = _PRICE_FIELDS - {"conditions"}
_OPERATIONS: tuple[str, ...] = ("chat", "embeddings")
# 各 operation 必须给出单价的适用桶；缺失视为坏条目，整个目录失效
_OPERATION_REQUIRED_BUCKETS: dict[str, tuple[str, ...]] = {
    "chat": ("input", "output"),
    "embeddings": ("input",),
}
# conditions 只能引用服务端受信请求事实（固定 schema）
_CONDITION_KEYS: frozenset[str] = frozenset({"cache_ttl", "region"})
_CONTENT_VERSION_MAX_LENGTH = 64

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class JcsCanonicalizationError(ValueError):
    """输入无法按 RFC 8785（JCS）规范化。

    code 取固定诊断码：lone_surrogate / non_finite_number /
    number_out_of_range / unsupported_type。
    """

    def __init__(self, code: str, description: str) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code


class _CatalogDataError(Exception):
    """目录数据非法，携带固定诊断码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# RFC 8785（JCS）最小实现
# ---------------------------------------------------------------------------

_SIMPLE_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _jcs_string(value: str) -> str:
    """按 JSON.stringify 转义规则序列化字符串。

    只转义双引号、反斜杠和 U+0000-U+001F 控制字符（优先短转义，其余用小写
    \\u00xx）；其余字符（含 U+007F、U+0080 与非 ASCII）原样输出，孤立代理项
    拒绝。
    """
    parts: list[str] = ['"']
    for char in value:
        code_point = ord(char)
        if 0xD800 <= code_point <= 0xDFFF:
            raise JcsCanonicalizationError("lone_surrogate", "字符串包含孤立代理项")
        escape = _SIMPLE_ESCAPES.get(char)
        if escape is not None:
            parts.append(escape)
        elif code_point < 0x20:
            parts.append("\\u%04x" % code_point)
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _es6_number_to_string(sign: int, digits: str, exp: int) -> str:
    """按 ES6 Number::toString 输出十进制文本。

    输入表示数值 (-1)^sign * 0.digits * 10^(exp + len(digits))，digits 为无
    前导零的十进制数字串。
    """
    ndigits = len(digits)
    n = exp + ndigits
    k = ndigits
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    elif k == 1:
        body = digits + "e" + ("+" if n - 1 > 0 else "-") + str(abs(n - 1))
    else:
        body = (
            digits[0]
            + "."
            + digits[1:]
            + "e"
            + ("+" if n - 1 > 0 else "-")
            + str(abs(n - 1))
        )
    return ("-" + body) if sign else body


def _jcs_number(value: Any) -> str:
    if isinstance(value, int):
        # I-JSON 要求整数在 IEEE-754 双精度安全范围内
        if abs(value) > 2 ** 53:
            raise JcsCanonicalizationError("number_out_of_range", "整数超出双精度安全范围")
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise JcsCanonicalizationError("non_finite_number", "非有限数不可规范化")
        if value == 0.0:
            # 含 -0.0，与 ES6 一致输出 "0"
            return "0"
        # repr 给出最短往返十进制，与 ES6 Number::toString 位数一致
        sign, digit_tuple, exp = Decimal(repr(value)).as_tuple()
        digits = "".join(str(d) for d in digit_tuple)
        while len(digits) > 1 and digits.endswith("0"):
            digits = digits[:-1]
            exp += 1
        return _es6_number_to_string(1 if sign else 0, digits, exp)
    raise JcsCanonicalizationError("unsupported_type", f"不可序列化类型: {type(value)!r}")


def _jcs_write(value: Any, parts: list[str]) -> None:
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, str):
        parts.append(_jcs_string(value))
    elif isinstance(value, (int, float)):
        parts.append(_jcs_number(value))
    elif isinstance(value, list):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _jcs_write(item, parts)
        parts.append("]")
    elif isinstance(value, dict):
        # 对象键按 UTF-16 code unit 升序排序：utf-16-be 字节序的字典序与
        # code unit 序一致，正确处理增补平面字符（代理项 0xD800-0xDFFF 小于
        # U+E000-U+FFFF 的 BMP 字符）
        keys = sorted(value.keys(), key=lambda key: key.encode("utf-16-be"))
        parts.append("{")
        for index, key in enumerate(keys):
            if index:
                parts.append(",")
            parts.append(_jcs_string(key))
            parts.append(":")
            _jcs_write(value[key], parts)
        parts.append("}")
    else:
        raise JcsCanonicalizationError("unsupported_type", f"不可序列化类型: {type(value)!r}")


def jcs_canonicalize(value: Any) -> str:
    """按 RFC 8785（JCS）把已解析的 JSON 数据规范化为字符串。

    输入须为 json.loads 的产物（dict/list/str/int/float/bool/None）；对象键
    按 UTF-16 code unit 排序，字符串按 JSON.stringify 转义，数字按 ES6
    Number::toString 输出。孤立代理项、非有限数与超界整数拒绝。
    """
    parts: list[str] = []
    _jcs_write(value, parts)
    return "".join(parts)


# ---------------------------------------------------------------------------
# 目录解析与校验
# ---------------------------------------------------------------------------


def _parse_json_strict(text: str) -> Any:
    """解析 JSON，拒绝重复键与非有限数（NaN/Infinity）。"""

    def _object_pairs(pairs: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise _CatalogDataError("duplicate_json_key")
            result[key] = value
        return result

    def _parse_constant(name: str) -> Any:
        raise _CatalogDataError("non_finite_number")

    try:
        return json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_parse_constant
        )
    except json.JSONDecodeError as error:
        raise _CatalogDataError("invalid_json") from error


def _build_entry(raw: Any) -> PricingCatalogEntry:
    """把一条 price JSON 对象转为 PricingCatalogEntry，非法时抛 _CatalogDataError。"""
    if not isinstance(raw, dict):
        raise _CatalogDataError("schema_invalid")
    fields = set(raw.keys())
    if not _PRICE_REQUIRED_FIELDS <= fields or not fields <= _PRICE_FIELDS:
        raise _CatalogDataError("schema_invalid")
    for label in ("price_id", "billing_provider", "billing_model", "source_url", "verified_at"):
        if not isinstance(raw[label], str):
            raise _CatalogDataError("schema_invalid")
    if raw["operation"] not in _OPERATIONS:
        raise _CatalogDataError("schema_invalid")
    aliases = raw["aliases"]
    if not isinstance(aliases, list) or not all(
        isinstance(alias, str) and alias for alias in aliases
    ):
        raise _CatalogDataError("schema_invalid")
    unit_prices = raw["unit_prices"]
    if not isinstance(unit_prices, dict):
        raise _CatalogDataError("schema_invalid")
    for bucket, price in unit_prices.items():
        if bucket not in BUCKETS:
            raise _CatalogDataError("schema_invalid")
        if price is not None and not isinstance(price, str):
            raise _CatalogDataError("schema_invalid")
    conditions = raw.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, dict):
            raise _CatalogDataError("schema_invalid")
        for key, value in conditions.items():
            if key not in _CONDITION_KEYS or not isinstance(value, str) or not value:
                raise _CatalogDataError("schema_invalid")
    try:
        return PricingCatalogEntry(
            price_id=raw["price_id"],
            billing_provider=raw["billing_provider"],
            billing_model=raw["billing_model"],
            operation=raw["operation"],
            aliases=tuple(aliases),
            conditions=dict(conditions) if conditions is not None else None,
            unit_prices=dict(unit_prices),
            source_url=raw["source_url"],
            verified_at=raw["verified_at"],
        )
    except CostInvariantError:
        # 单价非法（负数、符号前缀、非十进制等）：坏条目使整个目录失效
        raise _CatalogDataError("schema_invalid") from None


def _validate_document(document: Any) -> tuple[list[PricingCatalogEntry], str]:
    """校验顶层 schema 与全部条目，返回 (entries, content_version)。"""
    if not isinstance(document, dict) or set(document.keys()) != _TOP_LEVEL_FIELDS:
        raise _CatalogDataError("schema_invalid")
    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise _CatalogDataError("schema_invalid")
    if schema_version != 1:
        raise _CatalogDataError("schema_invalid")
    content_version = document["content_version"]
    if (
        not isinstance(content_version, str)
        or not content_version
        or len(content_version) > _CONTENT_VERSION_MAX_LENGTH
    ):
        raise _CatalogDataError("schema_invalid")
    if document["currency"] != "USD":
        raise _CatalogDataError("schema_invalid")
    prices = document["prices"]
    if not isinstance(prices, list):
        raise _CatalogDataError("schema_invalid")

    entries: list[PricingCatalogEntry] = []
    price_ids: set[str] = set()
    conflict_keys: set[tuple[tuple[str, str, str], tuple[tuple[str, str], ...]]] = set()
    for raw in prices:
        entry = _build_entry(raw)
        if entry.price_id in price_ids:
            raise _CatalogDataError("schema_invalid")
        price_ids.add(entry.price_id)
        # operation 必需的适用桶缺单价：坏条目
        for bucket in _OPERATION_REQUIRED_BUCKETS[entry.operation]:
            if entry.unit_prices.get(bucket) is None:
                raise _CatalogDataError("schema_invalid")
        # 同键同条件冲突检测：键为 (billing_provider, billing_model, operation)。
        # alias 与其他条目 billing_model 的重叠不在此隔离，留给运行期判
        # price_ambiguous（运行期 aliases 多命中才为 price_ambiguous）
        condition_items = tuple(sorted((entry.conditions or {}).items()))
        conflict_key = (
            (entry.billing_provider, entry.billing_model, entry.operation),
            condition_items,
        )
        if conflict_key in conflict_keys:
            raise _CatalogDataError("schema_invalid")
        conflict_keys.add(conflict_key)
        entries.append(entry)
    return entries, content_version


# ---------------------------------------------------------------------------
# 目录入口
# ---------------------------------------------------------------------------


class JsonPricingCatalog:
    """文件加载的版本化价格目录。

    通过 load 构造，永不抛异常：任何失效都以 available=False 呈现并携带固定
    诊断码 diagnostic；resolve 在失效目录上一律返回 unavailable。
    """

    def __init__(
        self,
        available: bool,
        diagnostic: str | None,
        entries: list[PricingCatalogEntry],
        content_version: str | None,
        content_sha256: str | None,
    ) -> None:
        self._available = available
        self._diagnostic = diagnostic
        self._entries = entries
        self._content_version = content_version
        self._content_sha256 = content_sha256

    @classmethod
    def _failed(
        cls, diagnostic: str, content_sha256: str | None = None
    ) -> "JsonPricingCatalog":
        # 唯一失效出口：诊断码必须落在固定枚举内，禁止拼接自由文本
        assert diagnostic in _DIAGNOSTIC_CODES, f"未知诊断码: {diagnostic}"
        return cls(
            available=False,
            diagnostic=diagnostic,
            entries=[],
            content_version=None,
            content_sha256=content_sha256,
        )

    @classmethod
    def load(cls, path: str | Path, expected_sha256: str) -> "JsonPricingCatalog":
        """加载并核对目录。失效时返回不可用实例并输出一次受控诊断日志。"""
        if path is None or str(path) == "":
            logger.error("pricing catalog unavailable: diagnostic=%s", "not_configured")
            return cls._failed("not_configured")
        if not expected_sha256:
            logger.error(
                "pricing catalog unavailable: diagnostic=%s", "expected_sha256_missing"
            )
            return cls._failed("expected_sha256_missing")
        if not _SHA256_PATTERN.fullmatch(expected_sha256):
            logger.error(
                "pricing catalog unavailable: diagnostic=%s", "expected_sha256_invalid"
            )
            return cls._failed("expected_sha256_invalid")
        try:
            raw = Path(path).read_bytes()
        except OSError:
            logger.error("pricing catalog unavailable: diagnostic=%s", "file_unreadable")
            return cls._failed("file_unreadable")
        try:
            document = _parse_json_strict(raw.decode("utf-8"))
            entries, content_version = _validate_document(document)
            canonical = jcs_canonicalize(document)
        except UnicodeDecodeError:
            logger.error("pricing catalog unavailable: diagnostic=%s", "invalid_json")
            return cls._failed("invalid_json")
        except RecursionError:
            # 病态深嵌套使 json 解析/校验/规范化递归越界，同样按 JSON 非法处理
            logger.error("pricing catalog unavailable: diagnostic=%s", "invalid_json")
            return cls._failed("invalid_json")
        except _CatalogDataError as error:
            logger.error("pricing catalog unavailable: diagnostic=%s", error.code)
            return cls._failed(error.code)
        except JcsCanonicalizationError as error:
            logger.error("pricing catalog unavailable: diagnostic=%s", error.code)
            return cls._failed(error.code)
        content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if content_sha256 != expected_sha256:
            logger.error("pricing catalog unavailable: diagnostic=%s", "hash_mismatch")
            return cls._failed("hash_mismatch", content_sha256=content_sha256)
        logger.info(
            "pricing catalog loaded: content_version=%s content_sha256=%s",
            content_version,
            content_sha256,
        )
        return cls(
            available=True,
            diagnostic=None,
            entries=entries,
            content_version=content_version,
            content_sha256=content_sha256,
        )

    @property
    def available(self) -> bool:
        return self._available

    @property
    def diagnostic(self) -> str | None:
        return self._diagnostic

    @property
    def content_version(self) -> str | None:
        return self._content_version

    @property
    def content_sha256(self) -> str | None:
        return self._content_sha256

    @property
    def pricing_version(self) -> str | None:
        """content_version 与实际哈希共同构成的版本标识。"""
        if not self._available or self._content_version is None:
            return None
        return f"{self._content_version}@{self._content_sha256}"

    def resolve(
        self,
        identity: tuple[str, str],
        operation: str,
        facts: Mapping[str, str] | None = None,
    ) -> CatalogResolution:
        """按 (billing_provider, billing_model 或 alias, operation) 精确匹配。

        conditions 按 facts 判定：条件事实缺失返回 unsupported（优先，不得
        误判为零匹配）；条件已知但不满足的条目不命中；多命中返回 ambiguous，
        零命中返回 not_found。纯内存查询，不做 IO。
        """
        if not self._available:
            return CatalogResolution.unavailable()
        if identity is None:
            raise ValueError("identity 必须提供 (billing_provider, billing_model)")
        provider, model = identity
        candidates = [
            entry
            for entry in self._entries
            if entry.billing_provider == provider
            and entry.operation == operation
            and (entry.billing_model == model or model in entry.aliases)
        ]
        fact_map = dict(facts) if facts is not None else {}
        matched: list[PricingCatalogEntry] = []
        for entry in candidates:
            undecidable = False
            satisfied = True
            for condition_key, condition_value in (entry.conditions or {}).items():
                fact = fact_map.get(condition_key)
                if fact is None:
                    undecidable = True
                elif fact != condition_value:
                    satisfied = False
            if undecidable:
                # 条件事实缺失优先 unsupported，不得跳过该候选后误报零匹配。
                # 注意刻意保守的读法：同键下无条件条目本可单独命中，但只要任一
                # 同键候选条件不可判定，整体仍返回 unsupported（事实齐全时同键
                # 多候选则正常返回 ambiguous）；T6 镜像实现不得偏离
                return CatalogResolution.unsupported(self.pricing_version)
            if satisfied:
                matched.append(entry)
        if not matched:
            return CatalogResolution.not_found(self.pricing_version)
        if len(matched) > 1:
            return CatalogResolution.ambiguous(
                tuple(sorted(entry.price_id for entry in matched)), self.pricing_version
            )
        return CatalogResolution.unique(matched[0], self.pricing_version)
