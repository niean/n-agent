"""T20: 评估 C 类配置 domain 单一来源测试（字段集/覆盖解析/跨字段校验）。"""
from __future__ import annotations

import pytest

from app.domain.evaluation.config import (
    EVALUATION_CONFIG_FIELDS,
    EvaluationConfig,
    EvaluationConfigOverrides,
    EvaluationConfigStoreError,
    EvaluationConfigValidationError,
    merge_overrides,
    normalize_session_source_allowlist,
    overrides_from_payload,
    validate_evaluation_config,
    validate_model_name_string,
)


def test_field_set_is_stable():
    assert EVALUATION_CONFIG_FIELDS == (
        "evaluation_enabled",
        "evaluation_sampling_rate",
        "evaluation_failure_sampling_rate",
        "evaluation_sampling_policy_version",
        "evaluation_worker_concurrency",
        "evaluation_daily_call_ceiling",
        "evaluation_judge_provider_id",
        "evaluation_session_source_allowlist",
    )


def test_validate_concurrency_over_b_ceiling_rejected():
    cfg = EvaluationConfig(evaluation_worker_concurrency=5)
    with pytest.raises(EvaluationConfigValidationError, match="B 类部署上限"):
        validate_evaluation_config(cfg, worker_max_concurrency=4)
    validate_evaluation_config(cfg, worker_max_concurrency=5)


def test_validate_concurrency_below_one_rejected():
    cfg = EvaluationConfig(evaluation_worker_concurrency=0)
    with pytest.raises(EvaluationConfigValidationError, match=">= 1"):
        validate_evaluation_config(cfg, worker_max_concurrency=4)


def test_validate_rate_bounds():
    validate_evaluation_config(
        EvaluationConfig(evaluation_sampling_rate=1.0), worker_max_concurrency=4
    )
    for bad in (-0.1, 1.01):
        with pytest.raises(EvaluationConfigValidationError):
            validate_evaluation_config(
                EvaluationConfig(evaluation_sampling_rate=bad),
                worker_max_concurrency=4,
            )


def test_validate_policy_version_non_empty():
    with pytest.raises(EvaluationConfigValidationError):
        validate_evaluation_config(
            EvaluationConfig(evaluation_sampling_policy_version=""),
            worker_max_concurrency=4,
        )


def test_validate_daily_call_ceiling_bounds():
    validate_evaluation_config(
        EvaluationConfig(evaluation_daily_call_ceiling=0),
        worker_max_concurrency=4,
    )
    with pytest.raises(EvaluationConfigValidationError):
        validate_evaluation_config(
            EvaluationConfig(evaluation_daily_call_ceiling=-1),
            worker_max_concurrency=4,
        )


def test_validate_zero_ceiling_rejected_when_judge_configured():
    """env 侧 Judge 供给就绪（judge_configured=True）时 ceiling=0 必抛
    （统一预算条件的 judge_configured 分支在 domain 层自证）。"""
    with pytest.raises(EvaluationConfigValidationError):
        validate_evaluation_config(
            EvaluationConfig(evaluation_daily_call_ceiling=0),
            worker_max_concurrency=4,
            judge_configured=True,
        )


def test_overrides_roundtrip_and_strict_parse():
    overrides = EvaluationConfigOverrides(
        evaluation_enabled=True, evaluation_sampling_rate=0.5
    )
    data = overrides.to_dict()
    assert data == {"evaluation_enabled": True, "evaluation_sampling_rate": 0.5}
    parsed = EvaluationConfigOverrides.from_dict(data)
    assert parsed == overrides
    # int 数值字段接受 int，投影为 float（率字段）
    assert EvaluationConfigOverrides.from_dict(
        {"evaluation_sampling_rate": 1}
    ).evaluation_sampling_rate == 1.0


def test_overrides_unknown_key_rejected():
    with pytest.raises(EvaluationConfigStoreError, match="未知覆盖键"):
        EvaluationConfigOverrides.from_dict({"nope": 1})


