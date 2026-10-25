from pathlib import Path
import os
import json
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"


OBSERVABILITY_SERVICES = (
    "otel-collector",
    "tempo",
    "prometheus",
    "grafana",
    "pyroscope",
    "loki",
)


def _load_compose(compose_name: str) -> dict:
    compose_path = DOCKER_DIR / compose_name
    if compose_name == "docker-compose.yml" and not compose_path.exists():
        pytest.skip(
            "machine-local docker/docker-compose.yml is absent in a clean checkout"
        )
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


def _grace_seconds(value) -> float:
    text = str(value).strip()
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    unit = text[-1]
    assert unit in multipliers, f"unsupported stop_grace_period unit: {value}"
    return float(text[:-1]) * multipliers[unit]


def _parse_env_assignments(text: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid env assignment: {key}"
        assert key not in assignments, f"duplicate env assignment: {key}"
        assignments[key] = value
    return assignments


def _published_target_ranges(service: dict) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for port in service.get("ports", []):
        if isinstance(port, dict):
            target = str(port["target"])
        else:
            target = str(port).rsplit("/", 1)[0].rsplit(":", 1)[-1]
        first, separator, last = target.partition("-")
        start = int(first)
        end = int(last) if separator else start
        ranges.append((min(start, end), max(start, end)))
    return ranges


def _assert_sensitive_ports_not_published(document: dict) -> None:
    sensitive_ports = {8766, 9222, 6080}
    for service in document["services"].values():
        for start, end in _published_target_ranges(service):
            assert not any(
                start <= port <= end for port in sensitive_ports
            ), f"sensitive target port published: {start}-{end}"


def _host_browser_token_mount(service: dict) -> str:
    return next(
        item
        for item in service["volumes"]
        if ":/app/locals/host-browser.token:" in item
    )


def test_docker_compose_config():
    compose = (DOCKER_DIR / "docker-compose.yml.example").read_text()
    dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
    env = (DOCKER_DIR / ".env.example").read_text()

    assert "n-agent:" in compose
    assert '"8201:8201"' in compose
    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8201/health" in compose
    assert "network_mode: host" not in compose
    assert "build:" in compose
    assert "context: .." in compose
    assert "dockerfile: docker/Dockerfile" in compose
    assert "env_file:" in compose
    assert "path: ./.env" in compose
    assert "required: false" in compose
    assert "/app/locals" in compose
    assert "/workspace" in compose
    assert "TZ: Asia/Shanghai" in compose
    assert "N_AGENT_SQLITE_PATH: /app/locals/sessions.db" in compose
    assert "N_AGENT_WORKSPACE_ROOT: /workspace" in compose
    assert "n-kb_default" in compose
    assert "external: true" in compose
    assert "FROM python:3.11-slim" in dockerfile
    assert "ENV TZ=Asia/Shanghai" in dockerfile
    assert "tzdata" in dockerfile
    assert "/etc/localtime" in dockerfile
    assert '"uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8201"' in dockerfile
    assert "N_AGENT_KB_ENABLED" in env
    assert "N_AGENT_KB_BASE_URL" in env
    assert "http://n-kb:8212" in env


def test_env_example_documents_host_cdp_without_enabling_browser_by_default():
    env = _parse_env_assignments(
        (DOCKER_DIR / ".env.example").read_text(encoding="utf-8")
    )
    assert env["N_AGENT_BROWSER_ENABLED"] == "false"
    assert env["N_AGENT_BROWSER_DEFAULT_BACKEND"] == "host_cdp"
    assert (
        env["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "http://host.docker.internal:8766"
    )
    assert (
        env["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "/app/locals/host-browser.token"
    )
    assert env["N_AGENT_BROWSER_TRUSTED_DEV"] == "true"


def test_env_assignment_parser_rejects_duplicate_conflicts():
    with pytest.raises(AssertionError, match="duplicate env assignment"):
        _parse_env_assignments(
            "N_AGENT_BROWSER_ENABLED=false\n"
            "N_AGENT_BROWSER_ENABLED=true\n"
        )


def test_example_compose_adds_host_bridge_wiring_without_changing_topology():
    document = _load_compose("docker-compose.yml.example")
    assert set(document["services"]) == (
        {"n-agent", "browser"} | set(OBSERVABILITY_SERVICES)
    )
    assert set(document["networks"]) == {"default", "n-kb"}

    service = document["services"]["n-agent"]
    assert service["ports"] == ["8201:8201"]
    assert service["networks"] == ["default", "n-kb"]
    assert service["depends_on"] == {"browser": {"condition": "service_healthy"}}
    environment = service["environment"]
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_URL:-http://host.docker.internal:8766}"
    )
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH:-/app/locals/host-browser.token}"
    )
    assert (
        environment["N_AGENT_BROWSER_TRUSTED_DEV"]
        == "${N_AGENT_BROWSER_TRUSTED_DEV:-false}"
    )
    # .env.example documents the default backend; the compose environment
    # does not pin it.
    assert "N_AGENT_BROWSER_DEFAULT_BACKEND" not in environment
    assert _host_browser_token_mount(service).endswith(":ro")
    browser = document["services"]["browser"]
    assert browser["networks"] == {
        "default": {"ipv4_address": "172.19.0.10"}
    }
    assert browser["expose"] == ["9223", "6080"]
    assert "ports" not in browser


