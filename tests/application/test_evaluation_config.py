"""T20: 评估 C 类配置服务测试（EvaluationConfigService；application 层）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.application.evaluation_config import (
    EvaluationConfigService,
    evaluation_config_from_env,
)
from app.config import Settings
from app.domain.evaluation.config import (
    EvaluationConfigConflictError,
    EvaluationConfigValidationError,
)


def _settings(tmp_path, **overrides) -> Settings:
    kwargs = dict(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        evaluation_worker_max_concurrency=4,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_service(tmp_path, **overrides) -> EvaluationConfigService:
    from app.infrastructure.evaluation.config_store import (
        SqliteEvaluationConfigStore,
    )

    settings = _settings(tmp_path, **overrides)
    store = SqliteEvaluationConfigStore(str(tmp_path / "evaluations.db"))
    return EvaluationConfigService(settings, store)


# ---------------------------------------------------------------------------
# env 基线
# ---------------------------------------------------------------------------


def test_env_baseline_projects_b_fields(tmp_path):
    settings = _settings(
        tmp_path,
        evaluation_capture_enabled=True,
        evaluation_sampling_rate=0.25,
        evaluation_failure_sampling_rate=0.5,
        evaluation_sampling_policy_version="capture-sampling-v2",
        evaluation_worker_concurrency=2,
        evaluation_daily_call_ceiling=100,
    )
    cfg = evaluation_config_from_env(settings)
    assert cfg.evaluation_enabled is True
    assert cfg.evaluation_sampling_rate == 0.25
    assert cfg.evaluation_failure_sampling_rate == 0.5
    assert cfg.evaluation_sampling_policy_version == "capture-sampling-v2"
    assert cfg.evaluation_worker_concurrency == 2
    assert cfg.evaluation_daily_call_ceiling == 100


def test_invalid_env_baseline_fails_fast(tmp_path):
    # env 基线越界（并发超过 B 上限）：pydantic 启动校验先于服务构造
    # 拒绝（两层同源：Settings model_validator + 服务构造期防御校验）
    with pytest.raises(ValidationError, match="evaluation_worker_concurrency"):
        _settings(
            tmp_path,
            evaluation_worker_concurrency=8,
            evaluation_worker_max_concurrency=4,
        )


# ---------------------------------------------------------------------------
# 运行面 current()
# ---------------------------------------------------------------------------


async def test_current_no_row_returns_env_base(tmp_path):
    service = _make_service(tmp_path)
    cfg = await service.current()
    assert cfg.evaluation_enabled is False
    assert cfg.evaluation_sampling_rate == 0.0


async def test_current_after_update_returns_merged(tmp_path):
    service = _make_service(tmp_path)
    resolved = await service.update(
        {"evaluation_enabled": True, "evaluation_sampling_rate": 0.05},
        expected_version=0,
        updated_by="tester",
    )
    assert resolved.version == 1
    cfg = await service.current()
    assert cfg.evaluation_enabled is True
    assert cfg.evaluation_sampling_rate == 0.05
    # 未覆盖字段跟随 env
    assert cfg.evaluation_worker_concurrency == 1


def test_current_sync_matches_async(tmp_path):
    import asyncio

    service = _make_service(tmp_path)
    asyncio.run(
        service.update({"evaluation_enabled": True}, 0, "tester")
    )
    cfg = service.current_sync()
    assert cfg.evaluation_enabled is True


def test_current_sync_store_failure_falls_back(tmp_path):
    from app.infrastructure.evaluation.config_store import (
        SqliteEvaluationConfigStore,
    )

    settings = _settings(tmp_path)
    store = SqliteEvaluationConfigStore(str(tmp_path / "evaluations.db"))
    service = EvaluationConfigService(settings, store)
    # 销毁 get_sync（模拟读取失败）-> 回退 last-known-good，不抛错
    store.get_sync = None  # type: ignore[assignment]
    cfg = service.current_sync()
    assert cfg.evaluation_enabled is False


def test_current_sync_store_raises_falls_back(tmp_path):
    """N1: get_sync 抛异常路径同样回退 last-known-good，不抛错。"""
    service = _make_service(tmp_path)

    def _boom():
        raise RuntimeError("db locked")

    service._store.get_sync = _boom  # type: ignore[method-assign]
    cfg = service.current_sync()
    assert cfg.evaluation_enabled is False


async def test_current_and_current_sync_same_fallback_semantics(tmp_path):
    """m3 防漂移：current/current_sync 共享 _resolve_stored，回退口径
    一致——store 失败回退 last-known-good、覆盖非法回退 env 基线，两
    入口结果恒等。"""
    service = _make_service(tmp_path)
    await service.update(
        {"evaluation_worker_concurrency": 4}, 0, "tester"
    )

    # 合法覆盖：两入口同值（最后已知良好 = 4）
    assert (await service.current()).evaluation_worker_concurrency == 4
    assert service.current_sync().evaluation_worker_concurrency == 4

    # 模拟 env 基线收紧使存量覆盖越界：两入口一致回退 env 基线
    service._worker_max_concurrency = 2
    fallback_async = await service.current()
    fallback_sync = service.current_sync()
    assert fallback_async == fallback_sync
    assert fallback_async.evaluation_worker_concurrency == 1

    # store 读取失败（get 抛错 / get_sync 抛错）：两入口一致回退
    async def _boom_async():
        raise RuntimeError("db locked")

    def _boom_sync():
        raise RuntimeError("db locked")

    service._store.get = _boom_async  # type: ignore[method-assign]
    service._store.get_sync = _boom_sync  # type: ignore[method-assign]
    assert await service.current() == service.current_sync()


# ---------------------------------------------------------------------------
# 管理面 update / CAS / 校验
# ---------------------------------------------------------------------------


async def test_update_unknown_field_rejected(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(EvaluationConfigValidationError, match="未知覆盖键"):
        await service.update({"unknown_key": 1}, 0, "tester")


async def test_update_cas_version_conflict(tmp_path):
    service = _make_service(tmp_path)
    await service.update({"evaluation_enabled": True}, 0, "tester")
    with pytest.raises(EvaluationConfigConflictError):
        await service.update({"evaluation_enabled": False}, 0, "tester")


async def test_update_concurrency_over_b_ceiling_rejected(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(EvaluationConfigValidationError, match="B 类部署上限"):
        await service.update(
            {"evaluation_worker_concurrency": 5}, 0, "tester"
        )


async def test_update_rate_out_of_range_rejected(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(EvaluationConfigValidationError, match="0, 1"):
        await service.update({"evaluation_sampling_rate": 1.5}, 0, "tester")


async def test_update_bad_updated_by_rejected(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(EvaluationConfigValidationError, match="updated_by"):
        await service.update({"evaluation_enabled": True}, 0, "")


async def test_update_empty_patch_rejected(tmp_path):
    service = _make_service(tmp_path)
    with pytest.raises(EvaluationConfigValidationError, match="非空"):
        await service.update({}, 0, "tester")


async def test_update_type_garbage_rejected(tmp_path):
    service = _make_service(tmp_path)
    for bad_value in ("yes", 1, None):
        with pytest.raises(EvaluationConfigValidationError):
            await service.update(
                {"evaluation_enabled": bad_value}, 0, "tester"
            )
    with pytest.raises(EvaluationConfigValidationError):
        await service.update(
            {"evaluation_worker_concurrency": True}, 0, "tester"
        )


async def test_get_resolved_reports_overrides_and_metadata(tmp_path):
    service = _make_service(tmp_path)
    await service.update(
        {"evaluation_enabled": True, "evaluation_daily_call_ceiling": 50},
        0,
        "tester",
    )
    resolved = await service.get_resolved()
    assert resolved.version == 1
    assert resolved.overridden_fields == (
        "evaluation_enabled",
        "evaluation_daily_call_ceiling",
    )
    assert resolved.updated_by == "tester"


async def test_replace_overrides_full_set(tmp_path):
    service = _make_service(tmp_path)
    await service.update({"evaluation_enabled": True}, 0, "tester")
    resolved = await service.replace_overrides({}, 1, "tester")
    # 空对象 = 无覆盖（配置迁移合法输入），全部跟随 env
    assert resolved.version == 2
    assert resolved.overridden_fields == ()
    cfg = await service.current()
    assert cfg.evaluation_enabled is False


async def test_update_rejects_zero_ceiling_when_judge_configured(tmp_path):
    """24 项三方审阅 #15：Judge Provider 已配置时，C 类运行时不得把日
    调用预算改为 0（热读 0 投影 None 后 worker 预算预留失效）。"""
    from app.infrastructure.evaluation.config_store import (
        SqliteEvaluationConfigStore,
    )

    settings = _settings(
        tmp_path,
        evaluation_judge_provider_base_url="http://judge",
        evaluation_judge_provider_api_key="k",
        evaluation_judge_provider_model="m",
        # 启动期守卫同判据：Judge 已配置时 env 基线预算必须为正
        evaluation_daily_call_ceiling=100,
    )
    store = SqliteEvaluationConfigStore(str(tmp_path / "evaluations.db"))
    service = EvaluationConfigService(settings, store)
    with pytest.raises(EvaluationConfigValidationError):
        await service.update(
            {"evaluation_daily_call_ceiling": 0},
            expected_version=0,
            updated_by="tester",
        )
    # 未配置 Judge：0 仍合法（0 = 不设上限，既有语义）
    settings_no_judge = _settings(tmp_path)
    store2 = SqliteEvaluationConfigStore(str(tmp_path / "evaluations2.db"))
    service2 = EvaluationConfigService(settings_no_judge, store2)
    resolved = await service2.update(
        {"evaluation_daily_call_ceiling": 0},
        expected_version=0,
        updated_by="tester",
    )
    assert resolved.config.evaluation_daily_call_ceiling == 0
