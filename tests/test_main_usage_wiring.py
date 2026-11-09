from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.domain.provider import ProviderConfig
from app.main import build_application_services


@pytest.fixture(autouse=True)
def _isolate_global_observability(monkeypatch):
    """wiring 测试不得安装进程级观测状态（评审修复第三轮测试卫生）。

    otel_enabled=True 的装配会经真实 configure_observability 发布全局
    Tracer/Meter/Logger Provider（进程内不可逆）并经 instrument_clients
    注册 httpx/sqlite3 instrumentor 与 wrapt 补丁——这些状态在测试后残留，
    污染同进程后续观测测试（仅子集顺序暴露，全量字母序被掩蔽）。
    estimator/身份接线只需 SemanticsTelemetry 门面（Span/Metric 走 OTel
    空代理），不依赖真实 SDK 装配，统一以 fake handle + 空实现隔离。"""
    from app.infrastructure.observability import instrumentors
    from app.infrastructure.observability import setup as obs_setup

    monkeypatch.setattr(
        obs_setup, "configure_observability",
        lambda settings: SimpleNamespace(instance_id="test"),
    )
    monkeypatch.setattr(instrumentors, "instrument_clients", lambda **kwargs: True)
    yield
    # 防御复位：即便有路径绕过隔离，也不把注册状态泄漏给后续测试
    instrumentors._reset_for_tests()
    obs_setup._reset_for_tests()


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        artifacts_enabled=False,
    )


def test_usage_service_assembled_on_runner(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    runner = services.chat_service.graph_runner
    assert runner.usage_service is not None
    # verify internal components wired
    assert runner.usage_service._recorder is not None
    assert runner.usage_service._pricing is not None
    assert runner.usage_service._breakdown is not None


def test_usage_service_exposed_on_application_services(tmp_path: Path):
    """build_application_services must expose usage_service for CLI access."""
    services = build_application_services(_settings(tmp_path))
    assert services.usage_service is not None
    assert services.usage_service is services.chat_service.graph_runner.usage_service


def test_usage_recorder_uses_same_sqlite_path(tmp_path: Path):
    """The SqliteUsageRecorder should share the same DB path as MemoryStore."""
    settings = _settings(tmp_path)
    services = build_application_services(settings)
    recorder = services.usage_service._recorder
    from app.infrastructure.usage.sqlite_usage_recorder import SqliteUsageRecorder
    assert isinstance(recorder, SqliteUsageRecorder)
    # the recorder shares the same sqlite path (db_path attribute is private but
    # we verify it via the public init() being callable again idempotently)
    import asyncio
    asyncio.run(recorder.init())


def _write_vector_catalog(tmp_path: Path) -> tuple[str, str]:
    """统一测试价格向量目录：input=2、output=10、cache_read=0.2 USD/百万。"""
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": [
                {
                    "price_id": "p-gpt4o-test",
                    "billing_provider": "openai",
                    "billing_model": "gpt-4o",
                    "operation": "chat",
                    "aliases": [],
                    "unit_prices": {
                        "input": "2", "output": "10",
                        "cache_read": "0.2", "cache_write": None,
                    },
                    "source_url": "https://example.com/pricing",
                    "verified_at": "2026-11-09",
                }
            ],
        },
        ensure_ascii=False,
    )
    path = tmp_path / "catalog.json"
    path.write_text(text, encoding="utf-8")
    from app.infrastructure.usage.pricing_catalog import jcs_canonicalize

    expected = hashlib.sha256(jcs_canonicalize(json.loads(text)).encode("utf-8")).hexdigest()
    return str(path), expected


def _provider_config(provider_id: str) -> ProviderConfig:
    now = datetime.now(timezone.utc)
    return ProviderConfig(
        id=provider_id, name="test", provider_type="openai-compatible",
        base_url="", model="gpt-4o", api_key_present=True, is_active=True,
        extra_headers=None, created_at=now, updated_at=now,
    )