def test_current_machine_compose_adds_host_bridge_wiring_without_forcing_backend():
    document = _load_compose("docker-compose.yml")
    assert set(document["services"]) == (
        {"n-agent", "browser"} | set(OBSERVABILITY_SERVICES)
    )
    assert set(document["networks"]) == {"default", "n-kb"}

    service = document["services"]["n-agent"]
    assert service["ports"] == ["8201:8201"]
    assert service["networks"] == ["default", "n-kb"]
    assert service["depends_on"] == {"browser": {"condition": "service_healthy"}}
    environment = service["environment"]
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_URL:-http://host.docker.internal:8766}"
    )
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH:-/app/locals/host-browser.token}"
    )
    assert (
        environment["N_AGENT_BROWSER_TRUSTED_DEV"]
        == "${N_AGENT_BROWSER_TRUSTED_DEV:-false}"
    )
    assert "N_AGENT_BROWSER_DEFAULT_BACKEND" not in environment
    assert (
        environment["N_AGENT_BROWSER_CONTAINER_NOVNC_ENDPOINT"]
        == "${N_AGENT_BROWSER_CONTAINER_NOVNC_ENDPOINT:-http://browser:6080}"
    )
    assert _host_browser_token_mount(service).endswith(":ro")
    browser = document["services"]["browser"]
    assert browser["networks"] == {
        "default": {"ipv4_address": "172.19.0.10"}
    }
    assert browser["expose"] == ["9223", "6080"]
    assert "ports" not in browser


