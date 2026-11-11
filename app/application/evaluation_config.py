"""评估 C 类运行时配置服务（T20；复用 task_config 模式）。

分层解析：代码默认 -> env 基线（B 类启动期值投影）-> DB 覆盖 ->
解析配置。运行面 ``current()`` 永不抛错（store 读取失败或覆盖非法
回退 last-known-good）；管理面 ``get_resolved()``/``update()`` 严格
模式（错误传播，CAS 乐观锁，未知键拒绝）。

B 类 ``evaluation_worker_max_concurrency`` 是 C 并发的部署上限：
跨字段约束在 domain ``validate_evaluation_config`` 单一来源，env 基线
构造与 DB 覆盖合并后共用同一校验。
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.domain.evaluation.config import (
    EVALUATION_CONFIG_FIELDS,
    EvaluationConfig,
    EvaluationConfigConflictError,
    EvaluationConfigOverrides,
    EvaluationConfigStore,
    EvaluationConfigValidationError,
    ResolvedEvaluationConfig,
    StoredEvaluationConfig,
    merge_overrides,
    overrides_from_payload,
    validate_evaluation_config,
)

logger = logging.getLogger(__name__)


def evaluation_config_from_env(settings: Settings) -> EvaluationConfig:
    """B 类 env 基线 -> C 类解析基线（启动期投影；Dashboard 编辑落在
    DB 覆盖层，env 值永远只读）。"""
    return EvaluationConfig(
        evaluation_enabled=settings.evaluation_capture_enabled,
        evaluation_sampling_rate=settings.evaluation_sampling_rate,
        evaluation_failure_sampling_rate=settings.evaluation_failure_sampling_rate,
        evaluation_sampling_policy_version=(
            settings.evaluation_sampling_policy_version
        ),
        evaluation_worker_concurrency=settings.evaluation_worker_concurrency,
        evaluation_daily_call_ceiling=settings.evaluation_daily_call_ceiling,
    )


class EvaluationConfigService:
    """评估 C 类配置的解析/读取/CAS 更新入口。"""

    def __init__(
        self,
        settings: Settings,
        store: EvaluationConfigStore,
    ) -> None:
        self._store = store
        self._worker_max_concurrency = settings.evaluation_worker_max_concurrency
        # Judge Provider 是否已配置（B 类 env）：已配置时 C 类更新不得把
        # 日调用预算改为 0（24 项三方审阅 #15，domain 校验单一来源）
        self._judge_configured = bool(
            settings.evaluation_judge_provider_base_url
            and settings.evaluation_judge_provider_api_key
            and settings.evaluation_judge_provider_model
        )
        self._env_config = evaluation_config_from_env(settings)
        self._validate_env(self._env_config)
        # last-known-good 从通过校验的 env 基线起步；仅在成功读取后更新
        self._last_known_good: EvaluationConfig = self._env_config

    def _validate_env(self, cfg: EvaluationConfig) -> None:
        try:
            validate_evaluation_config(
                cfg,
                worker_max_concurrency=self._worker_max_concurrency,
                judge_configured=self._judge_configured,
            )
        except EvaluationConfigValidationError as exc:
            raise ValueError(f"evaluation C 类 env 基线非法: {exc}") from exc

    # ------------------------------------------------------------------
    # 运行面（永不抛错）
    # ------------------------------------------------------------------

    def _resolve_stored(
        self, stored: StoredEvaluationConfig | None
    ) -> EvaluationConfig:
        """stored 行 -> 运行面解析配置（m3：current/current_sync 共享
        回退口径，防漂移——None/覆盖非法回退 last-known-good 或 env 基
        线，合法覆盖更新 last-known-good）。"""
        if stored is None:
            self._last_known_good = self._env_config
            return self._env_config
        resolved = merge_overrides(self._env_config, stored.overrides)
        try:
            validate_evaluation_config(
                resolved,
                worker_max_concurrency=self._worker_max_concurrency,
                judge_configured=self._judge_configured,
            )
        except EvaluationConfigValidationError:
            logger.warning(
                "stored evaluation config overrides invalid; using env base"
            )
            self._last_known_good = self._env_config
            return self._env_config
        self._last_known_good = resolved
        return resolved

    async def current(self) -> EvaluationConfig:
        """运行时热重载读取：store 失败/覆盖损坏回退 last-known-good。

        保留理由（N2）：T21 评估管理面路由（Dashboard/HTTP GET 配置）
        的异步运行面读取入口——当前运行面消费方（捕获钩子）为同步上下
        文故使用 ``current_sync``，两者共享 last-known-good 语义；异步
        面与 ``update``/``get_resolved`` 同层对称，不为 T21 重复实现。"""
        try:
            stored = await self._store.get()
        except Exception:
            logger.warning(
                "evaluation config store read failed; using last-known-good"
            )
            return self._last_known_good
        return self._resolve_stored(stored)

    def current_sync(self) -> EvaluationConfig:
        """同步热重载读取（捕获钩子等同步上下文使用）。

        经 store 的 ``get_sync``（SQLite 本地单行读取，微秒级，阻塞
        可接受）；store 不提供 get_sync 或读取失败时回退 last-known-good，
        永不抛错。回退口径与 ``current`` 共享 ``_resolve_stored``。"""
        get_sync = getattr(self._store, "get_sync", None)
        if get_sync is None:
            return self._last_known_good
        try:
            stored = get_sync()
        except Exception:
            logger.warning(
                "evaluation config sync read failed; using last-known-good"
            )
            return self._last_known_good
        return self._resolve_stored(stored)

    # ------------------------------------------------------------------
    # 管理面（严格模式）
    # ------------------------------------------------------------------

    @staticmethod
    def _overrides_from_patch(
        payload: dict[str, Any],
    ) -> EvaluationConfigOverrides:
        """patch 载荷 -> 严格覆盖对象（m4：单一来源改调 domain
        ``overrides_from_payload``——未知键/类型垃圾统一映射
        EvaluationConfigValidationError，管理面 422 语义）。"""
        return overrides_from_payload(payload)

    async def get_resolved(self) -> ResolvedEvaluationConfig:
        stored = await self._store.get()
        if stored is None:
            return ResolvedEvaluationConfig(
                config=self._env_config, version=0, overridden_fields=()
            )
        resolved = merge_overrides(self._env_config, stored.overrides)
        validate_evaluation_config(
            resolved, worker_max_concurrency=self._worker_max_concurrency
        )
        return ResolvedEvaluationConfig(
            config=resolved,
            version=stored.version,
            overridden_fields=stored.overrides.overridden_fields(),
            updated_at=stored.updated_at,
            updated_by=stored.updated_by,
        )

    async def update(
        self,
        partial: dict[str, Any],
        expected_version: int,
        updated_by: str,
    ) -> ResolvedEvaluationConfig:
        """部分覆盖合并更新（CAS + 未知键拒绝 + 合并后整体校验）。"""
        if not isinstance(partial, dict) or not partial:
            raise EvaluationConfigValidationError(
                "patch 必须是非空对象"
            )
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise EvaluationConfigValidationError(
                "expected_version 必须是非负整数"
            )
        if not isinstance(updated_by, str) or not updated_by:
            raise EvaluationConfigValidationError(
                "updated_by 必须是非空字符串"
            )
        patch_overrides = self._overrides_from_patch(partial)

        stored = await self._store.get()
        if stored is None:
            existing = EvaluationConfigOverrides()
            old_version = 0
        else:
            existing = stored.overrides
            old_version = stored.version
        if old_version != expected_version:
            raise EvaluationConfigConflictError(
                f"version mismatch: expected {expected_version}, got {old_version}"
            )
        merged_kwargs: dict[str, Any] = {}
        for field in EVALUATION_CONFIG_FIELDS:
            if field in partial:
                merged_kwargs[field] = getattr(patch_overrides, field)
            else:
                merged_kwargs[field] = getattr(existing, field)
        merged = EvaluationConfigOverrides(**merged_kwargs)

        candidate = merge_overrides(self._env_config, merged)
        validate_evaluation_config(
            candidate,
            worker_max_concurrency=self._worker_max_concurrency,
            judge_configured=self._judge_configured,
        )
        saved = await self._store.save(merged, expected_version, updated_by)
        self._last_known_good = candidate
        return ResolvedEvaluationConfig(
            config=candidate,
            version=saved.version,
            overridden_fields=merged.overridden_fields(),
            updated_at=saved.updated_at,
            updated_by=saved.updated_by,
        )

    async def replace_overrides(
        self,
        overrides: dict[str, Any],
        expected_version: int,
        updated_by: str,
        *,
        dry_run: bool = False,
    ) -> ResolvedEvaluationConfig:
        """整组覆盖替换（配置迁移入口；空对象 = 无覆盖）。"""
        if not isinstance(overrides, dict):
            raise EvaluationConfigValidationError("overrides 必须是 JSON 对象")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise EvaluationConfigValidationError(
                "expected_version 必须是非负整数"
            )
        if not isinstance(updated_by, str) or not updated_by:
            raise EvaluationConfigValidationError(
                "updated_by 必须是非空字符串"
            )
        replacement = self._overrides_from_patch(overrides)
        candidate = merge_overrides(self._env_config, replacement)
        validate_evaluation_config(
            candidate,
            worker_max_concurrency=self._worker_max_concurrency,
            judge_configured=self._judge_configured,
        )
        if dry_run:
            return ResolvedEvaluationConfig(
                config=candidate,
                version=0,
                overridden_fields=replacement.overridden_fields(),
            )
        saved = await self._store.save(replacement, expected_version, updated_by)
        self._last_known_good = candidate
        return ResolvedEvaluationConfig(
            config=candidate,
            version=saved.version,
            overridden_fields=replacement.overridden_fields(),
            updated_at=saved.updated_at,
            updated_by=saved.updated_by,
        )
