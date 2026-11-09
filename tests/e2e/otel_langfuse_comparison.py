"""Docker E2E probe plus stdlib reconciliation of fixed backend evidence."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, urlopen
from urllib.parse import quote, urlencode


def _post(url, data, timeout=120):
    body = json.dumps(data).encode()
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, data=body, headers={"Content-Type": "application/json"},
                             method="POST"), timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


COUNTERS = (
    "nagent_agent_runs_total", "gen_ai_invoke_agent_inference_calls_sum",
    "gen_ai_invoke_agent_tool_calls_sum", "gen_ai_client_inference_usage_input_tokens_total",
    "gen_ai_client_inference_usage_output_tokens_total", "nagent_model_cache_read_tokens_total",
    "nagent_model_cache_write_tokens_total", "nagent_model_normalized_tokens_total",
    "nagent_model_cache_eligible_prompt_tokens_total", "nagent_model_cache_eligible_read_tokens_total",
)
# 具名 Counter 索引：位置取值易随登记顺序漂移，统一以名称引用。
AGENT_RUNS_COUNTER, INFERENCE_CALLS_COUNTER, TOOL_CALLS_COUNTER = COUNTERS[:3]
# 四桶用量总量 Counter，与 usageDetails 桶一一对应（input/output/cache_read/cache_write）。
TOKEN_BUCKET_COUNTERS = COUNTERS[3:7]
(NORMALIZED_TOKENS_TOTAL_COUNTER, CACHE_PROMPT_ELIGIBLE_TOTAL_COUNTER,
 CACHE_READ_ELIGIBLE_TOTAL_COUNTER) = COUNTERS[7:]
HISTOGRAMS = (
    "gen_ai_invoke_agent_duration_seconds", "gen_ai_client_inference_duration_seconds",
    "gen_ai_execute_tool_duration_seconds",
)
CASE_ID = "langfuse-comparison"

# T8 成本对比常量（spec: OTel Data Model / Langfuse Projection / Metric 对账）。
COST_REASONS = frozenset((
    "missing_usage", "invalid_usage", "price_not_found", "price_ambiguous",
    "unsupported_pricing", "identity_unresolved", "pricing_unavailable",
    "amount_unrepresentable"))
COST_BUCKETS = ("input", "output", "cache_read", "cache_write")
COST_AMOUNT_KEYS = (
    "nagent.model.cost_usd", "nagent.model.cost_decimal",
    "nagent.model.cost_input_usd", "nagent.model.cost_output_usd",
    "nagent.model.cost_cache_read_usd", "nagent.model.cost_cache_write_usd")
KB_AMOUNT_KEYS = tuple(key.replace("nagent.", "nkb.", 1) for key in COST_AMOUNT_KEYS)
# Counter 实测名称以 docker/observability/metric-name-mapping.md 登记为准；
# USD 单位实测产生字面 `_USD` 后缀，{attempt} 不产生后缀。
AGENT_COST_COUNTERS = ("nagent_model_cost_USD_total",
                       "nagent_model_cost_components_USD_total",
                       "nagent_model_cost_attempts_total",
                       "nagent_model_cost_component")
KB_COST_COUNTERS = ("nkb_model_cost_USD_total",
                    "nkb_model_cost_components_USD_total",
                    "nkb_model_cost_attempts_total",
                    "nkb_model_cost_component")
E2E_PRICING_CONTENT_VERSION = "e2e-test-261109"
E2E_PRICING_SHA256 = "1536ca56d137af4f903d3ca43b589e986cfddfb3395a2e0a357d537bdb40c758"
# unknown 样例：请求模型不在受信计价身份映射内 => identity_unresolved，
# 确定性触发 unknown（阻断自动推价的正向证据）。
UNKNOWN_PROBE_MODEL = "cost-unknown-model"


def load_support(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scalar(rows, name):
    values = [float(row["value"][1]) for row in rows if row["metric"]["__name__"] == name]
    return sum(values) if values else None


def buckets(rows, name):
    values = {}
    for row in rows:
        if row["metric"]["__name__"] == name + "_bucket":
            le = float(row["metric"]["le"])
            values[le] = values.get(le, 0) + float(row["value"][1])
    return values


def seconds(row):
    start, end = (datetime.fromisoformat(row[key].replace("Z", "+00:00"))
                  for key in ("startTime", "endTime"))
    return (end - start).total_seconds()


def verdict(checks):
    results = {row["result"] for row in checks}
    return "FAIL" if "FAIL" in results else "SKIP" if not checks or "SKIP" in results else "PASS"


def compare_metrics(before, after, observations, tempo_span_count):
    """No extrapolation: compare raw counter differences for exactly one instance."""
    checks = []

    def delta(name):
        end = scalar(after, name)
        return None if end is None else end - (scalar(before, name) or 0)

    def check(name, actual, expected, tolerance=0, note=""):
        result = ("SKIP" if expected is None else "FAIL" if actual is None or
                  not math.isfinite(actual) or abs(actual - expected) > tolerance else "PASS")
        checks.append({"metric": name, "otel": actual, "langfuse": expected, "result": result,
                       "absolute_error": None if actual is None or expected is None else abs(actual - expected),
                       "tolerance": tolerance, "note": note or ("缺失指标，不能补零" if actual is None else
                                                                "未知用量，不能补零" if expected is None else
                                                                f"绝对容差 {tolerance:g}")})

    agents = [r for r in observations if r["type"] == "AGENT"]
    generations = [r for r in observations if r["type"] == "GENERATION"]
    tools = [r for r in observations if r["type"] == "TOOL"]
    usage = [r.get("usageDetails") or {} for r in generations]
    check("Agent 运行数", delta(AGENT_RUNS_COUNTER), len(agents))
    check("模型调用数", delta(INFERENCE_CALLS_COUNTER), len(generations))
    check("工具调用数", delta(TOOL_CALLS_COUNTER), len(tools))
    for title, field in (("输入", "input"), ("输出", "output")):
        name = "gen_ai_client_inference_operation_" + field + "_tokens"
        known = [u[field] for u in usage if field in u]
        check(title + "Token直方图样本数", delta(name + "_count"), len(known))
        check(title + "Token直方图总和", delta(name + "_sum"), sum(known))
    for field, name in zip(("input", "output", "cache_read", "cache_write"), TOKEN_BUCKET_COUNTERS):
        expected = sum(u[field] for u in usage) if usage and all(field in u for u in usage) else None
        check(field + " tokens", delta(name), expected)
    deltas = [delta(name) for name in TOKEN_BUCKET_COUNTERS]
    check("total tokens（input+output+cache read+cache write）",
          sum(deltas) if all(n is not None for n in deltas) else None,
          sum(u["total"] for u in usage) if usage and all("total" in u for u in usage) else None)
    known = [u for u in usage if all(k in u for k in ("input", "cache_read", "output"))]
    check("normalized tokens（逐调用取整）", delta(NORMALIZED_TOKENS_TOTAL_COUNTER),
          sum(round(u["input"] + .2 * u["cache_read"] + 5 * u["output"]) for u in known)
          if known and len(known) == len(usage) else None)
    eligible = []
    for row, u in zip(generations, usage):
        provider = (row.get("metadata") or {}).get("gen_ai.provider.name")
        if provider in ("openai", "openai-compatible"):
            if all(k in u for k in ("input", "cache_read")) and u.get("cache_write", 0) == 0:
                eligible.append((u["input"] + u["cache_read"], u["cache_read"]))
        elif all(k in u for k in ("input", "cache_read", "cache_write")):
            eligible.append((u["input"] + u["cache_read"] + u["cache_write"], u["cache_read"]))
    prompt = sum(u[0] for u in eligible) if eligible and len(eligible) == len(usage) else None
    read = sum(u[1] for u in eligible) if prompt is not None else None
    check("cache eligible prompt tokens", delta(CACHE_PROMPT_ELIGIBLE_TOTAL_COUNTER), prompt)
    check("cache eligible read tokens", delta(CACHE_READ_ELIGIBLE_TOTAL_COUNTER), read)
    denominator, numerator = (delta(CACHE_PROMPT_ELIGIBLE_TOTAL_COUNTER),
                              delta(CACHE_READ_ELIGIBLE_TOTAL_COUNTER))
    rate = numerator / denominator if denominator and numerator is not None else None
    check("缓存命中率", rate, read / prompt if prompt else None, 1e-10,
          "已知缓存读 / 可判定prompt；缺失计数器不按零流量处理")
    for title, name, group in zip(("Agent", "模型", "工具"), HISTOGRAMS, (agents, generations, tools)):
        if title == "模型":
            # 推理耗时直方图由 Provider 调用钩子计时；Span 墙钟包含 _complete
            # 收尾（含 T8 计价计算），口径不同。同口径核对使用 metadata 投影的
            # nagent.model.duration_ms（与直方图同一测量值，毫秒）。
            durations = []
            for row in group:
                value = (row.get("metadata") or {}).get("nagent.model.duration_ms")
                if not isinstance(value, (int, float)) or isinstance(value, bool) \
                        or not math.isfinite(value) or value < 0:
                    durations = None
                    break
                durations.append(value / 1000)
            sum_note = ("同口径核对：OTel直方图与metadata duration_ms同为Provider调用钩子测量"
                        "（同一OTLP流两管线）；Langfuse Span墙钟含_complete收尾与计价计算，"
                        "系统性偏大（真机实测每样本约12-22ms），不作为对账口径")
            p95_note = "同口径（duration_ms投影）；OTel桶内线性插值；Langfuse样本最近秩；"
        else:
            durations = [seconds(row) for row in group]
            sum_note = ("不同测量边界对账：OTel直方图（Provider调用钩子）与Langfuse Span"
                        "墙钟为不同测量边界，按既有容差（每样本5ms或1%）核对")
            p95_note = "Langfuse样本最近秩（Span墙钟）；OTel桶内线性插值；"
        count, total = delta(name + "_count"), delta(name + "_sum")
        if durations is None:
            note = "Generation 缺少 nagent.model.duration_ms 投影，无法同口径核对耗时"
            check(title + "耗时样本数", count, None, 0, note)
            check(title + "耗时总和(s)", total, None, 0, note)
            check(title + "平均耗时(s)", None, None, 0, note)
            checks.append({"metric": title + " p95(s)", "otel": None, "langfuse": None,
                           "result": "SKIP", "absolute_error": None, "note": note})
            continue
        check(title + "耗时样本数", count, len(durations))
        check(title + "耗时总和(s)", total, sum(durations), max(.005 * len(durations), sum(durations) * .01),
              sum_note)
        check(title + "平均耗时(s)", total / count if count and total is not None else None,
              sum(durations) / len(durations) if durations else None,
              max(.005, sum(durations) / len(durations) * .01) if durations else 0)
        old = buckets(before, name)
        bs = sorted((le, n - old.get(le, 0)) for le, n in buckets(after, name).items())
        # Entire CDF must agree, allowing timing measurements near a bucket edge.
        cdf_ok = bool(bs) and all(sum(x < le - max(.005, x * .01) for x in durations) <= n <=
                                  sum(x <= le + max(.005, x * .01) for x in durations) for le, n in bs)
        rank = .95 * len(durations)
        previous_le, previous_n, quantile, bounds = 0., 0., None, None
        for le, n in bs:
            if n >= rank and rank and n > previous_n:
                quantile = (previous_le + (le - previous_le) * (rank - previous_n) / (n - previous_n)
                            if math.isfinite(le) else previous_le)
                bounds = (previous_le, le)
                break
            previous_le, previous_n = le, n
        raw = sorted(durations)[math.ceil(rank) - 1] if durations else None
        margin = max(.005, raw * .01) if raw is not None else 0
        good = cdf_ok and bounds is not None and raw is not None and bounds[0] - margin <= raw <= bounds[1] + margin
        checks.append({"metric": title + " p95(s)", "otel": quantile, "langfuse": raw,
                       "result": "PASS" if good else "FAIL",
                       "absolute_error": abs(quantile - raw) if quantile is not None and raw is not None else None,
                       "note": f"{p95_note}CDF及对应桶{bounds}须一致，边界容忍每样本5ms或1%"})
    check("Tempo/Langfuse Span 数", tempo_span_count, len(observations))
    return checks


# ---------------------------------------------------------------------------
# T8 成本对比：本地 Decimal 预期 vs Tempo vs Langfuse costDetails vs 完成日志
# ---------------------------------------------------------------------------

def cost_tolerance(amount):
    """spec 金额容差：max(1e-12 USD, abs(total)*1e-9)，Decimal 语义。"""
    return max(Decimal("1e-12"), abs(amount) * Decimal("1e-9"))


def canonical_cost_string(value):
    """与组合根 _canonical_decimal_string 同语义：无指数、无冗余尾零。"""
    with localcontext() as ctx:
        ctx.prec = 60
        return format(Decimal(value).normalize(), "f")


def metadata_decimal(metadata, prefix):
    """Langfuse metadata 中 cost_decimal 的规范化读取。

    采集器以扁平键导出 metadata，Langfuse exporter 重组 JSON 时会把数字形态
    的字符串解析为 number（实测 '0' -> 0、'0.00041' -> 0.00041、'0.00002' ->
    2e-05）。逐字节核对以 Tempo Span 属性与完成日志的原始字符串为权威；
    metadata 侧按 Decimal 规范化后与规范字符串做等值比对。
    """
    value = metadata.get(prefix + ".model.cost_decimal")
    if isinstance(value, str):
        return value
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)):
        return canonical_cost_string(Decimal(str(value)))
    return None


def load_pricing(path):
    """E2E 测试目录：内容版本 + JCS 哈希 + 精确价格表；目录不可用时显式失败。"""
    from app.infrastructure.usage.pricing_catalog import jcs_canonicalize
    text = Path(path).read_text()
    document = json.loads(text)
    prices = {}
    for item in document["prices"]:
        key = (item["billing_provider"], item["billing_model"], item["operation"])
        if key in prices:
            raise ValueError("e2e pricing catalog has duplicate key " + str(key))
        prices[key] = {bucket: (None if item["unit_prices"].get(bucket) is None
                                else Decimal(item["unit_prices"][bucket]))
                       for bucket in COST_BUCKETS}
    return {"content_version": document["content_version"],
            "content_sha256": hashlib.sha256(
                jcs_canonicalize(document).encode()).hexdigest(),
            "prices": prices}


def pricing_entry(catalog, provider, model, operation):
    if not catalog:
        return None
    return catalog.get("prices", {}).get((provider, model, operation))


def expected_cost(usage, prices, operation):
    """本地 Decimal 预期：C_bucket = T_bucket * P_bucket / 1_000_000，全程 Decimal。

    usage 取 Langfuse usageDetails 桶（input 已是 Ti 非缓存输入，openai 与
    anthropic 同义）；chat 需要 input/output/cache_read 已知，cache_write 仅在
    携带时适用（不适用桶省略）；embeddings 仅 input。缺已知桶或缺单价返回
    (None, 原因)，不得伪造为零。返回 (Decimal 总额, {bucket: Decimal})。
    """
    def amount(bucket, tokens):
        unit = (prices or {}).get(bucket)
        if unit is None:
            return None
        with localcontext() as ctx:
            ctx.prec = 60
            return Decimal(tokens) * unit / Decimal(1_000_000)

    try:
        if operation == "embeddings":
            if usage.get("input") is None:
                return None, "missing_usage"
            component = amount("input", usage["input"])
            if component is None:
                return None, "pricing_unavailable"
            return component, {"input": component}
        components = {}
        for bucket in ("input", "output", "cache_read"):
            if usage.get(bucket) is None:
                return None, "missing_usage"
            component = amount(bucket, usage[bucket])
            if component is None:
                return None, "pricing_unavailable"
            components[bucket] = component
        if "cache_write" in usage:
            component = amount("cache_write", usage["cache_write"])
            if component is None:
                # 采集器把适配器声明的已知零值 cache_write 投影为 0（IsInt>=0）；
                # 目录不计价（not_applicable）时 0 桶对总额贡献恒为 0，跳过即可；
                # 非零且无单价仍是 pricing_unavailable，不得伪造为零。
                if usage.get("cache_write") != 0:
                    return None, "pricing_unavailable"
            else:
                components["cache_write"] = component
        return sum(components.values(), Decimal(0)), components
    except (InvalidOperation, TypeError, ValueError):
        return None, "invalid_usage"


def verify_cost_leaf(values, row, catalog):
    """逐叶核对：本地 Decimal 预期、Tempo 属性、Langfuse costDetails、metadata。

    values 为 Tempo 原始 Span 属性，row 为 Langfuse observation。返回失败列
    表；完成日志的逐字节核对由 verify_completion_logs 在 Trace 级补充。
    """
    issues = []
    metadata = row.get("metadata") or {}
    prefix = "nkb" if "nkb.model.cost_status" in values else "nagent"
    status = values.get(prefix + ".model.cost_status")
    if status not in ("estimated", "unknown"):
        return ["缺少受控 cost_status"]
    if metadata.get(prefix + ".model.cost_status") != status:
        issues.append("metadata cost_status 与 Span 不一致")
    amount_keys = tuple(key.replace("nagent.", prefix + ".", 1)
                        for key in COST_AMOUNT_KEYS)
    if status == "unknown":
        for key in amount_keys:
            if key in values:
                issues.append("unknown 携带金额 " + key)
            if key in metadata:
                issues.append("unknown metadata 携带金额 " + key)
        if row.get("costDetails"):
            issues.append("unknown 携带 costDetails")
        reason = values.get(prefix + ".model.cost_reason")
        if reason not in COST_REASONS:
            issues.append("unknown 缺少受控 cost_reason")
        elif metadata.get(prefix + ".model.cost_reason") != reason:
            issues.append("metadata cost_reason 与 Span 不一致")
        if row.get("model"):
            issues.append("unknown 仍投影 model.name（自动推价未阻断）")
        return issues
    decimal = values.get(prefix + ".model.cost_decimal")
    provider = values.get(prefix + ".model.billing_provider")
    model = values.get(prefix + ".model.billing_model")
    operation = ("embeddings" if values.get("gen_ai.operation.name") == "embeddings"
                 else "chat")
    prices = pricing_entry(catalog, provider, model, operation)
    if prices is None:
        return ["目录缺少测试价格项 " + repr((provider, model, operation))]
    # expected_cost 成功时返回 (Decimal 总额, {bucket: Decimal 分项})，失败时
    # 返回 (None, 原因字符串)——两态第二元素语义不同，统一命名 expected。
    total, expected = expected_cost(row.get("usageDetails") or {}, prices, operation)
    if total is None:
        return ["本地预期不可计算（" + expected + "），不能补零"]
    if not isinstance(decimal, str) or canonical_cost_string(total) != decimal:
        issues.append("Tempo cost_decimal 与本地 Decimal 预期不一致")
        return issues
    if metadata_decimal(metadata, prefix) != decimal:
        issues.append("Langfuse metadata cost_decimal 与规范字符串不一致")
    if metadata.get(prefix + ".model.pricing_version") != values.get(prefix + ".model.pricing_version"):
        issues.append("pricing_version 与 Span 不一致")
    if metadata.get(prefix + ".model.price_id") != values.get(prefix + ".model.price_id"):
        issues.append("price_id 与 Span 不一致")
    if (values.get(prefix + ".model.cost_currency") != "USD"
            or metadata.get(prefix + ".model.cost_currency") != "USD"):
        issues.append("currency 必须为 USD")
    if prefix + ".model.cost_reason" in values or prefix + ".model.cost_reason" in metadata:
        issues.append("estimated 不得携带 cost_reason")
    if not row.get("model"):
        issues.append("estimated 未投影 model.name")
    details = row.get("costDetails")
    tolerance = cost_tolerance(total)
    if not isinstance(details, dict) or "total" not in details:
        issues.append("Langfuse costDetails 缺失")
        return issues
    if set(details) != set(expected) | {"total"}:
        issues.append("costDetails 键与适用分项不一致: " + ",".join(sorted(details)))
    for bucket, component in expected.items():
        actual = details.get(bucket)
        if (not isinstance(actual, (int, float)) or isinstance(actual, bool)
                or not math.isfinite(actual)
                or abs(Decimal(str(actual)) - component) > tolerance):
            issues.append("分项 " + bucket + " 超出 double 容差")
    actual_total = details.get("total")
    if (isinstance(actual_total, (int, float)) and not isinstance(actual_total, bool)
            and math.isfinite(actual_total)):
        if abs(Decimal(str(actual_total)) - total) > tolerance:
            issues.append("costDetails total 超出 double 容差")
        double_sum = sum(float(details[bucket]) for bucket in expected
                         if isinstance(details.get(bucket), (int, float))
                         and not isinstance(details.get(bucket), bool))
        if abs(double_sum - float(actual_total)) > float(tolerance):
            issues.append("double 分项和与 total 超出容差")
    else:
        issues.append("costDetails total 非有限数值")
    span_total = values.get(prefix + ".model.cost_usd")
    if (not isinstance(span_total, (int, float)) or isinstance(span_total, bool)
            or not math.isfinite(span_total)
            or abs(Decimal(str(span_total)) - total) > tolerance):
        issues.append("Tempo cost_usd 超出 double 容差")
    return issues


def verify_no_auto_pricing(rows):
    """unknown 样例阻断自动推价 + 计价覆盖率（estimated / 全部模型尝试）。

    每个真实模型叶（GENERATION/EMBEDDING）必须携带受控 cost_status。
    """
    issues = []
    counted = []
    for row in rows:
        if row.get("type") not in ("GENERATION", "EMBEDDING"):
            continue
        metadata = row.get("metadata") or {}
        status = (metadata.get("nagent.model.cost_status")
                  or metadata.get("nkb.model.cost_status"))
        if status not in ("estimated", "unknown"):
            issues.append("模型叶缺少 cost_status: " + str(row.get("id")))
            continue
        counted.append(row)
        if status != "unknown":
            continue
        if row.get("model"):
            issues.append("unknown 叶仍投影 model.name: " + str(row.get("id")))
        if row.get("costDetails"):
            issues.append("unknown 叶携带 costDetails: " + str(row.get("id")))
    estimated = sum(1 for row in counted
                    if row["metadata"][row_cost_prefix(row) + ".model.cost_status"]
                    == "estimated")
    coverage = {"estimated": estimated, "unknown": len(counted) - estimated,
                "total": len(counted),
                "ratio": (estimated / len(counted)) if counted else None}
    return issues, coverage


def row_cost_prefix(row):
    """按 metadata 键集判别叶子归属（N-Agent 与 N-KB 语义键同形不同前缀）。"""
    metadata = row.get("metadata") or {}
    return "nkb" if "nkb.model.cost_status" in metadata else "nagent"


def trace_cost_total(rows):
    """Trace 内模型叶子的精确 Decimal 总额；存在 unknown 或缺 costDetails 时
    返回 None（只能称已观测下界）。父级（AGENT/TOOL 等）不参与。"""
    leaves = [row for row in rows if row.get("type") in ("GENERATION", "EMBEDDING")]
    if not leaves:
        return None
    total = Decimal(0)
    for row in leaves:
        metadata = row.get("metadata") or {}
        prefix = row_cost_prefix(row)
        if metadata.get(prefix + ".model.cost_status") != "estimated":
            return None
        value = (row.get("costDetails") or {}).get("total")
        try:
            total += Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
    return total


def verify_expected_trace_total(rows, expected):
    actual = trace_cost_total(rows)
    if actual is None:
        return ["Trace 存在未知或缺失叶子，只能称已观测下界，无法精确核对总额"]
    return ([] if actual == expected
            else ["Trace 总额 " + str(actual) + " != 预期 " + str(expected)])


def verify_cross_service_chain(rows):
    """跨服务链：同一 Trace 同时含 N-Agent chat 叶与 N-KB embeddings 叶。"""
    kinds = {row.get("type") for row in rows}
    issues = []
    if "GENERATION" not in kinds:
        issues.append("跨服务链缺少 N-Agent chat 叶")
    if "EMBEDDING" not in kinds:
        issues.append("跨服务链缺少 N-KB embeddings 叶")
    return issues


def log_cost_decimal(line):
    """从完成日志行提取 cost_decimal（两服务前缀均识别；Tempo/Loki 分支安全费用事实）。"""
    try:
        document = json.loads(line)
    except ValueError:
        document = None
    keys = {"nagent.model.cost_decimal", "nkb.model.cost_decimal"}

    def search(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key.replace("_", ".") in keys:
                    return value
                found = search(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = search(item)
                if found is not None:
                    return found
        return None

    if document is not None:
        found = search(document)
        if found is not None:
            return found
    match = re.search(r'(?:nagent|nkb)[._]model[._]cost_decimal["=: ]+\\?"([0-9][0-9.eE+-]*)', line)
    return match.group(1) if match else None


def verify_completion_logs(decimals, log_lines):
    """同次完成日志核对：每个 estimated 叶的 cost_decimal 必须逐字节出现。"""
    found = [value for value in (log_cost_decimal(line) for line in log_lines or []) if value]
    issues = []
    if len(found) < len(decimals):
        issues.append("完成日志 cost_decimal 数量 " + str(len(found))
                      + " 少于模型叶 " + str(len(decimals)))
    for decimal in decimals:
        if decimal not in found:
            issues.append("完成日志缺少 cost_decimal " + decimal)
    return issues


def service_versions(tempo):
    """两仓版本证据：Tempo resource 的 service.name -> service.version。"""
    versions = {}
    for batch in tempo.get("batches", tempo.get("resourceSpans", [])):
        attributes = {item["key"]: next(iter(item["value"].values()))
                      for item in batch.get("resource", {}).get("attributes", [])
                      if item.get("value")}
        name, version = attributes.get("service.name"), attributes.get("service.version")
        if name and version:
            versions.setdefault(name, str(version))
    return versions


def series_sum(rows, name, **labels):
    values = [float(row["value"][1]) for row in rows
              if row["metric"].get("__name__") == name
              and all(row["metric"].get(key) == value for key, value in labels.items())]
    return sum(values) if values else None


def split_service_rows(rows):
    """按语义前缀拆分 Counter 对账行：N-Agent 与 N-KB 各自对账。

    窗口内 N-KB 探针叶计入 nkb Counter，不得混入 N-Agent 侧对账。
    """
    agent = [row for row in rows if row_cost_prefix(row) == "nagent"]
    kb = [row for row in rows if row.get("type") == "EMBEDDING"
          and row_cost_prefix(row) == "nkb"]
    return agent, kb


def compare_cost_metrics(before, after, rows, counters):
    """成本 Counter 对账：每实例原始差值 vs 同一尝试集合的 Langfuse 总和。

    total 与 components 分开对账（禁止相加）；attempts（estimated+unknown）
    精确相等。缺序列或缺 decimal 判 SKIP/FAIL，不补零，不用 increase 外推。
    counters = (total, components, attempts, component_label)。
    """
    total_counter, components_counter, attempts_counter, component_label = counters
    # AGENT 与 KB 两套 Counter 行同入一张检查表，行名带服务后缀防歧义。
    service_label = "（nkb）" if total_counter.startswith("nkb_") else "（nagent）"
    leaves = [row for row in rows
              if row.get("type") in ("GENERATION", "EMBEDDING")
              and (row.get("metadata") or {}).get(
                  row_cost_prefix(row) + ".model.cost_status")
              in ("estimated", "unknown")]
    estimated = [row for row in leaves
                 if row["metadata"][row_cost_prefix(row) + ".model.cost_status"]
                 == "estimated"]

    def delta(name, labels=None):
        end = series_sum(after, name, **(labels or {}))
        if end is None:
            return None
        return end - (series_sum(before, name, **(labels or {})) or 0.0)

    checks = []

    def check(name, actual, expected, tolerance=0.0, note=""):
        result = ("SKIP" if expected is None else
                  "FAIL" if actual is None or not math.isfinite(actual)
                  or abs(actual - expected) > tolerance else "PASS")
        checks.append({"metric": name, "otel": actual, "langfuse": expected,
                       "result": result, "tolerance": tolerance,
                       "note": note or ("缺失指标，不能补零" if actual is None else
                                        "无该集合样本，不能补零" if expected is None else
                                        f"绝对容差 {tolerance:g}")})

    decimals = []
    for row in estimated:
        value = metadata_decimal(row.get("metadata") or {}, row_cost_prefix(row))
        try:
            decimals.append(Decimal(value) if value is not None else None)
        except InvalidOperation:
            decimals.append(None)
    if not estimated or any(value is None for value in decimals):
        check("成本总额 Counter 差值" + service_label, delta(total_counter), None, 0.0,
              "存在 estimated 叶缺少 cost_decimal 或无 estimated 尝试，不能补零")
    else:
        expected_total = sum(decimals, Decimal(0))
        check("成本总额 Counter 差值" + service_label, delta(total_counter), float(expected_total),
              float(cost_tolerance(expected_total)), "total 与 components 分开对账，禁止相加")
    for bucket in COST_BUCKETS:
        values = []
        valid = True
        for row in estimated:
            value = (row.get("costDetails") or {}).get(bucket)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value):
                valid = False
                break
            values.append(Decimal(str(value)))
        actual = delta(components_counter, {component_label: bucket})
        if (not values and actual is None
                and not series_sum(before, components_counter, **{component_label: bucket})):
            continue  # 双侧均无该桶样本（不适用桶），不产出检查行
        if not valid:
            check("成本分项差值 " + bucket + service_label, actual, None, 0.0,
                  "costDetails 分项非法，不能补零")
            continue
        expected_bucket = sum(values, Decimal(0)) if values else None
        check("成本分项差值 " + bucket + service_label, actual,
              float(expected_bucket) if expected_bucket is not None else None,
              float(cost_tolerance(expected_bucket)) if expected_bucket is not None else 0.0)
    check("成本尝试数 Counter 差值" + service_label, delta(attempts_counter), float(len(leaves)), 0.0,
          "estimated 与 unknown 都计入，必须精确相等")
    return checks


def completion_log_lines(support, trace, service="n-agent"):
    """完成日志原始行；None 表示日志未取得（显式失败，不当作空集合）。"""
    query = '{service_name="' + service + '"} |= "' + trace + '"'
    url = ("http://loki:3100/loki/api/v1/query_range?" +
           urlencode({"query": query, "start": str(time.time_ns() - 600_000_000_000),
                      "limit": 100}))
    try:
        document = support.request(url)
    except Exception:
        return None
    streams = (document.get("data") or {}).get("result") or []
    return [line for stream in streams
            for _, line in (stream.get("values") or [])]


def verify_call_costs(spans, rows, log_lines, catalog):
    """一次调用的成本核对：逐叶三后端 + 完成日志 + Trace 级日志集合。"""
    issues = []
    decimals = []
    for row in rows:
        if row.get("type") not in ("GENERATION", "EMBEDDING"):
            continue
        metadata = row.get("metadata") or {}
        prefix = row_cost_prefix(row)
        status = metadata.get(prefix + ".model.cost_status")
        if status not in ("estimated", "unknown"):
            continue
        values = (spans.get(row.get("id")) or {}).get("values") or {}
        leaf_issues = verify_cost_leaf(values, row, catalog)
        issues.extend("叶 " + str(row.get("id")) + ": " + item for item in leaf_issues)
        if status == "estimated":
            decimal_value = metadata_decimal(metadata, prefix)
            if decimal_value is not None:
                decimals.append(decimal_value)
    if decimals:
        if log_lines is None:
            issues.append("完成日志未取得，无法逐字节核对 cost_decimal")
        else:
            issues.extend(verify_completion_logs(decimals, log_lines))
    return issues


def instance_metrics(support, service, instance_id, pattern):
    query = ('{service_name=' + json.dumps(service) + ',service_instance_id=' +
             json.dumps(instance_id) + ',__name__=~' + json.dumps(pattern) + '}')
    value = support.request("http://prometheus:9090/api/v1/query?" + urlencode({"query": query}))
    if value.get("status") != "success":
        raise AssertionError("Prometheus query failed")
    return value["data"]["result"]


def json_safe(value):
    """write_comparison 序列化保护：元组键/Decimal/未知类型降为字符串表示。"""
    if isinstance(value, dict):
        return {(key if isinstance(key, str) else str(key)): json_safe(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write_comparison(directory, data):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "comparison.json").write_text(
        json.dumps(json_safe(data), ensure_ascii=False, indent=2) + "\n")

    def cell(value):
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.9g}"
        return str(value).replace("|", "\\|").replace("\n", " ")

    counts = {key: sum(c["result"] == key for c in data["checks"]) for key in ("PASS", "FAIL", "SKIP")}
    lines = ["# 对比 OTel 与 Langfuse 指标", "",
             f'结果：{data["result"]}；通过{counts["PASS"]}，失败{counts["FAIL"]}，跳过{counts["SKIP"]}。', "",
             "同一服务实例、同批Trace；3次真实calculator流式请求（17×19）。计数使用原始样本差值，不使用increase外推。", "",
             "实例：" + str(data.get("instance_id") or "未取得"), "",
             "检查说明：" + data.get("reason", ""), ""]
    pricing = data.get("pricing") or {}
    versions = data.get("versions") or {}
    if versions or pricing:
        lines += ["## 版本与价格", "",
                  "| 项 | 值 |", "|---|---|",
                  "| n-agent 版本 | " + cell(versions.get("n-agent")) + " |",
                  "| n-kb 版本 | " + cell(versions.get("n-kb")) + " |",
                  "| 价格内容版本 | " + cell(pricing.get("content_version")) + " |",
                  "| 价格内容哈希(SHA-256) | " + cell(pricing.get("content_sha256")) + " |",
                  "| 价格目录来源 | " + cell(pricing.get("source")) + " |",
                  "| 计价异常 | " + cell(pricing.get("error")) + " |", ""]
    cost = data.get("cost") or {}
    if cost:
        coverage = cost.get("coverage") or {}
        ratio = coverage.get("ratio")
        lines += ["## 成本逐叶核对", "",
                  f'计价覆盖率：estimated {coverage.get("estimated")} / 全部 {coverage.get("total")}'
                  f'（{format(ratio, ".2%") if isinstance(ratio, (int, float)) else "无尝试"}）；'
                  f'unknown {coverage.get("unknown")}。', "",
                  "| Trace 场景 | 本地 Decimal 总额 | 预期总额 |", "|---|---|---|"]
        totals = cost.get("trace_totals") or []
        if isinstance(totals, dict):
            totals = [{"trace_id": name, "total": total} for name, total in totals.items()]
        for entry in totals:
            if isinstance(entry, dict):
                lines.append("| " + " | ".join(cell(entry.get(key)) for key in
                                               ("trace_id", "total", "expect")) + " |")
            else:
                lines.append("| " + " | ".join(cell(v) for v in entry) + " |")
        for issue in cost.get("issues", []):
            lines.append("")
            lines.append("- 失败：" + issue)
        lines.append("")
    lines += ["## 指标数值对比", "", "| 指标 | OTel | Langfuse | 结果 | 误差与口径 |",
              "|---|---:|---:|---|---|"]
    for row in data["checks"]:
        lines.append("| " + " | ".join(cell(row.get(k)) for k in ("metric", "otel", "langfuse", "result", "note")) + " |")
    lines += ["", "## Agent 调用 Trace", "", "| 调用 | 结果 | 会话 | TraceID | 链接 |", "|---|---|---|---|---|"]
    for i, call in enumerate(data["calls"], 1):
        link = f'[查看Trace]({call["trace_url"]})' if call.get("trace_url") else "未确认Tempo交付，无法生成Trace链接"
        lines.append("| " + " | ".join(cell(v) for v in (i, call["result"], call.get("session_id"), call.get("trace_id"), link)) + " |")
    if not data["calls"]:
        lines.append("| — | 未执行 | — | — | 前置检查失败，未调用Agent |")
    snapshots = [f'[{title}]({name})' for title, name in (("调用前快照", "prometheus-before.json"),
                 ("调用后快照", "prometheus-after.json")) if (directory / name).exists()]
    lines += ["", "## 证据与边界", "",
              "- Prometheus：" + (" / ".join(snapshots) if snapshots else "未取得快照"),
              "- 每次调用的trace-<TraceID>.json保留双端原始响应；用量、Span身份、父子关系和时间逐项校验。",
              "- p95按直方图桶精度核验，不表示两端p95数值相等；未知用量不补零且不能判为全部通过。",
              "- 成本：本地 Decimal 预期、Tempo 属性、Langfuse costDetails 与同次完成日志逐叶核对；"
              "cost_decimal 三后端逐字节一致；double 金额容差 max(1e-12, abs(total)*1e-9)。"
              "成本 Counter（total/components 分开）与 attempts 按每实例原始差值对账，不以 increase 外推。",
              "- 采样边界：SDK 头部丢弃、继承未采样父级与 Collector 尾部丢弃场景由 smoke 套件"
              "（sampling/fault-queue 用例）验证计算结果与独立 Metric 不变；本对比只覆盖已送达子集，"
              "总额仅称已观测叶子成本（下界），不按采样率反推支出，不宣称端到端总成本完整。",
              "- 会话清理：" + data.get("cleanup", "未执行"), ""]
    (directory / "comparison.md").write_text("\n".join(lines))


def cleanup_owned_sessions(directory, run_id, app, *, request_fn=None, timeout_seconds=30):
    """Manifest is the sole deletion authority; cancel, delete, then read back."""
    path = Path(directory) / "owned-sessions.jsonl"
    entries = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    owned = {}
    for entry in entries:
        if entry.get("run_id") != run_id:
            continue
        sid, source = entry.get("session_id"), entry.get("source")
        if not isinstance(sid, str) or not re.fullmatch(r"e2e-langfuse-[0-9a-f]{32}", sid) or source not in ("api", "dashboard"):
            raise ValueError("invalid owned session manifest")
        if sid in owned and owned[sid] != source:
            raise ValueError("conflicting session ownership")
        owned[sid] = source
    request_fn = request_fn or load_support("langfuse_smoke").request
    evidence = {"removed": [], "already_absent": [], "failed": []}

    def detail(url):
        try:
            value = request_fn(url, timeout=5)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(value, dict) or "session" not in value:
            raise ValueError("invalid session readback")
        return value["session"]

    for sid, source in owned.items():
        url = app.rstrip("/") + "/chat/sessions/" + sid
        value = detail(url)
        if value is None:
            evidence["already_absent"].append(sid)
            continue
        if (not isinstance(value, dict) or value.get("source") != source or
                value.get("session_id", sid) != sid):
            raise ValueError("session ownership mismatch")
        deadline = time.monotonic() + timeout_seconds
        cancelled = set()
        while True:
            try:
                request_fn(url, method="DELETE", timeout=5)
            except HTTPError as error:
                if error.code not in (404, 409):
                    raise
                if error.code == 409:
                    runs = request_fn(url + "/runs", timeout=5)
                    if not isinstance(runs, list):
                        raise ValueError("invalid session runs")
                    for run in runs:
                        if run.get("session_id") != sid:
                            raise ValueError("run ownership mismatch")
                        if run.get("status") not in ("pending", "running"):
                            continue
                        ident = run.get("id")
                        if not isinstance(ident, str) or not ident:
                            raise ValueError("missing active run identity")
                        if ident not in cancelled:
                            try:
                                request_fn(url + "/runs/" + quote(ident, safe="") + "/cancel", method="POST", timeout=5)
                            except HTTPError as cancel_error:
                                if cancel_error.code not in (404, 409):
                                    raise
                            cancelled.add(ident)
            value = detail(url)
            if value is None:
                evidence["removed"].append(sid)
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("owned session remains after cleanup")
            time.sleep(.25)
    return evidence


@contextlib.contextmanager
def probe_call_guard(call, runner, directory, run_id, reporting, save):
    """collect_call/unknown_call 共享脚手架。

    异常时保留已取得的 Trace 证据（Tempo 原始 Span 落盘、trace_url）；退出时
    清理 owned sessions，清理失败覆盖 cleanup=FAIL 并上抛，随后落盘快照。
    """
    try:
        yield
    except Exception:
        trace = runner.trace
        call.update(result="FAIL", trace_id=trace)
        if call.get("session_id") is None and trace:
            call["session_id"] = "e2e-langfuse-" + trace
        if trace:
            try:
                tempo = runner.tempo(trace)
                path = directory / ("trace-" + trace + ".json")
                if not path.exists():
                    path.write_text(json.dumps({"tempo": tempo}) + "\n")
                call["trace_url"] = reporting.trace_url(runner.report.base_url, trace)
            except Exception:
                pass
        raise
    finally:
        try:
            call["cleanup_evidence"] = cleanup_owned_sessions(directory, run_id, runner.app)
            call["cleanup"] = "PASS"
        except Exception:
            call["cleanup"] = "FAIL"
            raise
        finally:
            save()


def collect_call(runner, directory, run_id, data, support, reporting, save):
    call = {"result": "RUNNING", "trace_id": None, "trace_url": None}
    data["calls"].append(call)
    runner.trace = runner.confirmed_trace = None
    with probe_call_guard(call, runner, directory, run_id, reporting, save):
        evidence = runner.business()
        trace = evidence["trace_id"]
        tempo = runner.tempo(trace)
        observations = support.observations(runner.get, trace)
        (directory / ("trace-" + trace + ".json")).write_text(
            json.dumps({"tempo": tempo, "langfuse": observations}, ensure_ascii=False) + "\n")
        support.compare_backends(tempo, observations)
        data.setdefault("versions", {}).update(service_versions(tempo))
        issues = verify_call_costs(support.original_spans(tempo), observations,
                                   completion_log_lines(support, trace), data.get("catalog"))
        data.setdefault("cost_issues", []).extend(issues)
        total = trace_cost_total(observations)
        data.setdefault("trace_totals", []).append(
            {"trace_id": trace, "total": None if total is None else str(total),
             "issues": issues})
        call.update(result="PASS", trace_id=trace, session_id=evidence["session"],
                    trace_url=reporting.trace_url(runner.report.base_url, trace))
        return observations, len(support.original_spans(tempo))


def unknown_call(runner, directory, run_id, data, support, reporting, save):
    """unknown 样例：请求模型不在受信计价身份映射内 => identity_unresolved。

    正向验证自动推价阻断：Langfuse 不投影 model、不携带 costDetails；已观测
    用量仍保留；attempt 计入 Counter 对账窗口。不调用 compare_backends——
    unknown 叶的 model/costDetails 与 Tempo 有意不一致，其余口径由
    verify_call_costs 与本函数的定向断言覆盖。
    """
    call = {"result": "RUNNING", "trace_id": None, "trace_url": None, "kind": "unknown-probe"}
    data["unknown_call"] = call
    runner.trace = runner.confirmed_trace = None
    trace = uuid.uuid4().hex
    runner.trace = trace
    session = "e2e-langfuse-" + trace
    runner.manifest.add(session, "api")
    with probe_call_guard(call, runner, directory, run_id, reporting, save):
        headers = {"X-Session-ID": session,
                   "traceparent": "00-" + trace + "-1234567890abcdef-01",
                   "Content-Type": "application/json"}
        body = json.dumps({"model": UNKNOWN_PROBE_MODEL, "stream": True, "temperature": 0.25,
                           "messages": [{"role": "user", "content": "请仅回答数字 42。"}]},
                          ensure_ascii=False).encode()
        with urlopen(Request(runner.app + "/v1/chat/completions", data=body, headers=headers),
                     timeout=150) as response:
            response.read()
        rows = support.poll(lambda: (value if (value := support.observations(runner.get, trace))
                                     else None), seconds=90)
        tempo = support.poll(lambda: runner.tempo(trace), seconds=90)
        (directory / ("trace-" + trace + ".json")).write_text(
            json.dumps({"tempo": tempo, "langfuse": rows}, ensure_ascii=False) + "\n")
        data.setdefault("versions", {}).update(service_versions(tempo))
        issues = verify_call_costs(support.original_spans(tempo), rows,
                                   completion_log_lines(support, trace), data.get("catalog"))
        leaves = [row for row in rows if row.get("type") == "GENERATION"]
        if len(leaves) != 1:
            issues.append("unknown 样例应恰有 1 个 GENERATION 叶，实际 " + str(len(leaves)))
        else:
            reason = (leaves[0].get("metadata") or {}).get("nagent.model.cost_reason")
            if reason != "identity_unresolved":
                issues.append("unknown 样例 cost_reason 应为 identity_unresolved，实际 "
                              + repr(reason))
            if leaves[0].get("costDetails"):
                issues.append("unknown 样例携带 costDetails（自动推价未阻断）")
            if leaves[0].get("model"):
                issues.append("unknown 样例仍投影 model.name")
            if not leaves[0].get("usageDetails"):
                issues.append("unknown 样例已观测用量被丢弃")
        data.setdefault("cost_issues", []).extend(issues)
        call.update(result="FAIL" if issues else "PASS", trace_id=trace, session_id=session,
                    trace_url=reporting.trace_url(runner.report.base_url, trace))
        if issues:
            raise AssertionError("unknown 样例成本核对失败 " + str(len(issues)) + " 项")
        return rows, len(support.original_spans(tempo))


def replay_cleanup(directory, run_id):
    """Recovery requires the manifest and app only, never Langfuse credentials."""
    directory = Path(directory)
    path = directory / "comparison.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {"result": "FAIL", "checks": [], "calls": [], "reason": "对账执行中断或报告未完整写入"}
    if data["result"] == "RUNNING":
        data.update(result="FAIL", reason="对账执行中断；已重放会话清理")
    try:
        data["cleanup_evidence"] = cleanup_owned_sessions(
            directory, run_id, os.environ.get("LANGFUSE_APP_URL", "http://n-agent-n-agent-1:8201"))
        data["cleanup"] = "PASS"
    except Exception as error:
        data.update(result="FAIL", cleanup="FAIL", reason="清单会话清理失败：" + type(error).__name__)
    write_comparison(directory, data)
    return 0 if data["cleanup"] == "PASS" else 1


def kb_search(kb_url, payload, *, attempts=3):
    """N-KB 检索 POST；Ollama bge-m3 空闲卸载后冷加载（实测约 16s+）可超出
    N-KB 30s 嵌入超时并返回 502 infrastructure_error（attempt-10 真机根因），
    按重试兜底吸收冷启动时序噪声；仅重试 502，其余失败立即抛出。"""
    error = None
    for attempt in range(attempts):
        try:
            return _post(kb_url.rstrip("/") + "/retrieval/search", payload)
        except HTTPError as exc:
            if exc.code != 502:
                raise
            error = exc
            time.sleep(2)
    raise error


def tempo_kb_trace_ids(support, seconds=900):
    document = support.request("http://tempo:3200/api/search?" + urlencode(
        {"tags": 'service.name="n-kb"', "start": str(int(time.time()) - seconds),
         "end": str(int(time.time()))}))
    return {row["traceID"] for row in document.get("traces", [])}


def kb_index_probe(support, runner, directory, data, catalog):
    """直连 N-KB 检索（独立根 Trace，无 traceparent）：向量 C 精确 0.00002。

    检索 query 必须与 N-Agent 容器内挂载的 /tests/fixtures/cost-probe-query.txt
    一致（bge-m3 实测 prompt_eval_count=200）；文件缺失或为空即失败，不用替身
    字符串掩盖用量偏离。返回 (rows, span_count, trace_id, issues)。
    """
    probe_query = Path("/tests/fixtures/cost-probe-query.txt").read_text(
        encoding="utf-8").strip()
    if not probe_query:
        raise AssertionError("200-token 检索串为空，无法核对向量 C")
    kb_url = os.environ["N_KB_APP_URL"].rstrip("/")
    known = tempo_kb_trace_ids(support)

    def poll_labeled(fn, label, seconds=150):
        try:
            return support.poll(fn, seconds=seconds)
        except TimeoutError as error:
            raise TimeoutError("窗口内N-KB探针等待超时: " + label) from error

    kb_search(kb_url, {"query": probe_query, "top_k": 3})
    trace = poll_labeled(
        lambda: next(iter(tempo_kb_trace_ids(support) - known), None), "Tempo新Trace")
    rows = poll_labeled(lambda: (value if (value := support.observations(runner.get, trace))
                                 else None), "Langfuse observations")

    # 同跨服务链：等 Tempo 包含全部 Langfuse 模型叶对应 Span 后再逐叶核对。
    def complete_probe_tempo():
        document = runner.tempo(trace)
        spans = support.original_spans(document)
        wanted = {row["id"] for row in rows
                  if row.get("type") in ("GENERATION", "EMBEDDING")}
        return document if wanted and wanted <= set(spans) else None

    tempo = poll_labeled(complete_probe_tempo, "Tempo trace", seconds=120)
    (directory / ("trace-" + trace + ".json")).write_text(
        json.dumps({"tempo": tempo, "langfuse": rows}, ensure_ascii=False) + "\n")
    data.setdefault("versions", {}).update(service_versions(tempo))
    issues = verify_call_costs(support.original_spans(tempo), rows,
                               completion_log_lines(support, trace, "n-kb"), catalog)
    issues.extend(verify_expected_trace_total(rows, Decimal("0.00002")))
    return rows, len(support.original_spans(tempo)), trace, issues


def run_kb_scenarios(support, runner, directory, data, catalog, reporting,
                     chain_total=Decimal("0.00043"), index_total=Decimal("0.00002")):
    """T8 S4 场景：跨服务链（chat + 检索 embedding 同 Trace）与独立索引 Trace。

    需要：(1) 两服务注入同一测试价格目录（N_AGENT/N_KB_PRICING_CATALOG_*），
    (2) N-KB 检索 embedding 的 prompt_eval_count=200（向量 C），(3) N-Agent
    chat 为向量 B 用量（确定性 Provider）。未配置时显式记录未执行原因，不得
    计为通过。期望总额为向量 B+C 精确值；用量偏离即失败，不用时间窗补齐。
    """
    kb_url = os.environ.get("N_KB_APP_URL")
    if not kb_url or not catalog:
        return {"result": "SKIP",
                "reason": "N_KB_APP_URL 或测试价格目录未配置；跨服务链与独立索引场景未执行"}
    issues, totals = [], []

    # 独立索引 Trace：探针直连 N-KB 检索，不带 traceparent（独立根 Trace）。
    rows, _, trace, probe_issues = kb_index_probe(
        support, runner, directory, data, catalog)
    issues.extend(probe_issues)
    totals.append({"trace_id": trace, "expect": str(index_total),
                   "total": str(trace_cost_total(rows))})
    # 跨服务链：N-Agent chat 内触发知识库检索，N-KB embedding 落在同一 Trace。
    trace = uuid.uuid4().hex
    session = "e2e-langfuse-" + trace
    runner.manifest.add(session, "api")
    headers = {"X-Session-ID": session,
               "traceparent": "00-" + trace + "-1234567890abcdef-01",
               "Content-Type": "application/json"}
    body = json.dumps({"model": "N-Agent", "stream": True, "temperature": 0.25,
                       "messages": [{"role": "user", "content":
                                     "请必须先使用知识库检索工具查询「成本」，然后仅基于检索结果回答知识库里有什么。"}]},
                      ensure_ascii=False).encode()
    with urlopen(Request(runner.app + "/v1/chat/completions", data=body, headers=headers),
                 timeout=150) as response:
        response.read()

    def chain_rows():
        value = support.observations(runner.get, trace)
        kinds = {row["type"] for row in value}
        return value if {"GENERATION", "EMBEDDING"} <= kinds else None

    rows = support.poll(chain_rows, seconds=120)

    # Tempo 对跨服务的近期 Trace 可能先返回部分 Span；等待每个 Langfuse 模型叶
    # 都能在 Tempo 侧找到对应 Span 后再做逐叶核对，避免把部分响应当完整证据。
    def complete_chain_tempo():
        document = runner.tempo(trace)
        spans = support.original_spans(document)
        wanted = {row["id"] for row in rows
                  if row.get("type") in ("GENERATION", "EMBEDDING")}
        return document if wanted and wanted <= set(spans) else None

    tempo = support.poll(complete_chain_tempo, seconds=120)
    data.setdefault("versions", {}).update(service_versions(tempo))
    # 链上两服务的完成日志都要取得：N-KB embedding 的 cost_decimal 记在 n-kb 侧。
    log_lines = (completion_log_lines(support, trace, "n-agent") or []) + \
                (completion_log_lines(support, trace, "n-kb") or [])
    log_lines = log_lines or None
    issues.extend("跨服务链: " + item for item in
                  verify_call_costs(support.original_spans(tempo), rows,
                                    log_lines, catalog))
    issues.extend("跨服务链: " + item for item in verify_cross_service_chain(rows))
    issues.extend("跨服务链: " + item for item in
                  verify_expected_trace_total(rows, chain_total))
    totals.append({"trace_id": trace, "expect": str(chain_total),
                   "total": str(trace_cost_total(rows))})
    support.request(runner.app + "/chat/sessions/" + session, method="DELETE")
    cost = data.setdefault("cost", {"coverage": {}, "issues": [], "trace_totals": []})
    cost["issues"].extend(issues)
    cost["trace_totals"].extend(totals)
    if issues:
        raise AssertionError("KB 场景成本核对失败 " + str(len(issues)) + " 项")
    return {"result": "PASS", "index_trace": totals[0]["trace_id"],
            "chain_trace": totals[1]["trace_id"], "expect": [str(index_total), str(chain_total)]}


def run(directory, run_id):
    """All business and backend traffic is restricted to this Docker entry point."""
    if not Path("/.dockerenv").exists():
        raise RuntimeError("E2E must run inside Docker")
    support = load_support("langfuse_smoke")
    reporting = load_support("result_report")
    directory = Path(directory)
    data = {"result": "RUNNING", "reason": "执行中", "checks": [], "calls": [], "cleanup": "未执行",
            "started_at": datetime.now(timezone.utc).isoformat()}
    # T8：E2E 测试价格目录（与两服务注入的目录同文件同哈希）；加载失败显式记录，
    # 不静默降级——estimated 叶在无目录时逐叶核对会失败。
    catalog_path = (os.environ.get("E2E_PRICING_CATALOG")
                    or str(Path(__file__).with_name("fixtures/model-pricing-e2e.json")))
    try:
        pricing = load_pricing(catalog_path)
        pricing["source"] = catalog_path
        data["catalog"] = pricing
    except Exception as error:
        pricing = {"error": type(error).__name__ + "：" + str(error)[:200], "source": catalog_path}
        data["catalog"] = None
    # data["pricing"] 只放 JSON 安全的版本/哈希摘要（价格表以元组键+Decimal 供
    # 核对使用，不进报告体；完整目录经 json_safe 序列化保存在 catalog 字段）。
    data["pricing"] = ({key: pricing[key] for key in ("content_version", "content_sha256", "source")}
                       if data["catalog"] else pricing)
    # N-KB 上下文：跨服务链与 N-KB Counter 对账需要直达 N-KB 自检接口。
    kb_url = os.environ.get("N_KB_APP_URL")
    kb_instance = None
    if kb_url:
        try:
            kb_instance = support.request(kb_url.rstrip("/") + "/observations/otel")["instance_id"]
        except Exception as error:
            data["kb_error"] = type(error).__name__
    data["kb_instance_id"] = kb_instance
    runner = None

    def save():
        write_comparison(directory, data)

    def snapshot():
        if support.request(runner.app + "/chat/observations/otel")["instance_id"] != runner.instance_id:
            raise AssertionError("service instance changed during comparison")
        query = ('{service_name="n-agent",service_instance_id=' + json.dumps(runner.instance_id) +
                 ',__name__=~"(nagent_agent_runs_total|nagent_model_.*|gen_ai_.*)"}')
        value = support.request("http://prometheus:9090/api/v1/query?" + urlencode({"query": query}))
        if value.get("status") != "success":
            raise AssertionError("Prometheus query failed")
        return value["data"]["result"]

    save()
    try:
        # N-KB embed 冷启动预热：Ollama 空闲卸载 bge-m3 后冷加载可超 N-KB 30s
        # 嵌入超时（502），在 Metric 对账窗口前预热，不计入 Counter 差值；
        # 预热失败即失败，不静默进入窗口。
        if kb_url:
            try:
                kb_search(kb_url, {"query": "成本探针预热", "top_k": 1})
                data["kb_warmup"] = "PASS"
            except Exception as error:
                data["kb_warmup"] = "FAIL " + support.safe_error(error)
                raise
        runner = support.Runner(str(directory), run_id, plan=False)
        data["instance_id"] = runner.instance_id
        # Settle metrics from preceding OTel/failure/cancel or conversation cases.
        time.sleep(12)
        before = snapshot()
        before_kb = (instance_metrics(support, "n-kb", kb_instance, "nkb_model_cost_.*")
                     if kb_instance else [])
        (directory / "prometheus-before.json").write_text(json.dumps(before, indent=2) + "\n")
        rows, span_count = [], 0
        for _ in range(3):
            observations, count = collect_call(runner, directory, run_id, data, support, reporting, save)
            rows.extend(observations)
            span_count += count
        # unknown 样例在 Metric 对账窗口内执行：attempt 计入 Counter 差值。
        unknown_rows, unknown_count = unknown_call(
            runner, directory, run_id, data, support, reporting, save)
        rows.extend(unknown_rows)
        span_count += unknown_count
        # 窗口内 N-KB 独立索引探针：向量 C 0.00002，使 nkb Counter 差值可对账；
        # 直连 N-KB，不产生 N-Agent 侧请求。
        if kb_instance:
            kb_rows_in_window, kb_span_count, _, kb_probe_issues = kb_index_probe(
                support, runner, directory, data, data.get("catalog"))
            if kb_probe_issues:
                data.setdefault("cost_issues", []).extend(
                    "窗口内N-KB索引: " + item for item in kb_probe_issues)
                raise AssertionError("窗口内 N-KB 索引探针成本核对失败 "
                                     + str(len(kb_probe_issues)) + " 项")
            rows.extend(kb_rows_in_window)
            span_count += kb_span_count
        time.sleep(12)
        after = snapshot()
        after_kb = (instance_metrics(support, "n-kb", kb_instance, "nkb_model_cost_.*")
                    if kb_instance else [])
        (directory / "prometheus-after.json").write_text(json.dumps(after, indent=2) + "\n")
        data["checks"] = compare_metrics(before, after, rows, span_count)
        agent_rows, kb_rows = split_service_rows(rows)
        data["checks"].extend(compare_cost_metrics(before, after, agent_rows, AGENT_COST_COUNTERS))
        if kb_rows:
            data["checks"].extend(compare_cost_metrics(before_kb, after_kb, kb_rows, KB_COST_COUNTERS))
        data.update(result=verdict(data["checks"]), reason="计数/Token严格对齐；耗时与p95按报告所列容差核验")
        cost_issues, coverage = verify_no_auto_pricing(rows)
        cost_issues = list(data.pop("cost_issues", [])) + cost_issues
        data["cost"] = {"coverage": coverage, "issues": cost_issues,
                        "trace_totals": data.pop("trace_totals", [])}
        if cost_issues:
            data.update(result="FAIL", reason=data["reason"] +
                        "；成本核对失败 " + str(len(cost_issues)) + " 项")
        # KB 场景在 Metric 对账窗口之后执行，不污染 Counter 差值窗口。
        data["kb_scenarios"] = run_kb_scenarios(
            support, runner, directory, data, data.get("catalog"), reporting)
        save()
    except Exception as error:
        # 失败时保留已取得的成本证据；AssertionError 消息为本探针自述，可入报告。
        cost = data.setdefault("cost", {"coverage": {}, "issues": [], "trace_totals": []})
        cost["issues"].extend(data.pop("cost_issues", []))
        cost["trace_totals"].extend(data.pop("trace_totals", []))
        detail = str(error) if isinstance(error, (AssertionError, TimeoutError)) else ""
        if isinstance(error, HTTPError):
            # HTTPError 的类型名只含状态码；补充 URL 与 reason 以定位来源后端。
            extra = " ".join(x for x in (getattr(error, "url", "") or "",
                                         getattr(error, "reason", "") or "") if x)
            detail = (detail + " " + extra).strip() if detail else extra
        data.update(result="FAIL", reason=support.safe_error(error)
                    + (("；" + detail) if detail else "")
                    + "; " + getattr(runner, "stage", "preflight"))
    finally:
        if runner:
            try:
                data["cleanup_evidence"] = cleanup_owned_sessions(directory, run_id, runner.app)
                data["cleanup"] = "PASS"
            except Exception as error:
                data.update(result="FAIL", cleanup="FAIL", reason=data["reason"] + "; cleanup " + support.safe_error(error))
        data["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return 1 if data["result"] == "FAIL" else 0


if __name__ == "__main__":
    if not Path("/.dockerenv").exists():
        raise SystemExit("E2E must run inside Docker")
    if sys.argv[1] == "--cleanup-only":
        sys.exit(replay_cleanup(sys.argv[2], sys.argv[3]))
    sys.exit(run(sys.argv[1], sys.argv[2]))
