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
    EvaluationConfigStoreError,
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


class TestJudgeProviderIdEnvBaseline:
    """evaluation_judge_provider_id：env 基线恒为空串（纯运行时覆盖字段）。"""

    def test_env_baseline_provider_id_default_empty(self, tmp_path):
        cfg = evaluation_config_from_env(_settings(tmp_path))
        assert cfg.evaluation_judge_provider_id == ""
        # env 侧 judge provider model 不再投影到该字段
        cfg_with_env = evaluation_config_from_env(
            _settings(tmp_path, evaluation_judge_provider_model="env-model")
        )
        assert cfg_with_env.evaluation_judge_provider_id == ""

    async def test_patch_string_sets_and_empty_clears(self, tmp_path):
        # env 基线预算为正：单独 patch provider_id 不触发统一预算约束
        service = _make_service(
            tmp_path, evaluation_daily_call_ceiling=100
        )
        resolved = await service.update(
            {
                "evaluation_enabled": True,
                "evaluation_judge_provider_id": "provider-a",
            },
            expected_version=0,
            updated_by="tester",
        )
        assert resolved.config.evaluation_judge_provider_id == "provider-a"
        assert "evaluation_judge_provider_id" in resolved.overridden_fields

        # 空串 = 清除该覆盖键，回落 env 基线（空串）；其它覆盖不受影响
        cleared = await service.update(
            {"evaluation_judge_provider_id": ""},
            expected_version=1,
            updated_by="tester",
        )
        assert "evaluation_judge_provider_id" not in cleared.overridden_fields
        assert "evaluation_enabled" in cleared.overridden_fields
        assert cleared.config.evaluation_judge_provider_id == ""
        assert cleared.config.evaluation_enabled is True

    async def test_patch_invalid_string_rejected(self, tmp_path):
        service = _make_service(tmp_path)
        with pytest.raises(EvaluationConfigValidationError):
            await service.update(
                {"evaluation_judge_provider_id": " p"}, 0, "tester"
            )