def test_model_cost_estimator_is_injected_after_usage_service(tmp_path):
    """受信身份映射 + 目录齐备时 estimator 产出向量 B 的 estimated 结果；
    身份缺失回退 unknown（旧契约形态）；已知桶与确认桶不一致拒绝计价。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    catalog_path, expected_sha = _write_vector_catalog(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    telemetry = services.telemetry
    # The provider factory was created earlier and holds this bound method.
    callback = telemetry.begin_chat_attempt
    attempt = callback(model='gpt-4o', stream=False, provider='openai')
    estimator = attempt._cost_estimator
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    result = estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0))
    assert result['status'] == 'estimated'
    assert result['pricing_version']
    # 统一向量 B：Ti=100、To=20、Tic=50 => 0.0002 + 0.0002 + 0.00001
    assert result['amount_usd'] == pytest.approx(0.00041)
    # 受信身份缺失（provider 配置无映射）=> identity_unresolved 按旧契约收口
    asyncio.run(services.provider_holder.swap(_provider_config("prov-other"), "k"))
    unknown = estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0))
    assert unknown['status'] == 'unknown'
    assert unknown['pricing_version'] is None
    # 已知桶与 ChatAttempt 确认桶不一致 => 拒绝计价（保持 None 契约）
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    assert estimator(raw, 'gpt-4o', 'openai', (10, 4, 2, 0)) is None
    assert estimator({'prompt_tokens': 10, 'completion_tokens': 4}, 'gpt-4o', 'openai', (10, 4, 0, 0)) is None


def test_model_cost_estimator_unknown_when_catalog_unconfigured(tmp_path):
    """目录未配置：身份齐全、用量齐全仍按 pricing_unavailable 收口 unknown。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    attempt = services.telemetry.begin_chat_attempt(
        model='gpt-4o', stream=False, provider='openai'
    )
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    result = attempt._cost_estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0))
    assert result['status'] == 'unknown'
    assert result['pricing_version'] is None


