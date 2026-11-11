# app/domain/evaluation/errors.py
"""评估子域错误契约（spec 5.1 / 6.3 端口错误语义的唯一权威定义）。

错误词汇原本由 T3 在 Infrastructure 层声明；T5 引入 Application 用例层
（Application 禁止 import app.infrastructure，见 knowledge/03-conventions
"DDD 依赖方向"），按 app.domain.artifact 的既有房规把错误契约上移到
Domain：Infrastructure 适配（SQLiteEvaluationStore）从本模块导入并原样
re-export（T3 测试的既有导入路径不变），Application 直接依赖本模块。

服务层用例语义错误（EvaluationDisabledError / EvaluationCapacityError /
EvaluationRevisionConflictError）不属于存储契约，定义在
app/application/evaluation_service.py。
"""
from __future__ import annotations


class EvaluationStoreError(RuntimeError):
    """评估存储层错误基类。"""


class EvaluationNotFoundError(EvaluationStoreError):
    """目标行不存在（服务层映射 404）。"""


class EvaluationConflictError(EvaluationStoreError):
    """幂等键/唯一约束冲突（服务层映射 409）。"""


class EvaluationDeletionConflictError(EvaluationConflictError):
    """目标主体已被删除 tombstone 封禁（spec 14 删除传播）。

    命中场景：迟到样本写入（含新 retrieval_id 携带已删文档版本）、删除
    期间运行中 Judge 的结果提交、已删主体的投递发送准备（freeze）。调用
    方据此放弃写入/终结在途操作，不重试、不复活已删内容。"""


class EvaluationLeaseError(EvaluationConflictError):
    """结果提交携带的 lease token 不是最新持有者 token。"""


class EvaluationStaleSequenceError(EvaluationConflictError):
    """producer_state 只接受更高 sequence，收到 <= 当前水位。"""


class EvaluationBudgetExhaustedError(EvaluationStoreError):
    """日预算预留被拒：任一已配置维度（USD/tokens/calls）达到上限
    （spec 13；预算在 claim 时事务预留，超限 job 进入
    skipped(budget_exhausted)，不调用 Judge）。"""