def test_overrides_type_garbage_rejected():
    with pytest.raises(EvaluationConfigStoreError):
        EvaluationConfigOverrides.from_dict({"evaluation_enabled": "yes"})
    with pytest.raises(EvaluationConfigStoreError):
        EvaluationConfigOverrides.from_dict({"evaluation_worker_concurrency": True})
    with pytest.raises(EvaluationConfigStoreError):
        EvaluationConfigOverrides.from_dict({"evaluation_sampling_rate": "0.5"})


def test_merge_overrides_prefers_override_values():
    base = EvaluationConfig(evaluation_worker_concurrency=2)
    merged = merge_overrides(
        base, EvaluationConfigOverrides(evaluation_worker_concurrency=3)
    )
    assert merged.evaluation_worker_concurrency == 3
    merged = merge_overrides(base, EvaluationConfigOverrides())
    assert merged.evaluation_worker_concurrency == 2


class TestEvaluationJudgeProviderIdField:
    def test_default_empty_and_registered(self):
        cfg = EvaluationConfig()
        assert cfg.evaluation_judge_provider_id == ""
        assert "evaluation_judge_provider_id" in EVALUATION_CONFIG_FIELDS

    def test_overrides_field_none_default_and_registered(self):
        ov = EvaluationConfigOverrides()
        assert ov.evaluation_judge_provider_id is None
        assert ov.to_dict() == {}  # None 不序列化

    def test_from_dict_accepts_valid_id_and_normalizes_empty_to_none(self):
        # 合法 id 保存
        ov = EvaluationConfigOverrides.from_dict(
            {"evaluation_judge_provider_id": "provider-a"}
        )
        assert ov.evaluation_judge_provider_id == "provider-a"
        # 空串 = 清除覆盖：不写入 kwargs
        ov2 = EvaluationConfigOverrides.from_dict(
            {"evaluation_judge_provider_id": ""}
        )
        assert ov2.evaluation_judge_provider_id is None
        assert ov2.to_dict() == {}

    def test_from_dict_rejects_non_string(self):
        for bad in (None, True, 1, 1.5, []):
            with pytest.raises(EvaluationConfigStoreError):
                EvaluationConfigOverrides.from_dict(
                    {"evaluation_judge_provider_id": bad}
                )

    def test_from_dict_rejects_whitespace_with_message(self):
        with pytest.raises(EvaluationConfigStoreError, match="首尾空白") as exc_info:
            EvaluationConfigOverrides.from_dict(
                {"evaluation_judge_provider_id": " x"}
            )
        assert "覆盖 evaluation_judge_provider_id" in str(exc_info.value)

    def test_from_dict_rejects_control_chars(self):
        for bad in ("a\u0000b", "a\nb", "a\u007fb", "a\u0085b", "a\u009fb"):
            with pytest.raises(EvaluationConfigStoreError):
                EvaluationConfigOverrides.from_dict(
                    {"evaluation_judge_provider_id": bad}
                )

    def test_from_dict_rejects_over_200_codepoints(self):
        for bad in ("x" * 201, "\U0001f600" * 201):
            with pytest.raises(EvaluationConfigStoreError):
                EvaluationConfigOverrides.from_dict(
                    {"evaluation_judge_provider_id": bad}
                )

    def test_from_dict_rejects_legacy_judge_model_key(self):
        # 旧键 evaluation_judge_model 作为 PATCH 载荷必须被拒绝
        with pytest.raises(EvaluationConfigStoreError, match="未知覆盖键"):
            EvaluationConfigOverrides.from_dict(
                {"evaluation_judge_model": "m1"}
            )

    def test_overrides_from_payload_error_contract(self):
        # 管理面载荷路径：StoreError 再转回 EvaluationConfigValidationError
        with pytest.raises(EvaluationConfigValidationError):
            overrides_from_payload({"evaluation_judge_provider_id": " x"})

    @pytest.mark.parametrize(
        "bad",
        [
            " x",
            "x ",
            " x ",
            "\tx",
            " ",
            "a\u0000b",
            "a\nb",
            "a\u007fb",
            "a\u0085b",
            "a\u009fb",
            "　x",
            "x　",
            "x" * 201,
            "\U0001f600" * 201,
        ],
    )
    def test_validate_rejects_invalid(self, bad):
        with pytest.raises(EvaluationConfigValidationError):
            validate_model_name_string(bad, allow_empty=False)

    @pytest.mark.parametrize("bad", [None, True, 1, 1.5, []])
    def test_validate_rejects_non_string(self, bad):
        with pytest.raises(EvaluationConfigValidationError):
            validate_model_name_string(bad, allow_empty=False)
        # 冻结 dataclass 注入路径：validate_evaluation_config 同样拒绝
        cfg = EvaluationConfig()
        object.__setattr__(cfg, "evaluation_judge_provider_id", bad)
        with pytest.raises(EvaluationConfigValidationError):
            validate_evaluation_config(cfg, worker_max_concurrency=4)

    def test_validate_rejects_empty_when_not_allowed(self):
        with pytest.raises(EvaluationConfigValidationError):
            validate_model_name_string("", allow_empty=False)

    @pytest.mark.parametrize(
        "good",
        ["", "x" * 200, "模型-A.1", "gpt 4o", "\U0001f600" * 200],
    )
    def test_validate_accepts_valid(self, good):
        if good == "":
            assert validate_model_name_string(good, allow_empty=True) == ""
            return
        assert validate_model_name_string(good, allow_empty=False) == good

    def test_validate_model_name_string_returns_original_untrimmed(self):
        assert validate_model_name_string("gpt", allow_empty=False) == "gpt"
        merged = merge_overrides(
            # 预算为正：统一预算条件只允许在 domain 单一来源触发，本测
            # 试聚焦模型名不 trim，不与 ceiling=0 组合（任一 Judge 供给
            # 来源存在时 ceiling 必须为正）
            EvaluationConfig(evaluation_daily_call_ceiling=100),
            EvaluationConfigOverrides.from_dict(
                {"evaluation_judge_provider_id": "gpt 4o"}
            ),
        )
        assert merged.evaluation_judge_provider_id == "gpt 4o"
        validate_evaluation_config(merged, worker_max_concurrency=4)