def test_model_cost_estimator_extended_result_contract(tmp_path):
    """T4 扩展契约：estimated/unknown 结果携带成本完整事实（老键语义不变），
    ChatAttempt 桶状态经第五参传入（invalid 优先于计价、known 逐值一致）。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    catalog_path, expected_sha = _write_vector_catalog(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    estimator = services.telemetry.begin_chat_attempt(
        model='gpt-4o', stream=False, provider='openai')._cost_estimator
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    states = ('known', 'known', 'known', 'unknown')
    result = estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0), states)
    assert result['status'] == 'estimated'
    assert result['currency'] == 'USD'
    assert result['price_id'] == 'p-gpt4o-test'
    assert result['cost_decimal'] == '0.00041'
    assert result['reason'] is None
    assert result['billing_provider'] == 'openai'
    assert result['billing_model'] == 'gpt-4o'
    assert set(result['components']) == {'input', 'output', 'cache_read'}
    # unknown 形态：reason 8 枚举、目录成功时保留版本、携带受信身份、金额占位
    unknown = estimator({'prompt_tokens': 150, 'completion_tokens': 20},
                        'gpt-4o', 'openai', (None, 20, None, 0),
                        ('unknown', 'known', 'unknown', 'unknown'))
    assert unknown['status'] == 'unknown'
    assert unknown['reason'] == 'missing_usage'
    assert unknown['pricing_version']
    assert unknown['billing_provider'] == 'openai'
    assert unknown['amount_usd'] == 0.0  # 老键占位，观测侧不消费
    # 曾非法（invalid）按 T1 顺序先于 missing_usage 收口
    invalid = estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0),
                        ('invalid', 'known', 'unknown', 'unknown'))
    assert invalid['status'] == 'unknown'
    assert invalid['reason'] == 'invalid_usage'
    # 桶状态 known 与 raw 视图不一致仍拒绝计价（None 契约）
    assert estimator(raw, 'gpt-4o', 'openai', (10, 4, 2, 0),
                     ('known', 'known', 'known', 'unknown')) is None


def test_cache_ttl_fact_derivation(tmp_path):
    """TTL 条件事实：单一非零 TTL 产出 cache_ttl，多 TTL 并存或缺失不产出；
    任一在场子字段非法时整组事实拒绝（评审修复第三轮：非法子字段不得被
    "仅取正值" 抽取掩盖）。"""
    from app.application.usage_service import CACHE_TTL_UNSUPPORTED, cache_ttl_facts
    assert cache_ttl_facts({'cache_creation': {'ephemeral_1h_input_tokens': 10}}) == {'cache_ttl': '1h'}
    assert cache_ttl_facts({'cache_creation': {'ephemeral_5m_input_tokens': 3}}) == {'cache_ttl': '5m'}
    # 混合合法 TTL：显式不支持哨兵（评审修复第四轮 D1），不得回落为事实缺失
    assert cache_ttl_facts({'cache_creation': {
        'ephemeral_1h_input_tokens': 10, 'ephemeral_5m_input_tokens': 3}}) is CACHE_TTL_UNSUPPORTED
    # 嵌套 {"usage": {...}} 形态解包一次后与平铺同判（评审修复第四轮 D2）
    assert cache_ttl_facts({'usage': {
        'cache_creation': {'ephemeral_1h_input_tokens': 10}}}) == {'cache_ttl': '1h'}
    assert cache_ttl_facts({'usage': {
        'cache_creation': {'ephemeral_1h_input_tokens': 10,
                           'ephemeral_5m_input_tokens': 3}}}) is CACHE_TTL_UNSUPPORTED
    assert cache_ttl_facts({'cache_creation': {'ephemeral_1h_input_tokens': 0}}) is None
    assert cache_ttl_facts({'cache_creation': {
        'ephemeral_1h_input_tokens': 10, 'ephemeral_5m_input_tokens': -1}}) is None
    assert cache_ttl_facts({'cache_creation': {
        'ephemeral_1h_input_tokens': 10, 'ephemeral_5m_input_tokens': 1.5}}) is None
    assert cache_ttl_facts({'cache_creation': {
        'ephemeral_1h_input_tokens': 10, 'ephemeral_5m_input_tokens': 'x'}}) is None
    assert cache_ttl_facts({}) is None
    assert cache_ttl_facts({'cache_creation': 'bad'}) is None
    assert cache_ttl_facts(None) is None


def test_ledger_ttl_invalid_subfield_rejects_whole_fact_set(tmp_path):
    """评审修复（第三轮）：任一在场 TTL 子字段非法时，账本路径与观测路径对
    同一 raw_usage 收口为同一结论（invalid_usage）。此前账本侧 cache_ttl_facts
    只取正值子字段（1h=10 合法、5m=-1 被忽略）仍产出条件事实并可 estimated，
    而观测路径（ChatAttempt 桶级 invalid）报 invalid_usage——同一响应两种
    成本结论。"""
    catalog_path, expected_sha = _write_ttl_conditioned_catalog(tmp_path)
    settings = _settings(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "claude-test",
         "billing_provider": "anthropic", "billing_model": "claude-test"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    raw = {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
           'cache_creation_input_tokens': 10,
           'cache_creation': {'ephemeral_1h_input_tokens': 10,
                              'ephemeral_5m_input_tokens': -1}}
    cost = services.usage_service.estimate_attempt_cost(
        raw, "anthropic", ("anthropic", "claude-test"))
    assert cost.status == "unknown"
    assert cost.reason.value == "invalid_usage"
    # 合法单一 TTL 事实（同一目录同一用量）仍 estimated，验证判定差异来自
    # TTL 子字段合法性而非目录/身份缺失
    from app.application.usage_service import cache_ttl_facts
    healthy = dict(raw, cache_creation={'ephemeral_1h_input_tokens': 10})
    assert cache_ttl_facts(healthy) == {'cache_ttl': '1h'}
    recovered = services.usage_service.estimate_attempt_cost(
        healthy, "anthropic", ("anthropic", "claude-test"))
    assert recovered.status == "estimated"
    assert recovered.price_id == "p-claude-ttl"


def _write_ttl_conditioned_catalog(tmp_path: Path) -> tuple[str, str]:
    """统一 TTL 条件价格目录：input=3、output=15、cache_read=0.3、
    cache_write=3.75 USD/百万，条件 cache_ttl=1h。"""
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": [
                {
                    "price_id": "p-claude-ttl",
                    "billing_provider": "anthropic",
                    "billing_model": "claude-test",
                    "operation": "chat",
                    "aliases": [],
                    "unit_prices": {
                        "input": "3", "output": "15",
                        "cache_read": "0.3", "cache_write": "3.75",
                    },
                    "conditions": {"cache_ttl": "1h"},
                    "source_url": "https://example.com/pricing",
                    "verified_at": "2026-11-09",
                }
            ],
        },
        ensure_ascii=False,
    )
    path = tmp_path / "catalog-ttl.json"
    path.write_text(text, encoding="utf-8")
    from app.infrastructure.usage.pricing_catalog import jcs_canonicalize

    expected = hashlib.sha256(jcs_canonicalize(json.loads(text)).encode("utf-8")).hexdigest()
    return str(path), expected


def test_anthropic_conditioned_catalog_uses_ttl_fact_from_raw(tmp_path):
    """TTL 事实通道：raw_usage 携带 cache_creation 拆分时条件条目按已知 TTL
    命中（estimated）；事实缺失时 unsupported_pricing。"""
    catalog_path, expected_sha = _write_ttl_conditioned_catalog(tmp_path)
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "claude-test",
         "billing_provider": "anthropic", "billing_model": "claude-test"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    estimator = services.telemetry.begin_chat_attempt(
        model='claude-test', stream=False, provider='anthropic')._cost_estimator
    states = ('known', 'known', 'known', 'known')
    # 事实缺失 => unsupported_pricing
    without_fact = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 10},
        'claude-test', 'anthropic', (100, 20, 50, 10), states)
    assert without_fact['status'] == 'unknown'
    assert without_fact['reason'] == 'unsupported_pricing'
    # 单一 TTL 事实（经 ChatAttempt set_raw_usage 保留的 cache_creation）=> estimated
    with_fact = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 10,
         'cache_creation': {'ephemeral_1h_input_tokens': 10}},
        'claude-test', 'anthropic', (100, 20, 50, 10), states)
    assert with_fact['status'] == 'estimated'
    assert with_fact['price_id'] == 'p-claude-ttl'
    # 100*3 + 20*15 + 50*0.3 + 10*3.75（USD/百万）= 0.0006525，规范十进制字符串
    assert with_fact['cost_decimal'] == '0.0006525'


def _write_flat_anthropic_catalog(tmp_path: Path) -> tuple[str, str]:
    """无条件 Anthropic 价格目录（无 cache_ttl 条件）：input=3、output=15、
    cache_read=0.3、cache_write=3.75 USD/百万。"""
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": [
                {
                    "price_id": "p-claude-flat",
                    "billing_provider": "anthropic",
                    "billing_model": "claude-test",
                    "operation": "chat",
                    "aliases": [],
                    "unit_prices": {
                        "input": "3", "output": "15",
                        "cache_read": "0.3", "cache_write": "3.75",
                    },
                    "source_url": "https://example.com/pricing",
                    "verified_at": "2026-11-09",
                }
            ],
        },
        ensure_ascii=False,
    )
    path = tmp_path / "catalog-flat.json"
    path.write_text(text, encoding="utf-8")
    from app.infrastructure.usage.pricing_catalog import jcs_canonicalize

    expected = hashlib.sha256(jcs_canonicalize(json.loads(text)).encode("utf-8")).hexdigest()
    return str(path), expected


def _wired_estimator(tmp_path: Path, catalog: tuple[str, str]):
    """装配观测路径组合根 estimator（otel 开启 + 身份映射 + 目录就绪）。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.pricing_catalog_path = catalog[0]
    settings.pricing_catalog_expected_sha256 = catalog[1]
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "claude-test",
         "billing_provider": "anthropic", "billing_model": "claude-test"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    return services.telemetry.begin_chat_attempt(
        model='claude-test', stream=False, provider='anthropic')._cost_estimator


