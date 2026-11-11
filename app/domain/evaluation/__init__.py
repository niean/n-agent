# app/domain/evaluation
"""统一评估闭环领域契约包（spec 5.1 / 6）。

纯领域代码：契约模型（models）、状态机（state）、端口（ports）。
零 IO、零 SDK 依赖；不依赖 application / infrastructure / interfaces，
也不新增领域 Policy（spec 未定义评估 Policy）。
"""
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationLeaseError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
    EvaluationStoreError,
)
from app.domain.evaluation.models import (
    SCHEMA_VERSION,
    DatasetCase,
    DatasetManifest,
    EvidenceSnapshot,
    EvaluationTarget,
    EvaluatorDefinition,
    Experiment,
    EvaluationValidationError,
    EvaluationValue,
    canonical_json_bytes,
    validate_evaluation_value,
)
from app.domain.evaluation.state import (
    ALLOWED_DELIVERY_TRANSITIONS,
    ALLOWED_JOB_TRANSITIONS,
    EvaluationDeliveryStatus,
    EvaluationJobStatus,
    IllegalEvaluationTransition,
    SkipReason,
    transition_delivery_status,
    transition_job_status,
)

__all__ = [
    "ALLOWED_DELIVERY_TRANSITIONS",
    "ALLOWED_JOB_TRANSITIONS",
    "DatasetCase",
    "DatasetManifest",
    "EvidenceSnapshot",
    "EvaluationConflictError",
    "EvaluationDeliveryStatus",
    "EvaluationLeaseError",
    "EvaluationNotFoundError",
    "EvaluationStaleSequenceError",
    "EvaluationStoreError",
    "EvaluationJobStatus",
    "EvaluationTarget",
    "EvaluationValidationError",
    "EvaluationValue",
    "EvaluatorDefinition",
    "Experiment",
    "IllegalEvaluationTransition",
    "SCHEMA_VERSION",
    "SkipReason",
    "canonical_json_bytes",
    "transition_delivery_status",
    "transition_job_status",
    "validate_evaluation_value",
]