class TestEvaluationSessionSourceAllowlistField:
    """evaluation_session_source_allowlist：归一/校验/覆盖往返/空名单语义。"""

    def test_normalize_dedupes_and_orders_canonically(self):
        # 输出严格按 SessionSource 枚举定义顺序（dashboard 在首位）
        values = ["cli", "dashboard", "schedule", "dashboard"]
        assert normalize_session_source_allowlist(values) == (
            "dashboard",
            "cli",
            "schedule",
        )

    def test_normalize_rejects_non_list_and_bad_items(self):
        for bad in (
            "dashboard",
            None,
            1,
            True,
            ["Dashboard"],
            [" dashboard "],
            ["nope"],
            [1],
            [None],
        ):
            with pytest.raises(EvaluationConfigValidationError):
                normalize_session_source_allowlist(bad)

    def test_normalize_accepts_empty_list(self):
        assert normalize_session_source_allowlist([]) == ()
        assert normalize_session_source_allowlist(()) == ()
        assert normalize_session_source_allowlist(("cli", "dashboard", "cli")) == (
            "dashboard",
            "cli",
        )

    def test_config_field_default_and_whitelist(self):
        assert "evaluation_session_source_allowlist" in EVALUATION_CONFIG_FIELDS
        assert EvaluationConfig().evaluation_session_source_allowlist == (
            "dashboard",
        )

    def test_overrides_roundtrip_and_none_clear(self):
        ov = EvaluationConfigOverrides.from_dict(
            {"evaluation_session_source_allowlist": ["cli", "api"]}
        )
        assert ov.evaluation_session_source_allowlist == ("api", "cli")
        # 空数组 = 关闭会话评估，是合法覆盖值（非清除）
        assert (
            EvaluationConfigOverrides.from_dict(
                {"evaluation_session_source_allowlist": []}
            ).evaluation_session_source_allowlist
            == ()
        )
        # 显式 null = 清除覆盖（恢复 env 基线）
        assert (
            EvaluationConfigOverrides.from_dict(
                {"evaluation_session_source_allowlist": None}
            ).evaluation_session_source_allowlist
            is None
        )

    def test_overrides_reject_bad_payload(self):
        for bad in (["nope"], "dashboard", [True]):
            with pytest.raises(EvaluationConfigStoreError):
                EvaluationConfigOverrides.from_dict(
                    {"evaluation_session_source_allowlist": bad}
                )

    def test_overrides_to_dict_projects_allowlist_as_list(self):
        # to_dict 专用投影：元组 -> 数组（JSON 序列化友好）
        ov = EvaluationConfigOverrides.from_dict(
            {"evaluation_session_source_allowlist": ["cli", "api"]}
        )
        data = ov.to_dict()
        assert data == {"evaluation_session_source_allowlist": ["api", "cli"]}
        assert isinstance(data["evaluation_session_source_allowlist"], list)

    def test_overrides_from_payload_rejects_non_array_and_maps_error(self):
        # 管理面载荷路径：非数组 JSON 类型拒绝，StoreError 映射回领域校验异常
        for bad in ("dashboard", {"dashboard": 1}, 1, True):
            with pytest.raises(EvaluationConfigValidationError):
                overrides_from_payload({"evaluation_session_source_allowlist": bad})

    def test_merge_overrides_allowlist_semantics(self):
        base = EvaluationConfig()
        merged = merge_overrides(
            base,
            EvaluationConfigOverrides.from_dict(
                {"evaluation_session_source_allowlist": ["cli"]}
            ),
        )
        assert merged.evaluation_session_source_allowlist == ("cli",)
        # None = 未覆盖，跟随 env 基线
        merged2 = merge_overrides(base, EvaluationConfigOverrides())
        assert merged2.evaluation_session_source_allowlist == ("dashboard",)
        # 空元组覆盖 = 关闭（is not None 语义保留）
        merged3 = merge_overrides(
            base,
            EvaluationConfigOverrides(evaluation_session_source_allowlist=()),
        )
        assert merged3.evaluation_session_source_allowlist == ()

    def test_validate_evaluation_config_checks_allowlist_type(self):
        import dataclasses

        cfg = dataclasses.replace(
            EvaluationConfig(), evaluation_session_source_allowlist=("nope",)
        )
        with pytest.raises(EvaluationConfigValidationError):
            validate_evaluation_config(cfg, worker_max_concurrency=4)

    def test_validate_rejects_non_tuple_and_unnormalized_values(self):
        # 冻结 dataclass 注入路径：None/整数/字符串/bool/列表/非归一元组
        # 均抛领域校验异常而非 TypeError
        for bad in (None, 1, "dashboard", True, ["dashboard"], ["cli", "dashboard"]):
            cfg = EvaluationConfig()
            object.__setattr__(
                cfg, "evaluation_session_source_allowlist", bad
            )
            with pytest.raises(EvaluationConfigValidationError):
                validate_evaluation_config(cfg, worker_max_concurrency=4)

    def test_validate_does_not_mutate_frozen_config(self):
        cfg = EvaluationConfig()
        object.__setattr__(
            cfg, "evaluation_session_source_allowlist", ["dashboard"]
        )
        with pytest.raises(EvaluationConfigValidationError):
            validate_evaluation_config(cfg, worker_max_concurrency=4)
        # 校验失败不修正冻结对象（调用方可见原值，便于上层诊断）
        assert cfg.evaluation_session_source_allowlist == ["dashboard"]

    def test_validate_accepts_normalized_default(self):
        # 默认基线（"dashboard",）必须通过整体校验（env 基线构造路径）
        validate_evaluation_config(
            EvaluationConfig(), worker_max_concurrency=4
        )