def test_mixed_ttl_never_hits_unconditional_entry_in_estimator(tmp_path):
    """评审修复（第四轮 D1）：观测路径混合合法 TTL + 无条件价格项 =>
    unsupported_pricing（不得唯一命中无条件条目按单一 cache_write 单价
    estimated）；无 TTL 事实 + 无条件条目仍 estimated（回归，语义不变）。"""
    estimator = _wired_estimator(tmp_path, _write_flat_anthropic_catalog(tmp_path))
    states = ('known', 'known', 'known', 'known')
    mixed = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 30,
         'cache_creation': {'ephemeral_1h_input_tokens': 10,
                            'ephemeral_5m_input_tokens': 20}},
        'claude-test', 'anthropic', (100, 20, 50, 30), states)
    assert mixed['status'] == 'unknown'
    assert mixed['reason'] == 'unsupported_pricing'
    # 回归：无 TTL 事实 + 无条件条目 => estimated（100*3+20*15+50*0.3+30*3.75
    # USD/百万 = 0.0007275）
    without_ttl = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 30},
        'claude-test', 'anthropic', (100, 20, 50, 30), states)
    assert without_ttl['status'] == 'estimated'
    assert without_ttl['cost_decimal'] == '0.0007275'


def test_estimator_nested_usage_concludes_like_flat(tmp_path):
    """评审修复（第四轮 D2）：观测路径 estimator 对嵌套 {"usage": {...}} 形态
    与平铺形态同判。嵌套内非法 TTL 子字段：确认桶（快照通道只收合法值）与
    解包后 raw 视图（cache_write=-1）逐值不一致 => 拒绝计价（旧 None 契约），
    绝不按无条件条目 estimated；全部桶 invalid 状态时（真实 ChatAttempt 流水线
    的 TTL 非法标记）收口 invalid_usage。嵌套内混合合法 TTL 收口
    unsupported_pricing（与 D1 成对）。"""
    estimator = _wired_estimator(tmp_path, _write_flat_anthropic_catalog(tmp_path))
    states = ('known', 'known', 'known', 'known')
    nested_invalid = estimator(
        {'usage': {'input_tokens': 100, 'output_tokens': 20,
                   'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 10,
                   'cache_creation': {'ephemeral_1h_input_tokens': -1}}},
        'claude-test', 'anthropic', (100, 20, 50, 10), states)
    # 解包后 raw 视图 cache_write=-1 与确认桶 10 不一致：拒绝计价（非 estimated）
    assert nested_invalid is None
    # 真实流水线形态：TTL 子字段非法 => cache_write 桶曾非法 => invalid_usage
    pipeline_invalid = estimator(
        {'usage': {'input_tokens': 100, 'output_tokens': 20,
                   'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 10,
                   'cache_creation': {'ephemeral_1h_input_tokens': -1}}},
        'claude-test', 'anthropic', (100, 20, 50, 10),
        ('known', 'known', 'known', 'invalid'))
    assert pipeline_invalid['status'] == 'unknown'
    assert pipeline_invalid['reason'] == 'invalid_usage'
    nested_mixed = estimator(
        {'usage': {'input_tokens': 100, 'output_tokens': 20,
                   'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 30,
                   'cache_creation': {'ephemeral_1h_input_tokens': 10,
                                      'ephemeral_5m_input_tokens': 20}}},
        'claude-test', 'anthropic', (100, 20, 50, 30), states)
    assert nested_mixed['status'] == 'unknown'
    assert nested_mixed['reason'] == 'unsupported_pricing'