def test_container_browser_uses_4_by_3_viewport():
    """Container Xvfb and Chromium use a 1280x960 viewport."""
    document = _load_compose("docker-compose.yml")
    browser = document["services"]["browser"]
    assert browser["environment"]["SCREEN_WIDTH"] == 1280
    assert browser["environment"]["SCREEN_HEIGHT"] == 960

    browser_dockerfile = (DOCKER_DIR / "browser" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    runtime = (DOCKER_DIR / "browser" / "profile_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "SCREEN_WIDTH=1280" in browser_dockerfile
    assert "SCREEN_HEIGHT=960" in browser_dockerfile
    assert 'HEIGHT = os.environ.get("SCREEN_HEIGHT", "960")' in runtime

@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_host_bridge_is_not_exposed_or_given_host_networking(compose_name):
    document = _load_compose(compose_name)
    serialized = (DOCKER_DIR / compose_name).read_text(encoding="utf-8")
    for service in document["services"].values():
        assert "network_mode" not in service
        assert "extra_hosts" not in service
    _assert_sensitive_ports_not_published(document)
    assert "network_mode: host" not in serialized
    assert "host-browser-bridge" not in document["services"]
    assert "browser-host" not in document["services"]


@pytest.mark.parametrize(
    "ports",
    [
        ["127.0.0.1:9222:9222"],
        ["12345:9222"],
        [{"target": 6080, "published": 12345, "protocol": "tcp", "mode": "host"}],
        ["12345-12347:9221-9223"],
    ],
)
def test_sensitive_port_helper_rejects_all_compose_publication_syntaxes(ports):
    document = {"services": {"malicious": {"ports": ports}}}
    with pytest.raises(AssertionError):
        _assert_sensitive_ports_not_published(document)


def test_machine_local_compose_is_optional_in_clean_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tests.test_docker_compose_config.DOCKER_DIR", tmp_path
    )
    with pytest.raises(
        pytest.skip.Exception, match="machine-local.*absent"
    ):
        _load_compose("docker-compose.yml")


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_host_terminal_text_and_yaml_contract_is_safe(compose_name):
    compose_path = DOCKER_DIR / compose_name
    document = _load_compose(compose_name)
    service = document["services"]["n-agent"]
    environment = service["environment"]
    required_environment = {
        "N_AGENT_HOST_TERMINAL_ENABLED",
        "N_AGENT_HOST_TERMINAL_BRIDGE_URL",
        "N_AGENT_HOST_TERMINAL_POLICY_PATH",
        "N_AGENT_HOST_TERMINAL_TOKEN_PATH",
        "N_AGENT_HOST_TERMINAL_HOST_WORKSPACE_ROOT",
        "N_AGENT_HOST_TERMINAL_HOST_SKILLS_ROOT",
        "N_AGENT_HOST_TERMINAL_TOOL_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_BRIDGE_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_CONNECT_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_TRANSFER_MARGIN_SECONDS",
        "N_AGENT_HOST_TERMINAL_MAX_RESPONSE_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_STDOUT_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_STDERR_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_CONCURRENCY",
    }
    assert required_environment <= set(environment)
    assert "host.docker.internal" in environment["N_AGENT_HOST_TERMINAL_BRIDGE_URL"]

    volumes = service["volumes"]
    policy_mount = next(
        item for item in volumes
        if ":/app/locals/host-terminal-policy.yaml:" in item
    )
    token_mount = next(
        item for item in volumes if ":/app/locals/host-terminal.token:" in item
    )
    assert policy_mount.endswith(":ro")
    assert token_mount.endswith(":ro")
    serialized = compose_path.read_text(encoding="utf-8")
    assert "oss.env" not in serialized.lower()
    assert not any(str(key).startswith("OSS_") for key in environment)



@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_config_subprocess(compose_name):
    compose_path = DOCKER_DIR / compose_name
    if compose_name == "docker-compose.yml" and not compose_path.exists():
        pytest.skip(
            "machine-local docker/docker-compose.yml is absent in a clean checkout"
        )
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    completed = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=False,
        check=False,
    )
    assert completed.returncode == 0


def test_machine_local_compose_has_no_hardcoded_home_paths():
    compose_path = DOCKER_DIR / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("machine-local docker/docker-compose.yml is absent in a clean checkout")
    serialized = compose_path.read_text(encoding="utf-8")
    assert "/Users/niean" not in serialized
    # 必填形式：裸 ${VAR} 在 compose 下只告警并代入空串
    assert "${N_AGENT_INSTALL_ROOT:?" in serialized
    assert "${N_AGENT_CODE_ROOT:?" in serialized
    assert "${N_AGENT_INSTALL_ROOT}" not in serialized
    assert "${N_AGENT_CODE_ROOT}" not in serialized


def test_env_example_declares_deployment_roots():
    env = _parse_env_assignments((DOCKER_DIR / ".env.example").read_text(encoding="utf-8"))
    assert "N_AGENT_INSTALL_ROOT" in env
    assert "N_AGENT_CODE_ROOT" in env


def test_example_compose_keeps_minimal_topology_without_required_roots():
    serialized = (DOCKER_DIR / "docker-compose.yml.example").read_text(encoding="utf-8")
    # .example 必须能在没有 docker/.env 的干净 checkout 下 `docker compose config` 成功
    assert ":?" not in serialized


