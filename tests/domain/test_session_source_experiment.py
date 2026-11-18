"""SessionSource.EXPERIMENT：评估实验会话来源与前缀常量。

Spec reference:
  - SessionSource.EXPERIMENT = "experiment"（第 12 个来源，非 IM 平台，不进 im_platforms）
  - EXPERIMENT_SESSION_ID_PREFIX = "eval-experiment-"（模式十六显式例外：
    前缀已被评估子域 evidence_targets 与删除传播反向索引消费，不改名）
"""
from __future__ import annotations

from app.domain.session import (
    EXPERIMENT_SESSION_ID_PREFIX,
    SessionSource,
)


def test_session_source_experiment_enum_value():
    assert SessionSource.EXPERIMENT.value == "experiment"
    assert SessionSource.EXPERIMENT not in SessionSource.im_platforms()
    assert len(list(SessionSource)) == 12


def test_experiment_session_id_prefix_constant():
    assert EXPERIMENT_SESSION_ID_PREFIX == "eval-experiment-"