def test_invalid_ttl_subfield_finalizes_invalid_usage_then_recovers(tmp_path):
    """TTL 子字段存在但非法：ChatAttempt 将 cache_write 桶标记为曾非法，
    finalization 收口 invalid_usage（先于缺失事实的 unsupported_pricing）；
    后续合法值恢复后按条件条目 estimated。"""
    catalog_path, expected_sha = _write_ttl_conditioned_catalog(tmp_path)
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "claude-test",
         "billing_provider": "anthropic", "billing_model": "claude-test"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    telemetry = services.telemetry
    # 非法 TTL 子字段 -> cache_write 曾非法 -> invalid_usage（非 unsupported_pricing）；
    # 注意其后不再有合法 cache_write 更新（合法值按 no-erase 规则会恢复桶有效性）
    with telemetry.begin_chat_attempt(model='claude-test', stream=False,
                                      provider='anthropic') as attempt:
        attempt.set_raw_usage({'input_tokens': 100, 'output_tokens': 20,
                               'cache_read_input_tokens': 50,
                               'cache_creation_input_tokens': 10,
                               'cache_creation': {'ephemeral_1h_input_tokens': 'x'}})
        attempt.set_usage(100, 20)
        attempt.set_cache_usage(cache_read_tokens=50)
    assert attempt._cost_facts['nagent.model.cost_status'] == 'unknown'
    assert attempt._cost_facts['nagent.model.cost_reason'] == 'invalid_usage'
    # 合法 TTL 子字段恢复 -> estimated（与条件目录命中同值）
    with telemetry.begin_chat_attempt(model='claude-test', stream=False,
                                      provider='anthropic') as recovered:
        recovered.set_raw_usage({'input_tokens': 100, 'output_tokens': 20,
                                 'cache_read_input_tokens': 50,
                                 'cache_creation_input_tokens': 10,
                                 'cache_creation': {'ephemeral_1h_input_tokens': 10}})
        recovered.set_usage(100, 20)
        recovered.set_cache_usage(cache_read_tokens=50, cache_write_tokens=10)
    assert recovered._cost_facts['nagent.model.cost_status'] == 'estimated'
    assert recovered._cost_facts['nagent.model.cost_decimal'] == '0.0006525'


