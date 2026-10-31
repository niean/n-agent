"""宿主单测：otel_smoke.sh 观测冒烟编排脚本的静态合同（T10）。

最小集合：成功链路三信号关联与脱敏、Provider 500、真实客户端断连。
"""
from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SOURCE = REPO_ROOT / "tests" / "e2e" / "otel_smoke.sh"
RUN_SOURCE = REPO_ROOT / "tests" / "e2e" / "run.sh"

# T9 S4 锁定的 Prometheus 实际导出名称（docker/observability/
# metric-name-mapping.md；不是 spec 示例名）。
PROM_COUNTERS = (
    "gen_ai_client_inference_usage_input_tokens_total",
    "gen_ai_client_inference_usage_output_tokens_total",
    "nagent_turns_total",
)
PROM_HISTOGRAMS = (
    "gen_ai_execute_tool_duration_seconds",
    "gen_ai_invoke_agent_duration_seconds",
)


@pytest.fixture(scope="module")
def smoke() -> str:
    return SMOKE_SOURCE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_sh() -> str:
    return RUN_SOURCE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# run.sh integration
# ---------------------------------------------------------------------------


class TestRunShIntegration:
    def test_run_sh_reexecs_under_bash(self, run_sh):
        """sh tests/e2e/run.sh 必须有效：POSIX guard + exec bash。"""
        assert re.search(r'if \[ -z "\$\{BASH_VERSION:-\}" \]', run_sh), (
            "run.sh 缺少 POSIX sh 检测"
        )
        assert re.search(r"exec bash \"\$0\"|exec bash -", run_sh), (
            "run.sh 缺少 exec bash 再执行"
        )

    def test_all_includes_otel_smoke(self, run_sh):
        all_line = next(
            line for line in run_sh.splitlines() if "all)" in line
        )
        assert "otel_smoke.sh" in all_line, "all 列表未接入观测冒烟"
        for suite in ("artifacts.sh", "conversations.sh"):
            assert suite in all_line, f"既有 suite {suite} 被移除"

    def test_usage_lists_otel_smoke(self, run_sh):
        assert "otel-smoke" in run_sh, "usage 未列出 otel-smoke"

    def test_runner_keeps_single_rebuild_and_inherits_observability_env(
        self, run_sh
    ):
        assert "./restart.sh" in run_sh, "run.sh 必须保留重建入口"
        # 环境贯穿：runner 不得 unset/覆盖观测相关环境（item 23）
        for forbidden in (
            "unset COMPOSE_FILE",
            "unset COMPOSE_PROFILES",
            "unset N_AGENT_COMPOSE_STOP_TIMEOUT",
        ):
            assert forbidden not in run_sh, f"run.sh 覆盖了观测环境: {forbidden}"


# ---------------------------------------------------------------------------
# otel_smoke.sh structure
# ---------------------------------------------------------------------------


class TestSmokeGating:
    def test_script_exists_and_executable(self):
        assert SMOKE_SOURCE.is_file(), "tests/e2e/otel_smoke.sh 不存在"
        mode = SMOKE_SOURCE.stat().st_mode
        assert mode & stat.S_IXUSR, "otel_smoke.sh 必须可执行（run.sh 直接调用）"

    def test_self_gating_on_app_switch(self, smoke):
        """门控读取容器内应用开关，而非 compose profile。"""
        assert "N_AGENT_OTEL_ENABLED" in smoke
        assert "N_AGENT_PROFILING_ENABLED" in smoke, (
            "Profile 分支必须按自身开关启用"
        )

    def test_disabled_path_asserts_disabled_and_exits_zero(self, smoke):
        disabled = re.search(
            r"run_disabled_checks\(\) \{(.+?)^\}", smoke, re.S | re.M
        )
        assert disabled, "缺少 run_disabled_checks()（disabled 断言集中点）"
        body = disabled.group(1)
        assert '"disabled"' in body, "disabled 分支必须断言 status=disabled"
        assert "delivery" in body, "disabled 分支必须断言各信号 delivery"
        assert "exit 0" in smoke, "disabled 路径必须以 0 退出（自门控）"

    def test_no_host_side_service_start(self, smoke):
        """宿主仅编排；禁止在宿主启动业务服务或观测栈。"""
        assert not re.search(r"uvicorn|python\s+(-m\s+)?app\.main", smoke), (
            "冒烟脚本不得在宿主启动 N-Agent 服务"
        )
        assert "127.0.0.1:8201" in smoke, "业务流量必须走容器发布端口"


