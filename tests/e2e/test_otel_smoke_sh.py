"""宿主单测：otel_smoke.sh 观测冒烟编排脚本的静态合同（T10）。

锁定的契约（plan-261024 T10 S3 + 执行合同 item 22/23/24/25）：
- run.sh POSIX 再执行（sh 调用有效）且 all 列表接入 otel_smoke.sh，
  保留既有 suite 与一次性重建机制；继承观测环境变量不覆盖；
- otel_smoke.sh 自门控：观测关闭时只做 disabled 断言并以 0 退出；
- OTel/Profile 分支各按自身开关启用；
- Tempo 查询走 v2 的 /api/v3/*（legacy /api/services 已 404）+ 60 秒有界轮询；
- Prometheus 断言使用 T9 S4 锁定的实际导出名称（含 Counter _total 与
  Histogram _bucket/_sum/_count），不使用 spec 示例名；
- 大盘 UID nagent-overview 与 histogram_quantile p95 经真实 API 校验；
- ratio=0 无远端父上下文 -> 无已采样本地根 span 而指标继续增长；
- 零调用运行（vision 拒绝路径，零 LLM 调用完成 run）-> gen_ai usage
  token/调用指标零增量；
- 确定性 fixture：固定可控 Provider（calculator 安全工具，LLM->工具->LLM）
  实际接入 provider factory，且结束后恢复原 Provider；
- 环境覆盖（ratio=0 / 预算拒绝 / 压缩）须备份并恢复 docker/.env；
- 观测容器重启后持久卷数据仍可查（Prometheus/Pyroscope）；
- 全部业务流量在 Docker（BASE_URL + docker compose），宿主仅编排；
- 秘密哨兵不得出现在 Tempo/Prometheus/日志/自检出口。
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
    "nagent_context_compressions_total",
    "nagent_budget_denied_total",
)
PROM_HISTOGRAMS = (
    "gen_ai_client_inference_duration_seconds",
    "gen_ai_execute_tool_duration_seconds",
    "gen_ai_invoke_agent_duration_seconds",
    "gen_ai_invoke_agent_inference_calls",
    "gen_ai_invoke_agent_tool_calls",
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
        for suite in ("artifacts.sh", "config-bundle.sh", "conversations.sh"):
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

    def test_asserts_histogram_series_suffixes(self, smoke):
        for suffix in ("_bucket", "_sum", "_count"):
            assert suffix in smoke, f"缺少 Histogram 序列后缀断言 {suffix}"

    def test_asserts_ratio_zero_metrics_still_grow(self, smoke_code):
        assert re.search(
            r'N_AGENT_OTEL_TRACES_SAMPLE_RATIO["\']?\s*[:=]\s*["\']?0', smoke_code
        ), "ratio=0 覆盖缺失"
        assert "traces_in_window" in smoke_code or "ratio=0" in smoke_code

    def test_asserts_compression_and_budget_denial(self, smoke):
        assert "N_AGENT_BUDGET_MAX_LLM_CALLS" in smoke, "缺少预算拒绝触发"
        assert "N_AGENT_CONTEXT_COMPRESSION_THRESHOLD" in smoke, (
            "缺少压缩触发"
        )
        assert "nagent_budget_denied_total" in smoke
        assert "nagent_context_compressions_total" in smoke

    def test_asserts_zero_llm_call_run_no_usage_delta(self, smoke):
        """plan item 24: 显式零调用运行，gen_ai usage 指标不得增长。"""
        assert "SESSION_ZEROCALL" in smoke, "缺少零调用运行探针会话"
        assert "zero-LLM-call" in smoke, "缺少零调用运行 section"
        assert "prom_assert_no_delta" in smoke, "缺少指标无增量断言 helper"
        assert "prom_stable_value" in smoke, "缺少基线稳定化 helper"


class TestSmokeFixture:
    def test_deterministic_fake_provider_wired_via_provider_factory(self, smoke):
        assert "/chat/providers" in smoke, (
            "固定 Provider 必须经 provider 注册/激活 API 接入 provider factory"
        )
        assert "calculator" in smoke, "必须使用安全测试工具 calculator"
        assert "chat/completions" in smoke, "必须经真实对话路径驱动 agent"

    def test_provider_and_env_restored_after_run(self, smoke):
        assert "activate" in smoke, "必须重新激活原 Provider"
        assert re.search(r"cp .*docker/\.env|\.env\.otel-smoke", smoke), (
            "env 覆盖必须基于备份文件恢复 docker/.env"
        )
        assert "trap" in smoke, "必须有 cleanup trap 保证恢复"


class TestSmokePersistenceAndDashboard:
    def test_restarts_observability_containers_and_requeries(self, smoke_code):
        assert re.search(
            r'docker restart "\$prom_id" "\$pyro_id"', smoke_code
        ), "必须重启 Prometheus/Pyroscope 容器验证持久卷"
        assert "prom_sum" in smoke_code and "pyroscope_render" in smoke_code, (
            "重启后必须重新查询 Prometheus/Pyroscope 数据"
        )
        assert "run_persistence_checks" in smoke_code

    def test_dashboard_uid_and_p95_verified(self, smoke):
        assert "nagent-overview" in smoke, "必须校验大盘 UID"
        assert "histogram_quantile" in smoke, "p95 PromQL 必须经 API 执行"


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

    def test_summary_only_mode_runs_delivery_checks_and_normal_cleanup(self, smoke):
        assert 'N_AGENT_OTEL_SMOKE_SUMMARY_ONLY' in smoke
        assert smoke.index('check_execution_summaries "$TMP_DIR/trace-good.json"') < smoke.index('if [ "${N_AGENT_OTEL_SMOKE_SUMMARY_ONLY:-false}"')
        assert 'execution summary PASS' in smoke
        assert 'trap cleanup EXIT' in smoke

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
    assert '$(obs_compose ps -q prometheus)' in source
    assert 'compose exec -T n-agent python3 - "$1"' not in source
    assert 'if compose exec -T n-agent python - "$TRACE_ID"' not in source