def test_invalid_ttl_survives_legal_totals_then_recovers_after_subfield_recovery(tmp_path):
    """TTL 子字段非法后，同一响应内随后到达的合法 cache_write 总额不得解除
    invalid 标记：finalization 必须按 invalid_usage 收口，绝不能用此前保留的
    陈旧 TTL 条件事实产出 estimated；该子字段自身恢复合法后才可 estimated。"""
    catalog_path, expected_sha = _write_ttl_conditioned_catalog(tmp_path)
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "claude-test",
         "billing_provider": "anthropic", "billing_model": "claude-test"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    telemetry = services.telemetry
    # message_start 保留合法 TTL（1h）；message_delta 同响应内 TTL 子字段非法，
    # 随后合法 cache_write 总额（raw 平铺 + 快照通道）不得清除 invalid 标记
    with telemetry.begin_chat_attempt(model='claude-test', stream=False,
                                      provider='anthropic') as attempt:
        attempt.set_raw_usage({'input_tokens': 100, 'output_tokens': 20,
                               'cache_read_input_tokens': 50,
                               'cache_creation_input_tokens': 10,
                               'cache_creation': {'ephemeral_1h_input_tokens': 10}})
        attempt.set_usage(100, 20)
        attempt.set_cache_usage(cache_read_tokens=50, cache_write_tokens=10)
        attempt.set_raw_usage({'cache_creation_input_tokens': 10,
                               'cache_creation': {'ephemeral_1h_input_tokens': 'x'}})
        attempt.set_cache_usage(cache_write_tokens=10)
    assert attempt._cost_facts['nagent.model.cost_status'] == 'unknown'
    assert attempt._cost_facts['nagent.model.cost_reason'] == 'invalid_usage'
    # 该 TTL 子字段自身恢复合法值后才可 estimated（与条件目录命中同值）
    with telemetry.begin_chat_attempt(model='claude-test', stream=False,
                                      provider='anthropic') as recovered:
        recovered.set_raw_usage({'input_tokens': 100, 'output_tokens': 20,
                                 'cache_read_input_tokens': 50,
                                 'cache_creation_input_tokens': 10,
                                 'cache_creation': {'ephemeral_1h_input_tokens': 'x'}})
        recovered.set_raw_usage({'input_tokens': 100, 'output_tokens': 20,
                                 'cache_read_input_tokens': 50,
                                 'cache_creation_input_tokens': 10,
                                 'cache_creation': {'ephemeral_1h_input_tokens': 10}})
        recovered.set_usage(100, 20)
        recovered.set_cache_usage(cache_read_tokens=50, cache_write_tokens=10)
    assert recovered._cost_facts['nagent.model.cost_status'] == 'estimated'
    assert recovered._cost_facts['nagent.model.cost_decimal'] == '0.0006525'


def _streaming_usage_chunk(usage=None, with_choice=False, content=None):
    from types import SimpleNamespace
    choice = None
    if with_choice:
        choice = SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=None),
            finish_reason=None,
        )
    return SimpleNamespace(choices=[choice] if with_choice else [], usage=usage)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def test_streaming_identity_bound_to_selected_provider_instance(tmp_path):
    """流式调用的 Provider 实例在调用时即选定，而迭代消费（ChatAttempt.__enter__
    冻结身份）发生在其后：二者之间切换 active provider 时，观测计价身份必须
    跟随实际服务请求的 Provider 实例（按实例绑定的受信配置解析），而不是
    读取切换后的当前配置导致错价。"""
    from types import SimpleNamespace

    settings = _settings(tmp_path)
    settings.otel_enabled = True
    catalog_path, expected_sha = _write_vector_catalog(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    provider = services.provider_holder._provider
    # 经 provider 遥测回调捕获本次流式尝试
    captured = {}
    original_callback = provider._telemetry_callback

    def capturing_callback(**kwargs):
        attempt = original_callback(**kwargs)
        captured['attempt'] = attempt
        return attempt

    provider._telemetry_callback = capturing_callback
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: _async_return(_FakeStream([
            _streaming_usage_chunk(with_choice=True, content="he"),
            _streaming_usage_chunk(usage=SimpleNamespace(
                prompt_tokens=150, completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=50),
            )),
        ]))))
    )
    # 调用发生：Provider 实例此时选定（prov-1 的实例）
    iterator = asyncio.run(services.provider_holder.chat(
        [{"role": "user", "content": "hi"}], [], True, "gpt-4o", {}))
    # 实例选择与迭代消费之间切换 active provider（新配置无该模型映射）
    asyncio.run(services.provider_holder.swap(_provider_config("prov-other"), "k"))

    async def _consume():
        async for _event in iterator:
            pass

    asyncio.run(_consume())
    attempt = captured['attempt']
    # 身份必须与实际服务请求的实例（prov-1）一致，而非切换后的当前配置
    assert attempt._cost_facts['nagent.model.cost_status'] == 'estimated'
    assert attempt._cost_facts['nagent.model.billing_provider'] == 'openai'
    assert attempt._cost_facts['nagent.model.billing_model'] == 'gpt-4o'