class TestJudgeProviderIdPersistence:
    """evaluation_judge_provider_id 落库/恢复/CAS/清除/last-known-good。"""

    def _make(self, tmp_path, db_name="evaluations.db", **overrides):
        from app.infrastructure.evaluation.config_store import (
            SqliteEvaluationConfigStore,
        )

        settings = _settings(tmp_path, **overrides)
        store = SqliteEvaluationConfigStore(str(tmp_path / db_name))
        return EvaluationConfigService(settings, store)

    async def test_first_write_persists_and_restores(self, tmp_path):
        service = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        resolved = await service.update(
            {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
        )
        assert resolved.version == 1

        # 重建 store/service（同库）后值恢复
        rebuilt = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        assert (
            (await rebuilt.current()).evaluation_judge_provider_id
            == "provider-a"
        )
        r2 = await rebuilt.get_resolved()
        assert r2.config.evaluation_judge_provider_id == "provider-a"
        assert r2.overridden_fields == ("evaluation_judge_provider_id",)

    async def test_cas_conflict_from_stale_client(self, tmp_path):
        client_a = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        client_b = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        await client_a.update(
            {"evaluation_judge_provider_id": "provider-a"}, 0, "a"
        )
        with pytest.raises(EvaluationConfigConflictError):
            await client_b.update(
                {"evaluation_judge_provider_id": "provider-b"}, 0, "b"
            )

    async def test_empty_clear_removes_key_from_db_row(self, tmp_path):
        import json as _json
        import sqlite3

        # env 基线预算为正：provider_id 覆盖不触发统一预算约束（ceiling
        # 只在 env 基线、未落库，清除后 DB 行仍只含 enabled）
        service = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        await service.update(
            {
                "evaluation_enabled": True,
                "evaluation_judge_provider_id": "provider-a",
            },
            0,
            "tester",
        )
        await service.update(
            {"evaluation_judge_provider_id": ""}, 1, "tester"
        )

        with sqlite3.connect(tmp_path / "evaluations.db") as conn:
            (config_json,) = conn.execute(
                "SELECT config_json FROM evaluation_config WHERE id = 1"
            ).fetchone()
        data = _json.loads(config_json)
        assert "evaluation_judge_provider_id" not in data
        assert data == {"evaluation_enabled": True}

    async def test_current_and_get_resolved_follow_env_baseline(
        self, tmp_path
    ):
        service = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        await service.update(
            {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
        )
        await service.update(
            {"evaluation_judge_provider_id": ""}, 1, "tester"
        )
        assert (await service.current()).evaluation_judge_provider_id == ""
        resolved = await service.get_resolved()
        assert resolved.config.evaluation_judge_provider_id == ""
        assert "evaluation_judge_provider_id" not in resolved.overridden_fields

    async def test_current_keeps_last_known_good_on_store_error(
        self, tmp_path
    ):
        """清除覆盖与读取失败是两回事：get 抛错时 current() 保留
        last-known-good（仍含已生效覆盖），不回退 env 基线。"""
        service = self._make(tmp_path, evaluation_daily_call_ceiling=100)
        await service.update(
            {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
        )
        assert (await service.current()).evaluation_judge_provider_id == (
            "provider-a"
        )

        async def _boom():
            raise RuntimeError("db locked")

        service._store.get = _boom  # type: ignore[method-assign]
        cfg = await service.current()
        assert cfg.evaluation_judge_provider_id == "provider-a"


class TestLegacyJudgeModelReadNormalization:
    """读时归一（legacy 迁移）：仅旧键 evaluation_judge_model 在存储读
    路径剥离，不写回存储；PATCH/replace 载荷仍严格拒绝旧键。"""

    LEGACY_KEY = "evaluation_judge_model"
    NEW_KEY = "evaluation_judge_provider_id"

    def _store(self, tmp_path, db_name="evaluations.db", **kwargs):
        from app.infrastructure.evaluation.config_store import (
            SqliteEvaluationConfigStore,
        )

        return SqliteEvaluationConfigStore(str(tmp_path / db_name), **kwargs)

    def _write_raw(self, tmp_path, config_obj, db_name="evaluations.db"):
        import json as _json
        import sqlite3

        store = self._store(tmp_path, db_name)  # 确保表已建
        with sqlite3.connect(tmp_path / db_name) as conn:
            conn.execute(
                "INSERT INTO evaluation_config (id, config_json, version, "
                "updated_at, updated_by) VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json, "
                "version = excluded.version, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (
                    _json.dumps(config_obj, ensure_ascii=False),
                    7,
                    "2026-01-01T00:00:00+00:00",
                    "legacy-writer",
                ),
            )
        return store

    def _read_raw_row(self, tmp_path, db_name="evaluations.db"):
        import sqlite3

        with sqlite3.connect(tmp_path / db_name) as conn:
            return conn.execute(
                "SELECT config_json, version, updated_at, updated_by "
                "FROM evaluation_config WHERE id = 1"
            ).fetchone()

    async def test_legacy_only_old_key_reads_as_empty(self, tmp_path):
        """仅旧键 -> 读出为空覆盖（不抛），且不写回存储。"""
        store = self._write_raw(tmp_path, {self.LEGACY_KEY: "m1"})
        for stored in (await store.get(), store.get_sync()):
            assert stored is not None
            assert stored.version == 7
            assert stored.updated_by == "legacy-writer"
            assert stored.overrides.to_dict() == {}
        # 读时无写入：原始 JSON/version/updated_at/updated_by 不变
        config_json, version, updated_at, updated_by = self._read_raw_row(
            tmp_path
        )
        import json as _json

        assert _json.loads(config_json) == {self.LEGACY_KEY: "m1"}
        assert version == 7
        assert updated_at == "2026-01-01T00:00:00+00:00"
        assert updated_by == "legacy-writer"

    async def test_legacy_old_and_new_keys_keeps_new(self, tmp_path):
        store = self._write_raw(
            tmp_path, {self.LEGACY_KEY: "old", self.NEW_KEY: "provider-a"}
        )
        for stored in (await store.get(), store.get_sync()):
            assert stored is not None
            assert stored.overrides.to_dict() == {self.NEW_KEY: "provider-a"}
        # 旧键仍保留在原始 JSON 中（读时不写回）
        import json as _json

        config_json, _, _, _ = self._read_raw_row(tmp_path)
        assert self.LEGACY_KEY in _json.loads(config_json)

    async def test_legacy_unknown_other_key_still_rejected(self, tmp_path):
        store = self._write_raw(tmp_path, {self.LEGACY_KEY: "m1", "nope": 1})
        with pytest.raises(EvaluationConfigStoreError, match="未知覆盖键"):
            await store.get()
        with pytest.raises(EvaluationConfigStoreError, match="未知覆盖键"):
            store.get_sync()

    async def test_legacy_bad_json_still_errors(self, tmp_path):
        import sqlite3

        self._store(tmp_path)
        with sqlite3.connect(tmp_path / "evaluations.db") as conn:
            conn.execute(
                "INSERT INTO evaluation_config (id, config_json, version, "
                "updated_at, updated_by) VALUES (1, ?, 1, ?, ?)",
                ("not json", "2026-01-01T00:00:00+00:00", "legacy-writer"),
            )
        store = self._store(tmp_path, initialize=False)
        with pytest.raises(EvaluationConfigStoreError, match="不是合法 JSON"):
            await store.get()
        with pytest.raises(EvaluationConfigStoreError, match="不是合法 JSON"):
            store.get_sync()

    async def test_legacy_cas_save_physically_removes_old_key(self, tmp_path):
        """读时归一后正常 CAS 保存 -> 原始 JSON 物理移除旧键。"""
        from app.domain.evaluation.config import EvaluationConfigOverrides

        store = self._write_raw(tmp_path, {self.LEGACY_KEY: "m1"})
        stored = await store.get()
        assert stored is not None
        assert stored.version == 7
        await store.save(
            EvaluationConfigOverrides(
                evaluation_enabled=True, **{self.NEW_KEY: "provider-a"}
            ),
            expected_version=stored.version,
            updated_by="tester",
        )
        import json as _json

        config_json, version, updated_at, updated_by = self._read_raw_row(
            tmp_path
        )
        data = _json.loads(config_json)
        assert self.LEGACY_KEY not in data
        assert data == {"evaluation_enabled": True, self.NEW_KEY: "provider-a"}
        assert version == 8
        assert updated_by == "tester"

    async def test_replace_overrides_legacy_key_rejected(self, tmp_path):
        """replace_overrides 新载荷含旧键必须拒绝，不走存储兼容路径。"""
        service = _make_service(tmp_path)
        with pytest.raises(
            EvaluationConfigValidationError, match="未知覆盖键"
        ):
            await service.replace_overrides(
                {self.LEGACY_KEY: "m1"}, 0, "tester"
            )


class TestUnifiedJudgeBudgetCondition:
    """统一预算条件（domain validate_evaluation_config 单一来源）：存在
    任一 Judge 供给来源（env 三字段就绪的 judge_configured，或运行时
    覆盖 evaluation_judge_provider_id 非空）时日预算必须为正。

    存量覆盖（provider_id 非空 + ceiling=0）造数用原始 SQL 写行：经
    CAS update 造数在统一条件生效后会被管理面校验拦截，无法模拟
    "规则生效前落库"的存量数据。
    """

    def _write_raw_row(self, tmp_path, config_obj, db_name="evaluations.db"):
        import json as _json
        import sqlite3

        from app.infrastructure.evaluation.config_store import (
            SqliteEvaluationConfigStore,
        )

        SqliteEvaluationConfigStore(str(tmp_path / db_name))  # 确保表已建
        with sqlite3.connect(tmp_path / db_name) as conn:
            conn.execute(
                "INSERT INTO evaluation_config (id, config_json, version, "
                "updated_at, updated_by) VALUES (1, ?, 7, ?, ?)",
                (
                    _json.dumps(config_obj, ensure_ascii=False),
                    "2026-01-01T00:00:00+00:00",
                    "pre-rule-writer",
                ),
            )

    async def test_runtime_falls_back_when_provider_id_overrides_zero_ceiling(
        self, tmp_path
    ):
        """运行面：env 未配置 Judge 时，存量覆盖 provider_id 非空 +
        ceiling=0 按既有非法覆盖口径回退 env 基线（last-known-good），
        不抛错；current/current_sync 口径一致。"""
        self._write_raw_row(
            tmp_path,
            {
                "evaluation_judge_provider_id": "provider-a",
                "evaluation_daily_call_ceiling": 0,
            },
        )
        service = _make_service(tmp_path)
        cfg = await service.current()
        # 回退 env 基线：provider_id 恒空串、跟随 env ceiling
        assert cfg.evaluation_judge_provider_id == ""
        assert cfg.evaluation_daily_call_ceiling == 0
        sync_cfg = service.current_sync()
        assert sync_cfg == cfg

    async def test_runtime_picks_up_repaired_override_after_invalid_row(
        self, tmp_path
    ):
        """运行面：存量覆盖非法（provider_id 非空 + ceiling=0）触发回退
        后，修复预算（保留 provider_id、ceiling 改正）即恢复读取，不需
        清除覆盖。"""
        self._write_raw_row(
            tmp_path,
            {
                "evaluation_judge_provider_id": "provider-a",
                "evaluation_daily_call_ceiling": 0,
            },
        )
        service = _make_service(tmp_path)
        assert (await service.current()).evaluation_judge_provider_id == ""

        # 修复存量数据：CAS save 以 store 层版本写合法覆盖
        from app.domain.evaluation.config import EvaluationConfigOverrides

        stored = await service._store.get()
        assert stored is not None
        await service._store.save(
            EvaluationConfigOverrides(
                evaluation_judge_provider_id="provider-a",
                evaluation_daily_call_ceiling=10,
            ),
            expected_version=stored.version,
            updated_by="tester",
        )
        fixed = await service.current()
        assert fixed.evaluation_judge_provider_id == "provider-a"
        assert fixed.evaluation_daily_call_ceiling == 10

    async def test_get_resolved_rejects_provider_id_with_zero_ceiling(
        self, tmp_path
    ):
        """严格管理面：get_resolved 读取同一存量覆盖必须抛校验错误。"""
        self._write_raw_row(
            tmp_path,
            {
                "evaluation_judge_provider_id": "provider-a",
                "evaluation_daily_call_ceiling": 0,
            },
        )
        service = _make_service(tmp_path)
        with pytest.raises(EvaluationConfigValidationError):
            await service.get_resolved()

    async def test_update_provider_id_requires_positive_ceiling(self, tmp_path):
        """管理面 update：env ceiling=0 时单独 patch provider_id 非空被
        拒绝；env ceiling 为正时同一 patch 合法（统一条件经调用点传布尔
        触发，不在调用点复制条件）。"""
        service = _make_service(tmp_path)
        with pytest.raises(EvaluationConfigValidationError):
            await service.update(
                {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
            )
        service_positive = _make_service(
            tmp_path, evaluation_daily_call_ceiling=100
        )
        resolved = await service_positive.update(
            {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
        )
        assert resolved.config.evaluation_judge_provider_id == "provider-a"
        assert resolved.config.evaluation_daily_call_ceiling == 100

    async def test_replace_overrides_provider_id_requires_positive_ceiling(
        self, tmp_path
    ):
        """管理面 replace_overrides 与 update 同规则。"""
        service = _make_service(tmp_path)
        with pytest.raises(EvaluationConfigValidationError):
            await service.replace_overrides(
                {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
            )
        # 清除 provider_id（空串剥离）后回到 env 基线：ceiling=0 仍合法
        resolved = await service.replace_overrides({}, 0, "tester")
        assert resolved.config.evaluation_judge_provider_id == ""
        assert resolved.config.evaluation_daily_call_ceiling == 0


class TestJudgeReferenceBudgetSamePatch:
    """T4 复核：首次设置 Provider 引用且同 PATCH 设正数预算——env 基线
    ceiling=0 时，单一 PATCH 同时携带 provider_id 与正数 ceiling 合法
    （统一预算条件对合并后候选配置整体判定，不在调用点复制条件）。"""

    async def test_first_reference_with_positive_ceiling_same_patch(self, tmp_path):
        service = _make_service(tmp_path)
        resolved = await service.update(
            {
                "evaluation_judge_provider_id": "provider-a",
                "evaluation_daily_call_ceiling": 10,
            },
            0,
            "tester",
        )
        assert resolved.config.evaluation_judge_provider_id == "provider-a"
        assert resolved.config.evaluation_daily_call_ceiling == 10
        assert resolved.overridden_fields == (
            "evaluation_daily_call_ceiling",
            "evaluation_judge_provider_id",
        )
        # 运行面热读立即生效
        current = await service.current()
        assert current.evaluation_judge_provider_id == "provider-a"
        assert current.evaluation_daily_call_ceiling == 10

    async def test_same_patch_without_ceiling_rejected(self, tmp_path):
        """同 PATCH 只带 provider_id（env ceiling=0）仍拒绝：引用无预算
        的非法组合不允许落库。"""
        service = _make_service(tmp_path)
        with pytest.raises(EvaluationConfigValidationError):
            await service.update(
                {"evaluation_judge_provider_id": "provider-a"}, 0, "tester"
            )
