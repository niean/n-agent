# tests/infrastructure/evaluation/_degraded_helpers.py
"""evaluation_feedback 线程索引降级测试辅助（v8 迁移降级分支）。

供 tests/infrastructure/evaluation/test_evaluation_store.py 与
tests/application/test_evaluation_annotation.py 共用，统一签名
``str | Path``；两处测试各自的 ``_make_degraded_index_store`` 播种
数据差异属测试意图，不在此收敛。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def force_schema_v7(db_path: str | Path) -> None:
    """把库降回 v7 状态（删 v8 版本行 + user_version=7），下次实例化
    store 触发 v8 索引迁移（迁移块按索引存在性判定，幂等）。"""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM evaluation_schema_migrations WHERE version = 8"
        )
        conn.execute("PRAGMA user_version = 7")
        conn.commit()


def drop_feedback_thread_indexes(db_path: str | Path) -> None:
    """删除 feedback 线程 revision 唯一索引与普通索引（拆索引后插
    历史冲突行，用于构造迁移降级输入）。"""
    with sqlite3.connect(str(db_path)) as conn:
        for name in (
            "uq_feedback_thread_revision_key",
            "ix_feedback_thread_revision_key",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.commit()