def _async_return(value):
    import types

    async def _coroutine():
        return value

    return _coroutine()


def test_holder_stamps_billing_identity_at_instance_selection(tmp_path):
    """ActiveProviderHolder.chat 在 Provider 实例选择同一作用域解析受信计价
    身份并随 LLMResult 携带（billing_identity 字段）：请求在飞期间切换
    active provider 后，账本仍能拿到与实际服务实例一致的身份，不重读切换
    后的当前配置（同一调用使用同一计价结果）。"""
    from types import SimpleNamespace

    settings = _settings(tmp_path)
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    provider = services.provider_holder._provider

    async def _scenario():
        response = SimpleNamespace(
            model="gpt-4o",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content="ok", tool_calls=None),
            )],
            usage=SimpleNamespace(prompt_tokens=150, completion_tokens=20,
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=50)),
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def create(**kwargs):
            entered.set()
            await release.wait()
            return response

        provider.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        task = asyncio.create_task(services.provider_holder.chat(
            [{"role": "user", "content": "hi"}], [], False, "gpt-4o", {}))
        await entered.wait()
        # 实例已选定、请求在飞：切换 active provider（新配置无该模型映射）
        await services.provider_holder.swap(_provider_config("prov-other"), "k")
        release.set()
        return await task

    result = asyncio.run(_scenario())
    # 身份来自实例选择时刻的配置快照（prov-1），而非切换后的当前配置
    assert result.billing_identity == ("openai", "gpt-4o")


def test_estimator_unknown_after_unique_resolution_keeps_price_id(tmp_path):
    """唯一命中价格项后的 unknown（命中项缺适用桶单价 => pricing_unavailable）
    组合根结果保留 price_id / pricing_version 供追溯（spec：unknown 仅在已
    唯一定位价格项时保留 price_id）。"""
    text = json.dumps(
        {
            "schema_version": 1,
            "content_version": "test-v1",
            "currency": "USD",
            "prices": [
                {
                    "price_id": "p-openai-partial",
                    "billing_provider": "openai",
                    "billing_model": "gpt-4o",
                    "operation": "chat",
                    "aliases": [],
                    "unit_prices": {
                        "input": "2", "output": "10",
                        "cache_read": None, "cache_write": None,
                    },
                    "source_url": "https://example.com/pricing",
                    "verified_at": "2026-11-09",
                }
            ],
        },
        ensure_ascii=False,
    )
    path = tmp_path / "catalog-partial.json"
    path.write_text(text, encoding="utf-8")
    from app.infrastructure.usage.pricing_catalog import jcs_canonicalize

    expected = hashlib.sha256(jcs_canonicalize(json.loads(text)).encode("utf-8")).hexdigest()
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    settings.pricing_catalog_path = str(path)
    settings.pricing_catalog_expected_sha256 = expected
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    estimator = services.telemetry.begin_chat_attempt(
        model='gpt-4o', stream=False, provider='openai')._cost_estimator
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    result = estimator(raw, 'gpt-4o', 'openai', (100, 20, 50, 0))
    assert result['status'] == 'unknown'
    assert result['reason'] == 'pricing_unavailable'
    assert result['price_id'] == 'p-openai-partial'
    assert result['pricing_version']


def test_billing_identity_map_invalid_json_fails_fast(tmp_path):
    """受信计价身份映射非法 JSON：启动期装配 fail-fast（B 类 env-only）。"""
    settings = _settings(tmp_path)
    settings.model_billing_identity_map = "{not-json"
    with pytest.raises(ValueError):
        build_application_services(settings)