@pytest.fixture(scope="module")
def smoke_code() -> str:
    """脚本正文（剥离注释行），避免文档性提及干扰行为断言。"""
    lines = [
        line
        for line in SMOKE_SOURCE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


class TestSmokeTempo:
    def test_uses_tempo_search_and_trace_by_id(self, smoke_code):
        helper = (REPO_ROOT / "tests/e2e/tempo_query.py").read_text()
        assert "/api/search?" in helper
        assert "/api/traces/" in helper
        assert "tempo_query.py" in smoke_code
        assert "/api/v3/" not in smoke_code
        assert "/api/v1/query_exemplars" in smoke_code

    def test_bounded_60s_polling(self, smoke):
        assert re.search(r"TEMPO_POLL_ATTEMPTS=60", smoke) or re.search(
            r"60.*poll|poll.*60", smoke
        ), "Tempo 轮询必须有 60 秒上界"
        assert "deadline=$((SECONDS + TEMPO_POLL_ATTEMPTS))" in smoke
        assert "PROM_POLL_ATTEMPTS=60" in smoke, "Prometheus 轮询必须有 60 秒上界"

    def test_asserts_invoke_agent_hierarchy_and_attributes(self, smoke):
        assert "invoke_agent" in smoke
        assert "gen_ai.request.model" in smoke
        assert "gen_ai.tool.name" in smoke
        assert "nagent.policy_outcome" in smoke
        assert "execute_tool" in smoke


class TestSmokeMetrics:
    def test_uses_locked_prometheus_names(self, smoke):
        for name in PROM_COUNTERS:
            assert name in smoke, f"缺少锁定 Counter 名称 {name}"
        for name in PROM_HISTOGRAMS:
            assert name in smoke, f"缺少锁定 Histogram 名称 {name}"



class TestSmokeFixture:
    def test_deterministic_fake_provider_wired_via_provider_factory(self, smoke):
        assert "/chat/providers" in smoke, (
            "固定 Provider 必须经 provider 注册/激活 API 接入 provider factory"
        )
        assert "calculator" in smoke, "必须使用安全测试工具 calculator"
        assert "chat/completions" in smoke, "必须经真实对话路径驱动 agent"

    def test_cleanup_restores_provider_and_removes_fixtures(self, smoke):
        assert "restore_provider" in smoke
        assert "stop_fake_llm" in smoke
        assert 'for session in "$SESSION_GOOD" "$SESSION_FAIL" "$SESSION_CANCEL"' in smoke
        assert "trap cleanup EXIT" in smoke

    def test_only_minimal_scenarios_remain(self, smoke_code):
        for removed in ("run_persistence_checks", "run_profile_checks", "apply_env_overlay",
                        "SESSION_ZEROCALL", "SUMMARY_ONLY", "docker restart", "recreate_nagent"):
            assert removed not in smoke_code
        assert "FORCE-LLM-500" in smoke_code
        assert 'check-failure "$TMP_DIR/trace-fail.json"' in smoke_code
        assert '"$CHAT_FINISH" = "error"' in smoke_code
        assert "DISCONNECT-PROBE" in smoke_code
        assert "time.sleep(3)" in smoke_code
        assert '"$CHAT_CURL_STATUS" = "28"' in smoke_code
        assert 'tempo_find_trace "$SESSION_CANCEL"' in smoke_code


class TestSmokeSentinel:
    def test_sentinel_asserted_absent_in_all_outlets(self, smoke):
        assert "SENTINEL" in smoke, "必须注入秘密哨兵"
        assert "api_key=$SENTINEL" in smoke, "详情允许正文后必须使用凭据格式哨兵"
        for outlet in ("tempo", "prometheus", "logs", "observations/otel"):
            assert outlet in smoke, f"缺少出口 {outlet} 的断言上下文"
        # count-based only: a bare "leak" substring would match the helper's
        # fail messages and pass vacuously
        assert "assert_no_sentinel() {" in smoke, "缺少集中的哨兵缺席断言 helper"
        sentinel_calls = smoke.count('assert_no_sentinel "')
        assert sentinel_calls >= 3, (
            f"哨兵缺席断言 helper 必须被实际调用（当前 {sentinel_calls} 处）"
        )


class TestExecutionSummary:
    def test_http_ingress_parents_agent(self, smoke):
        assert 'http["name"] in ("POST /chat/completions", "POST /v1/chat/completions")' in smoke
        assert "detached run context" not in smoke

    def test_session_lookup_waits_for_root_not_early_child(self, smoke):
        assert 'and s.get("name", "").startswith("invoke_agent ")' in smoke
        assert 'fail "Tempo good trace hierarchy invalid"' in smoke

    def test_delivery_check_is_wired_after_stderr(self, smoke):
        assert 'check_execution_summary.py' in smoke
        assert smoke.index('"application stderr JSON logs"') < smoke.index('check_execution_summaries "$TMP_DIR/trace-good.json"')
        assert 'python3 "$SCRIPT_DIR/check_execution_summary.py" fetch' in smoke
        assert '"$DOCKER_DIR/observability/restart.sh"' in smoke

    def test_matches_all_spans_and_rejects_indexed_identifiers(self):
        import importlib.util
        source = SMOKE_SOURCE.with_name("check_execution_summary.py")
        spec = importlib.util.spec_from_file_location("summary_smoke", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        spans, logs = [], []
        for i, operation in enumerate(("invoke_agent", "chat", "chat", "execute_tool")):
            attrs = {"nagent.summary": "structural summary", "gen_ai.conversation.id": "session", "nagent.run_id": "run"}
            spans.append({"name": operation + " fixture", "spanId": str(i), "parentSpanId": "" if i == 0 else "0", "attrs": attrs})
            logs.append({"event": "execution_completed", "trace_id": "trace", "span_id": str(i), "context": {"operation": operation, "summary": "structural summary", "session_id": "session", "request_id": "run", "duration_ms": 1, "outcome": "success"}})
        logs = [{**record, "context": {"execution": record["context"]}} for record in logs]
        trace = {"traceId": "trace", "spans": spans}
        module.check(trace, logs, [{"service_name": "n-agent"}])
        # HTTP ingress can contain title generation outside the Agent subtree.
        sibling = {"name": "chat title", "spanId": "title", "parentSpanId": "http", "attrs": {}}
        module.check({**trace, "spans": spans + [sibling]}, logs + [
            {"event": "execution_completed", "trace_id": "trace", "span_id": "title"}
        ], [])
        with pytest.raises(AssertionError):
            module.check(trace, logs[:-1], [])
        with pytest.raises(AssertionError):
            module.check(trace, logs + [logs[0]], [])
        for field in ("summary", "session_id", "request_id"):
            original = logs[1]["context"]["execution"][field]
            logs[1]["context"]["execution"][field] = "mismatch"
            with pytest.raises(AssertionError):
                module.check(trace, logs, [])
            logs[1]["context"]["execution"][field] = original
        with pytest.raises(AssertionError):
            module.check(trace, logs, [{"service_name": "n-agent", "request_id": "run"}])


def test_shared_backend_queries_use_independent_compose():
    source = SMOKE_SOURCE.read_text()
    assert 'obs_compose exec -T prometheus wget' in source
    assert 'obs_compose logs otel-collector' in source
    assert 'compose exec -T n-agent python3 - "$1"' not in source
    assert 'if compose exec -T n-agent python - "$TRACE_ID"' not in source
