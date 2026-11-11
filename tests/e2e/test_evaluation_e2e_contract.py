"""T22 静态合同测试（宿主机，无 Docker 依赖）。

锁定评估闭环 E2E 的编排合同，不执行任何业务逻辑：

- tests/e2e/evaluation_e2e.sh 存在且可执行，结构覆盖 plan T22 S1 全部
  用例（固定 3 样本三元对账 / execute-replay 实验 / Langfuse 停机故障
  注入与恢复补发 / 未采样 unlinked）与补充故障矩阵项；
- tests/e2e/run.sh 接入评估用例（all 套件 + 独立入口），保持既有两节
  报告契约与串行编排约束（无套件并行）；
- result_report.py 支持评估套件报告（评估分类、case 初始化、finish 落
  result.md 两节表格）。

RED 纪律：本测试先于脚本与接入编写，先真实失败，实现后同命令 GREEN。
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent.parent


def _read(name: str) -> str:
    return (E2E_DIR / name).read_text(encoding="utf-8")


class TestEvaluationScriptExists:
    def test_script_exists_and_executable(self) -> None:
        path = E2E_DIR / "evaluation_e2e.sh"
        assert path.is_file(), "tests/e2e/evaluation_e2e.sh 不存在"
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, "evaluation_e2e.sh 缺少可执行位"

    def test_script_bash_syntax_valid(self) -> None:
        path = E2E_DIR / "evaluation_e2e.sh"
        proc = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr

    def test_script_reuses_e2e_environment(self) -> None:
        content = _read("evaluation_e2e.sh")
        assert 'source "$(dirname "${BASH_SOURCE[0]}")/environment.sh"' in content
        assert "e2e_environment default" in content


class TestEvaluationScriptStructure:
    """S1 四个核心用例与补充故障矩阵的结构合同（关键标记逐项锁定）。"""

    def _content(self) -> str:
        return _read("evaluation_e2e.sh")

    def test_compose_override_enables_evaluation(self) -> None:
        content = self._content()
        # B 类总闸与 producer 凭据经 compose override 注入（Docker 内生效）
        assert "N_AGENT_EVALUATION_ENABLED" in content
        assert "N_AGENT_EVALUATION_PRODUCER_TOKEN" in content
        assert "COMPOSE_FILE" in content

    def test_producer_auth_matrix(self) -> None:
        content = self._content()
        # producer 通道 + 错误认证 401 用例
        assert "/evaluation/producer/messages" in content
        assert "401" in content

    def test_three_fixed_samples(self) -> None:
        content = self._content()
        # 固定 3 个已知 sample（对账分母）
        assert re.search(r"e2e-eval-fixed-[0-9]", content)

    def test_three_way_reconciliation(self) -> None:
        content = self._content()
        # 本地账本（容器内只读 SQLite）+ Langfuse Score API + Loki 事件
        assert "evaluations.db" in content
        assert "n-langfuse-web:3000/api/public" in content
        assert "gen_ai.evaluation.result" in content

    def test_diff_classification(self) -> None:
        content = self._content()
        # 差额分类（AC11）：unlinked / pending / superseded
        for word in ("unlinked", "pending", "superseded"):
            assert word in content, f"差额分类缺少 {word}"

    def test_execute_replay_experiment_in_container(self) -> None:
        content = self._content()
        # evaluate CLI 必须容器内执行（evaluate run + evaluate replay）
        assert "evaluate run" in content
        assert "evaluate replay" in content

    def test_langfuse_fault_injection_and_recovery(self) -> None:
        content = self._content()
        # Langfuse 停机注入 + 恢复后补发 verified（AC12）
        assert "langfuse-web" in content
        assert "verified" in content

    def test_unsampled_unlinked_case(self) -> None:
        content = self._content()
        # 未采样/无 Trace → unlinked（AC08）
        assert "unlinked" in content

    def test_window_gauge_reconciliation(self) -> None:
        content = self._content()
        # 24h current Gauge 与 current 视图一致（AC11）
        assert "nagent_evaluation_window_" in content

    def test_sentinel_redaction_check(self) -> None:
        content = self._content()
        # 敏感哨兵不得外发（Loki / Langfuse 双出口）
        assert "SENTINEL" in content

    def test_deletion_conflict_case(self) -> None:
        content = self._content()
        # 迟到样本携带已删文档版本 → 409 deletion_conflict（AC19 竞态面）
        assert "deletion_conflict" in content

    def test_idempotent_resubmit_case(self) -> None:
        content = self._content()
        # 同 producer_event_id 重试不重复样本（AC03）
        assert "producer_event_id" in content

    def test_report_lifecycle(self) -> None:
        content = self._content()
        # 两节报告契约：result_report 初始化 + finish 输出 RESULT_FILE/REPORT_DIR
        assert "result_report.py" in content
        assert "init-evaluation" in content
        assert "finish" in content

    def test_environment_restore_trap(self) -> None:
        content = self._content()
        # 环境恢复：退出时还原原 COMPOSE_FILE 部署（不残留评估 override）
        assert "trap" in content
        assert "force-recreate" in content


class TestRunShIntegration:
    def test_all_suite_includes_evaluation(self) -> None:
        content = _read("run.sh")
        assert re.search(
            r'CASE_SCRIPTS=\(.*"?\$SCRIPT_DIR/evaluation_e2e\.sh"?', content,
            re.S,
        ), "run.sh all 套件未接入 evaluation_e2e.sh"

    def test_standalone_suite_entry(self) -> None:
        content = _read("run.sh")
        assert "evaluation-e2e) CASE_SCRIPTS=" in content, (
            "run.sh 缺少 evaluation-e2e 独立套件入口"
        )

    def test_evaluation_category_mapping(self) -> None:
        content = _read("run.sh")
        # 汇总分类映射：evaluation_e2e.sh → 评估
        assert "评估" in content
        assert re.search(r"evaluation_e2e\.sh.*评估|评估.*evaluation_e2e", content)

    def test_suite_stays_serial(self) -> None:
        content = _read("run.sh")
        # E2E 无套件并行约束（artifacts.sh 重启容器）：顺序 for 循环编排
        assert not re.search(r"\bxargs\s+-P\b", content)
        assert not re.search(r"&\s*\n\s*wait\b", content)


class TestResultReportEvaluationSuite:
    def test_init_evaluation_action_supported(self) -> None:
        content = _read("result_report.py")
        assert "init-evaluation" in content, (
            "result_report.py 缺少 init-evaluation 动作"
        )

    def test_evaluation_cases_defined(self) -> None:
        content = _read("result_report.py")
        # 四个评估用例身份（与 evaluation_e2e.sh 的 report_case begin 对齐）
        for case_id in ("reconciliation", "experiment", "fault", "unlinked"):
            assert f'"{case_id}"' in content, f"评估用例缺失: {case_id}"

    def test_evaluation_category_is_chinese(self) -> None:
        content = _read("result_report.py")
        assert '"category": "评估"' in content