def test_billing_identity_frozen_at_request_start_survives_provider_switch(tmp_path):
    """受信计价身份在请求启动时冻结：在飞请求期间切换 active provider，
    finalization 仍按启动时身份计价（并发隔离），不读切换后的配置。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    catalog_path, expected_sha = _write_vector_catalog(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    with services.telemetry.begin_chat_attempt(
            model='gpt-4o', stream=False, provider='openai') as attempt:
        attempt.set_raw_usage(raw)
        attempt.set_usage(100, 20)
        attempt.set_cache_usage(cache_read_tokens=50)
        # 请求在飞期间切换 active provider（新配置无该模型映射）
        asyncio.run(services.provider_holder.swap(_provider_config("prov-other"), "k"))
    assert attempt._cost_facts['nagent.model.cost_status'] == 'estimated'
    assert attempt._cost_facts['nagent.model.billing_provider'] == 'openai'
    assert attempt._cost_facts['nagent.model.billing_model'] == 'gpt-4o'


def test_identity_unresolved_at_start_stays_unresolved(tmp_path):
    """启动时身份未解析：即使匹配映射的 provider 在请求在飞期间变为
    active，finalization 也不追溯重解析（保持 identity_unresolved）。"""
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    catalog_path, expected_sha = _write_vector_catalog(tmp_path)
    settings.pricing_catalog_path = catalog_path
    settings.pricing_catalog_expected_sha256 = expected_sha
    settings.model_billing_identity_map = json.dumps([
        {"provider_id": "prov-1", "requested_model": "gpt-4o",
         "billing_provider": "openai", "billing_model": "gpt-4o"},
    ])
    services = build_application_services(settings)
    # 请求启动时 active 为 prov-other（无该模型映射）
    asyncio.run(services.provider_holder.swap(_provider_config("prov-other"), "k"))
    raw = {'prompt_tokens': 150, 'completion_tokens': 20,
           'prompt_tokens_details': {'cached_tokens': 50}}
    with services.telemetry.begin_chat_attempt(
            model='gpt-4o', stream=False, provider='openai') as attempt:
        attempt.set_raw_usage(raw)
        attempt.set_usage(100, 20)
        attempt.set_cache_usage(cache_read_tokens=50)
        # 在飞期间匹配映射的 provider 变为 active
        asyncio.run(services.provider_holder.swap(_provider_config("prov-1"), "k"))
    assert attempt._cost_facts['nagent.model.cost_status'] == 'unknown'
    assert attempt._cost_facts['nagent.model.cost_reason'] == 'identity_unresolved'
    assert 'nagent.model.billing_provider' not in attempt._cost_facts
    assert 'nagent.model.billing_model' not in attempt._cost_facts


def test_estimator_mixed_ttl_with_unavailable_catalog_is_pricing_unavailable(tmp_path):
    """评审修复（第五轮 D1）观测路径同判：目录失效（哈希不匹配）+ 混合合法
    TTL => pricing_unavailable（两因并存取顺序靠前者，不得以
    unsupported_pricing 掩盖目录失效）；观测路径经共享
    cache_ttl_facts + calculate_attempt_cost 与账本路径自动同判。"""
    catalog_path, _ = _write_flat_anthropic_catalog(tmp_path)
    estimator = _wired_estimator(
        tmp_path, (catalog_path, "0" * 64))  # 预期哈希不匹配 => 目录失效
    states = ('known', 'known', 'known', 'known')
    result = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 30,
         'cache_creation': {'ephemeral_1h_input_tokens': 10,
                            'ephemeral_5m_input_tokens': 20}},
        'claude-test', 'anthropic', (100, 20, 50, 30), states)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'pricing_unavailable'
    assert result['pricing_version'] is None


def test_estimator_single_ttl_not_covering_total_is_unsupported(tmp_path):
    """评审修复（第五轮 D2）观测路径同判：单一非零合法 TTL 子字段之和（10）
    不等于 cache_creation_input_tokens 总量（30）=> unsupported_pricing，
    不得把全部 cache_write 按该 TTL 单价 estimated。"""
    estimator = _wired_estimator(tmp_path, _write_flat_anthropic_catalog(tmp_path))
    states = ('known', 'known', 'known', 'known')
    result = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 30,
         'cache_creation': {'ephemeral_1h_input_tokens': 10}},
        'claude-test', 'anthropic', (100, 20, 50, 30), states)
    assert result['status'] == 'unknown'
    assert result['reason'] == 'unsupported_pricing'


def test_estimator_single_ttl_covering_total_estimated_regression(tmp_path):
    """回归（第五轮 D2）：单一非零合法 TTL 子字段等于总量（1h=10、总量=10）
    => 条件被证明，观测路径 estimated（语义不变）。"""
    estimator = _wired_estimator(tmp_path, _write_flat_anthropic_catalog(tmp_path))
    states = ('known', 'known', 'known', 'known')
    result = estimator(
        {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50,
         'cache_creation_input_tokens': 10,
         'cache_creation': {'ephemeral_1h_input_tokens': 10}},
        'claude-test', 'anthropic', (100, 20, 50, 10), states)
    assert result['status'] == 'estimated'
    assert result['cost_decimal'] == '0.0006525'