@pytest.mark.parametrize(
    "install_root, code_root, missing",
    [
        (None, "/tmp/code", "N_AGENT_INSTALL_ROOT"),
        ("", "/tmp/code", "N_AGENT_INSTALL_ROOT"),
        ("/tmp/install", None, "N_AGENT_CODE_ROOT"),
        ("/tmp/install", "", "N_AGENT_CODE_ROOT"),
    ],
)
def test_machine_local_compose_requires_both_roots(install_root, code_root, missing):
    """缺失或空值时 `:?` 必须让 compose 直接报错，而不是代入空串产生错误挂载。"""
    compose_path = DOCKER_DIR / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("machine-local docker/docker-compose.yml is absent in a clean checkout")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"N_AGENT_INSTALL_ROOT", "N_AGENT_CODE_ROOT"}
    }
    if install_root is not None:
        env["N_AGENT_INSTALL_ROOT"] = install_root
    if code_root is not None:
        env["N_AGENT_CODE_ROOT"] = code_root
    completed = subprocess.run(
        [
            "docker", "compose",
            "--env-file", "/dev/null",
            "-f", str(compose_path),
            "config", "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode != 0
    assert missing in completed.stderr


def test_machine_local_compose_config_succeeds_when_both_roots_are_set():
    compose_path = DOCKER_DIR / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("machine-local docker/docker-compose.yml is absent in a clean checkout")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    env = {
        **os.environ,
        "N_AGENT_INSTALL_ROOT": "/tmp/install",
        "N_AGENT_CODE_ROOT": "/tmp/code",
    }
    completed = subprocess.run(
        [
            "docker", "compose",
            "--env-file", "/dev/null",
            "-f", str(compose_path),
            "config", "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# T9: observability profile backend stack (spec section 7)
# ---------------------------------------------------------------------------


def _load_observability_compose(compose_name: str = "docker-compose.yml") -> dict:
    document = _load_compose(compose_name)
    for name in OBSERVABILITY_SERVICES:
        assert name in document["services"], (
            f"observability profile service missing: {name}"
        )
    return document


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_services_are_profile_isolated(compose_name):
    """观测栈服务必须全部声明 observability profile，n-agent 不得硬依赖。"""
    document = _load_observability_compose(compose_name)
    for name in OBSERVABILITY_SERVICES:
        service = document["services"][name]
        assert "observability" in service.get("profiles", []), (
            f"{name} must declare the observability profile"
        )
    n_agent = document["services"]["n-agent"]
    depends_on = n_agent.get("depends_on", {})
    assert set(depends_on) == {"browser"}, (
        f"n-agent must not hard-depend on observability services: {set(depends_on)}"
    )
    for name in OBSERVABILITY_SERVICES:
        assert name not in depends_on


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_profile_disabled_by_default_compose_services(compose_name):
    """未启用 profile 时 compose 不解析出观测栈服务，启用后才出现。"""
    compose_path = DOCKER_DIR / compose_name
    if compose_name == "docker-compose.yml" and not compose_path.exists():
        pytest.skip("machine-local docker/docker-compose.yml is absent in a clean checkout")
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    env = {
        **os.environ,
        "N_AGENT_INSTALL_ROOT": "/tmp/install",
        "N_AGENT_CODE_ROOT": "/tmp/code",
    }

    def _services(*extra: str) -> set[str]:
        completed = subprocess.run(
            [
                "docker", "compose", "--env-file", "/dev/null", *extra,
                "-f", str(compose_path), "config", "--services",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert completed.returncode == 0, completed.stderr
        return {line.strip() for line in completed.stdout.splitlines() if line.strip()}

    assert _services() == {"n-agent", "browser"}
    assert _services("--profile", "observability") == (
        {"n-agent", "browser"} | set(OBSERVABILITY_SERVICES)
    )


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_ui_ports_loopback_and_collector_not_published(compose_name):
    """UI 端口绑定 127.0.0.1；Collector/Prometheus 默认不发布宿主端口。"""
    document = _load_observability_compose(compose_name)
    for name in ("otel-collector", "prometheus", "loki"):
        service = document["services"][name]
        assert not service.get("ports"), (
            f"{name} must not publish host ports by default"
        )
    for name in ("tempo", "grafana", "pyroscope"):
        service = document["services"][name]
        ports = service.get("ports", [])
        assert ports, f"{name} must expose its UI on 127.0.0.1"
        for entry in ports:
            published = str(entry).split(":")[0]
            assert published == "127.0.0.1", (
                f"{name} UI port must bind 127.0.0.1, got: {entry}"
            )


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_images_are_version_pinned(compose_name):
    document = _load_observability_compose(compose_name)
    expected_images = {
        "otel-collector": "otel/opentelemetry-collector-contrib:0.137.0",
        "tempo": "grafana/tempo:2.9.0",
        "prometheus": "prom/prometheus:v3.15.1",
        "grafana": "grafana/grafana:13.0.11",
        "pyroscope": "grafana/pyroscope:1.21.1",
        "loki": "grafana/loki:3.5.5",
    }
    for name, image in expected_images.items():
        assert document["services"][name]["image"] == image
    serialized = (DOCKER_DIR / compose_name).read_text(encoding="utf-8")
    assert ":latest" not in serialized


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_prometheus_enables_remote_write_receiver_and_retention(compose_name):
    document = _load_observability_compose(compose_name)
    prometheus = document["services"]["prometheus"]
    command = prometheus["command"]
    assert isinstance(command, list)
    joined = " ".join(str(part) for part in command)
    assert "--web.enable-remote-write-receiver" in joined
    assert "--storage.tsdb.retention.time=7d" in joined


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_stop_grace_period_at_least_10s(compose_name):
    document = _load_observability_compose(compose_name)
    for name in ("n-agent", *OBSERVABILITY_SERVICES):
        service = document["services"][name]
        grace = _grace_seconds(service["stop_grace_period"])
        assert grace >= 10, f"{name} stop_grace_period must be >= 10s, got {grace}"


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_config_mounts_are_readonly(compose_name):
    document = _load_observability_compose(compose_name)
    expected_mounts = {
        "otel-collector": "/etc/otelcol-contrib/config.yaml",
        "tempo": "/etc/tempo/config.yaml",
        "prometheus": "/etc/prometheus/prometheus.yml",
        "grafana": "/etc/grafana/provisioning",
        "pyroscope": "/etc/pyroscope/config.yaml",
        "loki": "/etc/loki/config.yaml",
    }
    for name, target in expected_mounts.items():
        service = document["services"][name]
        mount = next(
            (
                item
                for item in service.get("volumes", [])
                if str(item).rsplit(":", 1)[0].endswith(target.split("/")[-1])
                and target in str(item)
            ),
            None,
        )
        assert mount is not None, f"{name} must mount its config at {target}"
        assert str(mount).endswith(":ro"), f"{name} config mount must be :ro: {mount}"


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_named_volumes_declared(compose_name):
    document = _load_observability_compose(compose_name)
    expected_targets = {
        "prometheus-data": "/prometheus",
        "grafana-data": "/var/lib/grafana",
        "pyroscope-data": "/var/lib/pyroscope",
        "loki-data": "/loki",
    }
    for volume_name, target in expected_targets.items():
        assert volume_name in document.get("volumes", {}), (
            f"named volume missing: {volume_name}"
        )
        service = document["services"][
            {"prometheus-data": "prometheus", "grafana-data": "grafana",
             "pyroscope-data": "pyroscope", "loki-data": "loki"}[volume_name]
        ]
        mount = next(
            item for item in service["volumes"]
            if str(item).split(":")[0] == volume_name
        )
        assert target in str(mount), mount


def test_prometheus_scrapes_observability_stack_self_metrics():
    """观测栈自监控抓取合同：业务指标经 remote write，栈组件经 scrape。"""
    config_path = DOCKER_DIR / "observability" / "prometheus.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    expected_targets = {
        "prometheus-self": ["localhost:9090"],
        "otel-collector": ["otel-collector:8888"],
        "loki": ["loki:3100"],
        "pyroscope": ["pyroscope:4040"],
        "tempo": ["tempo:3200"],
    }
    assert set(jobs) == set(expected_targets), jobs
    for name, targets in expected_targets.items():
        static = jobs[name]["static_configs"][0]
        assert static["targets"] == targets, name


def test_collector_config_three_pipelines_bounded_queues():
    config_path = DOCKER_DIR / "observability" / "otel-collector.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    receivers = config["receivers"]
    grpc = receivers["otlp"]["protocols"]["grpc"]
    assert "4317" in str(grpc["endpoint"])

    processors = config["processors"]
    assert "memory_limiter" in processors
    assert "batch" in processors
    # transform 把 log body 改写为 JSON 行（event+trace_id），Loki 数据源的
    # derivedFields 依赖行内 trace_id 实现 Log -> Jaeger 跳转。
    loki_transform = processors["transform/loki_json"]
    assert loki_transform["error_mode"] == "ignore"
    statements = loki_transform["log_statements"][0]["statements"]
    assert any('"trace_id": trace_id.string' in s for s in statements)
    # Preserve only the approved execution facts, never arbitrary log extras.
    fields = {
        "session_id", "request_id", "source", "operation", "summary", "duration_ms",
        "outcome", "error_type", "message_count", "inference_calls", "tool_calls",
        "input_tokens", "output_tokens", "usage_known", "end_reason", "model", "provider",
        "iteration", "stream", "finish_reason", "tool", "call_id", "risk_level",
        "policy_outcome", "executed",
    }
    for key in fields:
        assert any(f'attributes["{key}"]' in s and 'cache["execution"]' in s for s in statements)
    # event 取自 attributes（不存在时回到固定诊断码），绝不退回 body 作事件码。
    assert any('attributes["event"]' in s for s in statements)
    assert any('"log_event_unknown"' in s for s in statements)
    assert not any('"event": body' in s for s in statements)
    # 改写 body 前缓存原字符串为 message。
    assert any(s.startswith('set(cache["message"]') for s in statements)
    assert any('attributes["exception.type"]' in s and 'cache["exception"]' in s for s in statements)
    assert any('attributes["stack"]' in s and 'cache["stack"]' in s for s in statements)
    branches = [s for s in statements if s.startswith('set(body,')]
    assert len(branches) == 2
    # context.execution 嵌套（本轮 spec 迁移目标）。
    assert all('"context": {"execution": cache["execution"]}' in s for s in branches)
    assert not any('"context": attributes' in s for s in statements)

    empty_filter = processors["filter/empty_unknown_logs"]
    assert empty_filter["error_mode"] == "ignore"
    conditions = empty_filter["logs"]["log_record"]
    assert len(conditions) == 1  # OTTL filter entries are ORed; guards stay together.
    condition = conditions[0]
    for guard in (
        'body["event"] == "log_event_unknown"',
        'severity_number < SEVERITY_NUMBER_WARN',
        'Len(body["details"]) == 0',
        '(body["message"] == nil or body["message"] == "" or body["message"] == "log_event_unknown")',
        'body["exception"] == nil',
        '(body["stack"] == nil or body["stack"] == "")',
    ):
        assert guard in condition
    assert 'Len(body["context"]["execution"])' not in condition
    for key in ("name", "version", "decision_kind", "reason", "outcome", "run_id", "session_id", "policy_scope", "occurred_at"):
        assert any(f'attributes["policy.{key}"]' in s and 'cache["execution"]' in s for s in statements)

    exporters = config["exporters"]
    jaeger_endpoint = exporters["otlp/tempo"]["endpoint"]
    assert "tempo" in jaeger_endpoint and "4317" in jaeger_endpoint
    assert exporters["prometheusremotewrite"]["endpoint"] == (
        "http://prometheus:9090/api/v1/write"
    )
    assert exporters["debug"]["verbosity"] == "detailed"
    assert exporters["otlphttp/loki"]["endpoint"] == "http://loki:3100/otlp"

    pipelines = config["service"]["pipelines"]
    assert set(pipelines) == {"traces", "metrics", "logs"}
    assert pipelines["traces"]["exporters"] == ["otlp/tempo"]
    assert pipelines["metrics"]["exporters"] == ["prometheusremotewrite"]
    # Loki 为日志主交付后端；debug exporter 保留 stdout 交付证据。
    assert pipelines["logs"]["exporters"] == ["otlphttp/loki", "debug"]
    assert pipelines["logs"]["processors"] == [
        "memory_limiter", "transform/loki_json", "filter/empty_unknown_logs", "batch",
    ]
    for pipeline in pipelines.values():
        assert "memory_limiter" in pipeline["processors"]
        assert "batch" in pipeline["processors"]

    for exporter_name, queue_key in (
        ("otlp/tempo", "sending_queue"),
        ("otlphttp/loki", "sending_queue"),
        # prometheusremotewrite exporter (0.137) exposes its bounded queue
        # under remote_write_queue rather than the shared sending_queue key.
        ("prometheusremotewrite", "remote_write_queue"),
    ):
        exporter = exporters[exporter_name]
        queue = exporter[queue_key]
        assert int(queue["queue_size"]) > 0, (
            f"{exporter_name} sending queue must be bounded"
        )
        retry = exporter["retry_on_failure"]
        assert "initial_interval" in retry
        assert "max_interval" in retry


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_resource_limits_healthchecks_no_curl(compose_name):
    document = _load_observability_compose(compose_name)
    for name in OBSERVABILITY_SERVICES:
        service = document["services"][name]
        assert service.get("mem_limit"), f"{name} must declare a memory limit"
        assert service.get("cpus"), f"{name} must declare a CPU limit"
        if "healthcheck" in service:
            command = service["healthcheck"]["test"]
            joined = " ".join(str(part) for part in command)
            assert "curl" not in joined, (
                f"{name} healthcheck must not assume curl: {joined}"
            )
    for name in ("otel-collector", "prometheus", "grafana"):
        assert "healthcheck" in document["services"][name], (
            f"{name} must define a healthcheck"
        )
    collector_logging = document["services"]["otel-collector"].get("logging", {})
    options = collector_logging.get("options", {})
    assert "max-size" in options and "max-file" in options, (
        "collector stdout must be size-rotated by the docker log driver"
    )


def test_observability_grafana_provisioning_uses_service_names():
    datasources_path = (
        DOCKER_DIR / "observability" / "grafana" / "provisioning" / "datasources"
    )
    files = list(datasources_path.glob("*.yaml"))
    assert files, "grafana datasources provisioning must exist"
    urls = {}
    for path in files:
        for datasource in yaml.safe_load(path.read_text(encoding="utf-8"))["datasources"]:
            urls[datasource["name"]] = datasource["url"]
    assert urls["Prometheus"] == "http://prometheus:9090"
    assert urls["Tempo"] == "http://tempo:3200"
    assert urls["Pyroscope"] == "http://pyroscope:4040"
    assert urls["Loki"] == "http://loki:3100"

    dashboards_dir = (
        DOCKER_DIR / "observability" / "grafana" / "provisioning" / "dashboards"
    )
    expected_uids = {
        "nagent-overview.json": "nagent-overview",
        "nagent-metrics.json": "nagent-metrics",
        "nagent-logs.json": "nagent-logs",
        "nagent-infra.json": "nagent-infra",
    }
    for filename, uid in expected_uids.items():
        dashboard_path = dashboards_dir / filename
        assert dashboard_path.exists(), f"{filename} dashboard must be provisioned"
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        assert dashboard["uid"] == uid
        assert dashboard["title"]
        assert dashboard["panels"], f"{filename} must have panels"


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_observability_stack_declares_no_secrets(compose_name):
    document = _load_observability_compose(compose_name)
    forbidden = ("API_KEY", "SECRET", "TOKEN", "PASSWORD")
    for name in OBSERVABILITY_SERVICES:
        service = document["services"][name]
        environment = service.get("environment", {})
        if isinstance(environment, dict):
            keys = [str(key) for key in environment]
        else:
            keys = [str(entry).split("=", 1)[0] for entry in environment]
        for key in keys:
            assert not any(word in key.upper() for word in forbidden), (
                f"{name} environment must not declare secret-like vars: {key}"
            )


def test_env_example_documents_observability_switches():
    env = _parse_env_assignments(
        (DOCKER_DIR / ".env.example").read_text(encoding="utf-8")
    )
    assert env["N_AGENT_OTEL_ENABLED"] == "false"
    assert env["N_AGENT_OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4317"
    assert env["N_AGENT_OTEL_TRACES_SAMPLE_RATIO"] == "1.0"
    assert env["N_AGENT_OTEL_GRAFANA_METRICS_BASE_URL"] == "http://127.0.0.1:13000/d/nagent-metrics"
    assert env["N_AGENT_OTEL_GRAFANA_LOGS_BASE_URL"] == "http://127.0.0.1:13000/d/nagent-logs"
    assert env["N_AGENT_OTEL_GRAFANA_PROFILE_BASE_URL"] == "http://127.0.0.1:13000/d/nagent-profile"
    assert env["N_AGENT_OTEL_GRAFANA_TRACES_BASE_URL"] == "http://127.0.0.1:13000/explore"
    assert env["N_AGENT_PROFILING_ENABLED"] == "false"
    assert env["N_AGENT_PROFILING_PYROSCOPE_ENDPOINT"] == "http://pyroscope:4040"
    assert env["N_AGENT_PROFILING_INTERVAL_SECONDS"] == "60"


@pytest.mark.parametrize('compose_name', ['docker-compose.yml.example', 'docker-compose.yml'])
def test_content_budget_defaults_to_2048_in_compose(compose_name):
    environment = _load_compose(compose_name)['services']['n-agent']['environment']
    assert environment['N_AGENT_OTEL_CONTENT_MAX_BYTES'] == '${N_AGENT_OTEL_CONTENT_MAX_BYTES:-2048}'


def test_collector_projects_only_approved_detail_fields_and_keeps_detail_logs():
    config = yaml.safe_load((DOCKER_DIR / 'observability/otel-collector.yaml').read_text())
    statements = config['processors']['transform/loki_json']['log_statements'][0]['statements']
    assert 'set(cache["details"], {})' in statements
    for name in ('user_message', 'assistant_message', 'system_prompt', 'input_messages',
                 'tool_definitions', 'output_messages', 'tool_arguments', 'tool_result', 'error'):
        for metadata in ('content', 'original_bytes', 'truncated', 'incomplete', 'observed_bytes'):
            assert any(f'attributes["nagent.details.{name}.{metadata}"]' in s and f'cache["details"]["{name}"]["{metadata}"]' in s for s in statements)
    bodies = [s for s in statements if s.startswith('set(body,')]
    assert len(bodies) == 2
    assert all('"details": cache["details"]' in s for s in bodies)
    assert not any('set(cache["details"], attributes)' in s for s in statements)
    condition = config['processors']['filter/empty_unknown_logs']['logs']['log_record'][0]
    assert 'Len(body["details"]) == 0' in condition


def test_tempo_correlation_and_persistent_storage():
    config = yaml.safe_load((DOCKER_DIR / "observability/tempo.yaml").read_text())
    assert config["compactor"]["compaction"]["block_retention"] == "168h"
    assert config["storage"]["trace"]["backend"] == "local"
    for name in ("docker-compose.yml.example", "docker-compose.yml"):
        compose = _load_compose(name)
        assert "tempo-data:/var/tempo" in compose["services"]["tempo"]["volumes"]
        assert "--enable-feature=exemplar-storage" in compose["services"]["prometheus"]["command"]
    path = DOCKER_DIR / "observability/grafana/provisioning/datasources/datasources.yaml"
    sources = {d["name"]: d for d in yaml.safe_load(path.read_text())["datasources"]}
    tempo = sources["Tempo"]
    assert tempo["uid"] == "nagent-tempo"
    destinations = sources["Prometheus"]["jsonData"]["exemplarTraceIdDestinations"]
    assert destinations == [{"name": "trace_id", "datasourceUid": tempo["uid"]}]
    logs = tempo["jsonData"]["tracesToLogsV2"]
    assert logs["datasourceUid"] == sources["Loki"]["uid"]
    assert logs["tags"] == [{"key": "service.name", "value": "service_name"}]
    assert logs["filterByTraceID"] is True
    assert logs["filterBySpanID"] is False
    field = sources["Loki"]["jsonData"]["derivedFields"][0]
    assert field["datasourceUid"] == tempo["uid"]
    assert field["url"] == "$${__value.raw}"
