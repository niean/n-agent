"""宿主单测：artifacts.sh 宿主编排脚本的静态合同。

锁定 cleanup 的防泄漏契约（bug fix 260915：RUNNING 任务删除被拒 +
`|| true` 吞错 + workspace 文件未跟踪，导致任务/派生 worker 会话/workspace
文件泄漏为脏数据）。静态合同直接读源文件，防脚本回归。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_SOURCE = REPO_ROOT / "tests" / "e2e" / "artifacts.sh"


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT_SOURCE.read_text(encoding="utf-8")


def _cleanup_body(source: str) -> str:
    """提取 cleanup() 函数体（从定义到下一个顶层 `}`）。"""
    match = re.search(r"^  cleanup\(\) \{(.+?)^  \}", source, re.S | re.M)
    assert match, "cleanup() 函数不存在"
    return match.group(1)


class TestTaskDeleteLeakGuard:
    """任务删除必须先脱离 RUNNING，且失败必须可见。"""

    def test_wait_task_terminal_helper_defined_and_used(self, source):
        assert re.search(r"^  wait_task_terminal\(\) \{", source, re.M), \
            "缺少 wait_task_terminal 轮询 helper"
        body = _cleanup_body(source)
        assert "wait_task_terminal" in body, \
            "cleanup() 未在删除前等待任务到达终态"

    def test_cleanup_cancels_via_http_before_delete(self, source):
        """cancel/delete 必须走服务端 HTTP 路由。

        CLI（docker exec n-agent task cancel/delete）是独立进程，其
        run_service 无 in-process worker handle，cancel 只能写
        terminate_requested 等待租约回收，清理窗口内不收敛（bug fix 260915
        二次实证）；服务端 HTTP 路由可立即 terminate in-process worker。
        """
        body = _cleanup_body(source)
        cancel_pos = body.find('/chat/tasks/$id/cancel')
        delete_pos = body.find('http DELETE "$BASE_URL/chat/tasks/$id"')
        assert cancel_pos != -1, "cleanup() 缺少服务端 HTTP cancel"
        assert delete_pos != -1, "cleanup() 缺少服务端 HTTP delete"
        assert cancel_pos < delete_pos, "cancel 必须先于 delete"
        assert "n-agent task cancel" not in body, \
            "禁止使用 CLI cancel（独立进程无法 terminate in-process worker）"
        assert "n-agent task delete" not in body, \
            "禁止使用 CLI delete（与 HTTP 路径不一致）"

    def test_task_delete_failure_warns_not_swallowed(self, source):
        body = _cleanup_body(source)
        assert "WARN: cleanup failed to delete task" in body, \
            "task 删除失败必须输出 WARN，禁止静默吞错"
        delete_line = next(
            line for line in body.splitlines()
            if "chat/tasks/$id" in line and "DELETE" in line)
        assert "|| true" not in delete_line, \
            "task delete 行禁止裸 `|| true` 吞错"
        assert "HTTP_STATUS" in body, \
            "delete 必须按 HTTP_STATUS 判定成败并 WARN"

    def test_wait_helper_polls_task_status(self, source):
        match = re.search(
            r"^  wait_task_terminal\(\) \{(.+?)^  \}", source, re.S | re.M)
        assert match, "wait_task_terminal 未定义"
        body = match.group(1)
        assert "/chat/tasks/" in body, "helper 必须轮询任务状态接口"
        # GET /chat/tasks/{id} 响应包装在 .task 下，裸 .status 恒为 null，
        # 会导致等待立即返回（bug fix 260915 三次实证：cleanup 未等即删，
        # delete 409）
        assert ".task.status" in body, "helper 必须从 .task.status 提取状态"
        for terminal in ("succeeded", "failed", "cancelled", "expired"):
            assert terminal in body, f"helper 必须识别终态 {terminal}"

    def test_task_settled_before_container_restart(self, source):
        """Section 2b docker restart 会杀死 in-process worker；此后任务成
        孤儿，只能等 900s 租约过期回收，cleanup 窗口内无法删除（bug fix
        260915 三次实证）。因此 restart 前必须先将任务收敛到终态：
        服务端 HTTP cancel（in-process worker 可立即 terminate）+
        wait_task_terminal。"""
        restart_pos = source.find('docker restart "$CONTAINER"')
        assert restart_pos != -1, "缺少 Section 2b 容器重启"
        before = source[:restart_pos]
        cancel_pos = before.rfind('/chat/tasks/$TASK_ID/cancel')
        wait_pos = before.rfind('wait_task_terminal "$TASK_ID"')
        assert cancel_pos != -1, \
            "restart 前缺少对 Section 1 任务的服务端 HTTP cancel"
        assert wait_pos != -1, \
            "restart 前缺少 wait_task_terminal 收敛等待"
        assert cancel_pos < wait_pos, "cancel 必须先于终态等待"


class TestWorkspaceFileCleanup:
    """Section 1c 写入的 workspace 文件必须被跟踪并在 cleanup 删除。"""

    def test_workspace_files_tracked(self, source):
        assert re.search(r"^  track_workspace_file\(\) \{", source, re.M), \
            "缺少 track_workspace_file 登记 helper"
        assert re.search(
            r"track_workspace_file \"?\$\{?RUN_TAG\}?-taskart-", source), \
            "Section 1c 的 taskart workspace 文件未登记"

    def test_cleanup_removes_workspace_files(self, source):
        body = _cleanup_body(source)
        assert "CLEANUP_WORKSPACE_FILES" in body, \
            "cleanup() 未遍历 CLEANUP_WORKSPACE_FILES"
        assert 'rm -f "/workspace/' in body, \
            "cleanup() 未删除容器内 workspace 文件"
        assert "WARN: cleanup failed to remove workspace file" in body, \
            "workspace 文件删除失败必须输出 WARN"
