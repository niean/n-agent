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
    validate_evaluation_config,
)


def test_field_set_is_stable():
    assert EVALUATION_CONFIG_FIELDS == (
        "evaluation_enabled",
        "evaluation_sampling_rate",
        "evaluation_failure_sampling_rate",
        "evaluation_sampling_policy_version",
        "evaluation_worker_concurrency",
        "evaluation_daily_call_ceiling",
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
