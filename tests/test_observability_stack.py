"""Deployment controller tests use a fake Docker engine, never real mutation."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "docker/observability"


@pytest.mark.parametrize(("args", "expected"), [
    ([], ["deploy"]), (["restart"], ["deploy"]), (["start"], ["deploy"]),
    (["stop"], ["stop"]), (["migrate", "--legacy-project", "old"], ["migrate", "--legacy-project", "old"]),
    (["compose", "ps"], ["compose", "ps"]),
])
def test_single_entrypoint_dispatches_without_docker(tmp_path, args, expected):
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    result = subprocess.run(["sh", str(STACK / "restart.sh"), *args], cwd=tmp_path,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(STACK / "manage-stack.py"), *expected]


def test_single_entrypoint_rejects_unknown_action_without_invoking_python(tmp_path):
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\necho unexpected invocation\n')
    python.chmod(0o755)
    result = subprocess.run(["sh", str(STACK / "restart.sh"), "invalid"],
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
                            capture_output=True, text=True)
    assert result.returncode == 2 and not result.stdout


def load_manager():
    spec = importlib.util.spec_from_file_location("obs_stack", STACK / "manage-stack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_compose_networks_and_persistence():
    doc = yaml.safe_load((STACK / "docker-compose.yml").read_text())
    assert doc["name"] == "n-observability"
    assert set(doc["services"]) == {"otel-collector", "tempo", "prometheus", "grafana", "loki", "pyroscope"}
    assert doc["networks"]["telemetry"] == {"external": True, "name": "n-telemetry"}
    assert len(doc["volumes"]) == 8
    assert all(v["external"] for v in doc["volumes"].values())
    for name, service in doc["services"].items():
        assert not service.get("profiles")
        assert set(service["networks"]) == ({"default", "telemetry"} if name in {"otel-collector", "pyroscope"} else {"default"})
        assert service["stop_grace_period"] == "15s"
        assert service["mem_limit"] and service["cpus"]
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
    pyro = doc["services"]["pyroscope"]["volumes"]
    assert {v.split(":")[1] for v in pyro} >= {"/data", "/data-compactor", "/data-metastore"}
    assert "healthcheck" not in doc["services"]["pyroscope"]


def test_pyroscope_uses_persisted_bucket_and_explicit_nonzero_disk_thresholds():
    config = yaml.safe_load((STACK / "pyroscope.yaml").read_text())
    assert config["storage"] == {"backend": "filesystem", "filesystem": {"dir": "/var/lib/pyroscope"}}
    # v1.21.1 only overrides defaults for >0 values; zero does not disable it.
    assert config["pyroscopedb"]["min_free_disk_gb"] == 1
    assert config["pyroscopedb"]["min_disk_available_percentage"] == 0.01
    assert config["limits"]["compactor_blocks_retention_period"] == "168h"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    mod = load_manager()
    directory = tmp_path / "stack"
    directory.mkdir()
    (directory / ".env.example").write_text("")
    state = SimpleNamespace(calls=[], config={"services": {}, "volumes": {}, "networks": {}},
                            containers={}, volumes=set(), network=False, fail=None,
                            target_exists=False)
    def run(args, **kwargs):
        state.calls.append((list(args), kwargs.get("env", {})))
        command = list(args)
        out, code = "", 0
        if "--project-name" in command and "up" in command:
            state.target_exists = True
        if state.fail and state.fail(command):
            code = 1
        elif "config" in command:
            out = json.dumps(state.config)
        elif "--project-name" in command and "ps" in command:
            out = "new-target" if state.target_exists else ""
        elif "--project-name" in command and "rm" in command:
            state.target_exists = False
        elif command[1:3] == ["network", "inspect"]:
            code, out = (0, json.dumps([{"Driver": "bridge"}])) if state.network else (1, "")
        elif command[1:3] == ["network", "create"]:
            state.network = True
        elif command[1:3] == ["volume", "inspect"]:
            code = 0 if command[3] in state.volumes else 1
        elif command[1:3] == ["volume", "create"]:
            state.volumes.add(command[3])
        elif command[1:3] == ["ps", "-aq"]:
            out = "\n".join(state.containers)
        elif command[1:3] == ["ps", "-q"]:
            volume = command[-1].removeprefix("volume=")
            out = "\n".join(key for key, item in state.containers.items() if item["State"]["Running"]
                            and any(m.get("Name") == volume for m in item["Mounts"]))
        elif command[1] == "inspect":
            out = json.dumps([state.containers[c] for c in command[2:]])
        elif command[1] == "stop":
            state.containers[command[-1]]["State"]["Running"] = False
        elif command[1] == "start":
            for key in command[2:]:
                state.containers[key]["State"]["Running"] = True
        result = SimpleNamespace(returncode=code, stdout=out, stderr="fake failure" if code else "")
        if kwargs.get("check") and code:
            raise mod.subprocess.CalledProcessError(code, command, output=out, stderr=result.stderr)
        return result
    monkeypatch.setattr(mod.subprocess, "run", run)
    manager = mod.Stack(directory)
    return mod, manager, state


def configuration(mod, stack):
    config = {"services": {}, "volumes": {}, "networks": {"telemetry": {"name": "n-telemetry", "external": True}}}
    for service in mod.SERVICES:
        config["services"][service] = {"volumes": [], "networks": {"default": {}}}
    for logical, (service, target, variable) in mod.VOLUMES.items():
        config["volumes"][logical] = {"name": "test_" + logical, "external": True}
        config["services"][service]["volumes"].append({"type": "volume", "source": logical, "target": target})
    for name in ("otel-collector", "pyroscope"):
        config["services"][name]["networks"]["telemetry"] = {}
    return config


def test_compose_isolates_inherited_settings_and_keeps_volume_map(engine, monkeypatch):
    mod, stack, state = engine
    monkeypatch.setenv("COMPOSE_FILE", "/wrong/compose.yml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "n-agent")
    monkeypatch.setenv("COMPOSE_PROJECT_DIR", "/wrong")
    monkeypatch.setenv("COMPOSE_ENV_FILES", "/secret")
    monkeypatch.setenv("COMPOSE_PROFILES", "observability")
    mapping = {key: "old_" + key for key in mod.VOLUMES}
    stack.save_mapping(mapping)
    stack.compose("ps")
    command, env = state.calls[-1]
    assert command[command.index("--project-name") + 1] == "n-observability"
    assert command[command.index("--project-directory") + 1] == str(stack.directory)
    assert not any(k.startswith("COMPOSE_") for k in env)
    assert env[mod.VOLUMES["tempo-data"][2]] == "old_tempo-data"


def test_invalid_mapping_refused_before_docker(engine):
    mod, stack, state = engine
    stack.mapping_file.write_text('{"tempo-data":"bad/name"}')
    with pytest.raises(mod.StackError):
        stack.compose("up", "-d")
    assert not state.calls


def test_deploy_preflights_before_mutation_and_initializes_external_resources(engine, monkeypatch):
    mod, stack, state = engine
    state.config = configuration(mod, stack)
    monkeypatch.setattr(stack, "wait_ready", lambda: None)
    stack.deploy()
    commands = [c for c, _ in state.calls]
    assert "config" in commands[0]
    assert len(state.volumes) == 8 and state.network
    assert any(c[-3:] == ["up", "-d", "--force-recreate"] for c in commands)
    assert commands[-1][-1] == "ps"


def test_deploy_reports_stages_before_blocking_work(engine, monkeypatch, capsys):
    mod, stack, state = engine
    state.config = configuration(mod, stack)
    def ready():
        assert "等待服务就绪" in capsys.readouterr().err
    monkeypatch.setattr(stack, "wait_ready", ready)
    stack.deploy()
    assert "启动完成" in capsys.readouterr().err


def test_lifecycle_compose_output_is_live_but_config_stays_captured(engine, monkeypatch):
    mod, stack, state = engine
    calls = []
    original = mod.subprocess.run
    def run(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(mod.subprocess, "run", run)
    stack.compose("config", "--format", "json")
    stack.compose("up", "-d", live=True)
    assert calls[0]["capture_output"] is True
    assert calls[1]["capture_output"] is False


def test_long_command_emits_waiting_message(engine, monkeypatch, capsys):
    import threading
    mod, stack, _ = engine
    monkeypatch.setattr(mod, "PROGRESS_INTERVAL_SECONDS", 0.01, raising=False)
    reported = threading.Event()
    original = stack.report
    def report(message):
        original(message)
        reported.set()
    monkeypatch.setattr(stack, "report", report)
    def slow(*args, **kwargs):
        assert reported.wait(2), "waiting progress was not emitted while Docker was running"
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", slow)
    stack.run("inspect", "container")
    assert "仍在执行 docker inspect" in capsys.readouterr().err


def test_readiness_reports_container_state_before_sleep(engine, monkeypatch, capsys):
    mod, stack, state = engine
    state.target_exists = True
    state.containers["new-target"] = {"Name": "/tempo", "State": {"Running": False, "Status": "starting"}}
    def stop_waiting(seconds):
        assert "waiting" in capsys.readouterr().err
        raise mod.StackError("test stopped")
    monkeypatch.setattr(mod.time, "sleep", stop_waiting)
    with pytest.raises(mod.StackError, match="test stopped"):
        stack.wait_ready()


def test_live_command_failure_keeps_exit_status_without_captured_stderr(engine, monkeypatch):
    mod, stack, _ = engine
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(17, args[0], stderr=None)
    monkeypatch.setattr(mod.subprocess, "run", fail)
    with pytest.raises(mod.StackError, match="exit 17"):
        stack.compose("up", "-d", live=True)


def test_compose_diagnostic_stdout_remains_machine_readable(engine, monkeypatch, capsys):
    mod, stack, state = engine
    state.config = {"sensitive_example": "must not be echoed as progress"}
    monkeypatch.setattr(mod, "__file__", str(stack.directory / "manage-stack.py"))
    monkeypatch.setattr(mod.sys, "argv", ["manage-stack.py", "compose", "config", "--format", "json"])
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == state.config
    assert captured.err == ""


def test_preflight_failure_is_read_only(engine):
    mod, stack, state = engine
    with pytest.raises(mod.StackError):
        stack.deploy()
    assert not state.network and not state.volumes
    assert all("create" not in c and "up" not in c for c, _ in state.calls)


def test_mapped_volume_missing_is_not_recreated(engine):
    mod, stack, state = engine
    state.config = configuration(mod, stack)
    stack.save_mapping({key: "old_" + key for key in mod.VOLUMES})
    with pytest.raises(mod.StackError, match="missing"):
        stack.deploy()
    assert not state.network and not state.volumes


def test_stop_preserves_data_networks_and_business(engine):
    _, stack, state = engine
    stack.stop()
    command = state.calls[-1][0]
    assert command[-3:] == ["stop", "--timeout", "30"]
    assert not any(v in command for v in ("down", "prune", "-v", "n-agent"))


def legacy(mod, state):
    for service in mod.SERVICES:
        mounts = []
        for key, (owner, target, _) in mod.VOLUMES.items():
            if owner == service:
                name = "legacy_" + key
                state.volumes.add(name)
                mounts.append({"Type": "volume", "Name": name, "Destination": target})
        state.containers[service] = {"Id": service, "Name": service, "Mounts": mounts,
            "Config": {"Labels": {"com.docker.compose.project": "n-agent", "com.docker.compose.service": service}},
            "State": {"Running": True}, "HostConfig": {}, "NetworkSettings": {"Ports": {}}}


def test_migration_refuses_missing_mount_before_stopping(engine, tmp_path):
    mod, stack, state = engine
    legacy(mod, state)
    state.containers["pyroscope"]["Mounts"].pop()
    with pytest.raises(mod.StackError):
        stack.migrate("n-agent", tmp_path / "backups", tmp_path / "legacy.yml")
    assert not any("stop" in c or "create" in c for c, _ in state.calls)


@pytest.mark.parametrize("failure", ["up", "ready"])
def test_migration_failure_stops_new_writers_before_restarting_old(engine, tmp_path, monkeypatch, failure):
    mod, stack, state = engine
    legacy(mod, state)
    state.config = configuration(mod, stack)
    for key in mod.VOLUMES:
        state.config["volumes"][key]["name"] = "legacy_" + key
    old_compose = tmp_path / "legacy.yml"
    old_compose.write_text("services: {}\n")
    if failure == "up":
        state.fail = lambda c: "up" in c
    else:
        def fail_ready():
            raise mod.StackError("not ready")
        monkeypatch.setattr(stack, "wait_ready", fail_ready)
    # No unrelated active writer; conflict query is tested separately.
    monkeypatch.setattr(stack, "check_other_writers", lambda *args: None)
    with pytest.raises(mod.StackError):
        stack.migrate("n-agent", tmp_path / "backups", old_compose)
    commands = [c for c, _ in state.calls]
    old_start = next(i for i, c in enumerate(commands) if c[1] == "start")
    new_stop = next(i for i, c in enumerate(commands) if "--project-name" in c and "stop" in c)
    assert new_stop < old_start
    assert not stack.mapping_file.exists()
    assert not any(c[1] == "rm" for c in commands)


def test_migration_does_not_restart_old_if_new_stop_fails(engine, tmp_path, monkeypatch):
    mod, stack, state = engine
    legacy(mod, state)
    state.config = configuration(mod, stack)
    for key in mod.VOLUMES:
        state.config["volumes"][key]["name"] = "legacy_" + key
    old = tmp_path / "legacy.yml"
    old.write_text("services: {}\n")
    monkeypatch.setattr(stack, "check_other_writers", lambda *args: None)
    state.fail = lambda c: "up" in c or ("--project-name" in c and "stop" in c)
    with pytest.raises(mod.StackError, match="manual recovery"):
        stack.migrate("n-agent", tmp_path / "backups", old)
    assert not any(c[1] == "start" for c, _ in state.calls)


def test_partial_legacy_stop_failure_restores_already_stopped_services(engine, tmp_path):
    mod, stack, state = engine
    legacy(mod, state)
    state.config = configuration(mod, stack)
    for key in mod.VOLUMES:
        state.config["volumes"][key]["name"] = "legacy_" + key
    old = tmp_path / "legacy.yml"
    old.write_text("services: {}\n")
    state.fail = lambda c: c[1] == "stop" and c[-1] == "tempo"
    with pytest.raises(mod.StackError, match="legacy services restored"):
        stack.migrate("n-agent", tmp_path / "backups", old)
    assert all(item["State"]["Running"] for item in state.containers.values())
    assert not stack.mapping_file.exists()


def test_successful_migration_preserves_all_eight_volumes_and_recovery_override(engine, tmp_path, monkeypatch):
    mod, stack, state = engine
    legacy(mod, state)
    state.config = configuration(mod, stack)
    for key in mod.VOLUMES:
        state.config["volumes"][key]["name"] = "legacy_" + key
    old = tmp_path / "legacy.yml"
    old.write_text("services: {}\n")
    monkeypatch.setattr(stack, "wait_ready", lambda: None)
    stack.migrate("n-agent", tmp_path / "backups", old)
    assert stack.mapping() == {key: "legacy_" + key for key in mod.VOLUMES}
    snapshot, = (tmp_path / "backups").iterdir()
    override = json.loads((snapshot / "recovery-volumes.json").read_text())
    for key, (owner, target, _) in mod.VOLUMES.items():
        assert override["volumes"][key] == {"external": True, "name": "legacy_" + key}
        assert {"type": "volume", "source": key, "target": target} in override["services"][owner]["volumes"]
    assert "recovery-volumes.json" in (snapshot / "recovery.txt").read_text()
    assert len(state.volumes) == 8
    assert any(c[1] == "rm" and "-v" not in c for c, _ in state.calls)


def test_foreign_volume_writer_blocks_migration_without_mutation(engine):
    mod, stack, state = engine
    legacy(mod, state)
    with pytest.raises(mod.StackError, match="outside migration"):
        stack.check_other_writers(["legacy_tempo-data"], [])
    assert not any(c[1] in ("stop", "start", "rm", "create") for c, _ in state.calls)


def test_ready_failure_can_be_retried_through_migrate(engine, tmp_path, monkeypatch):
    mod, stack, state = engine
    legacy(mod, state)
    state.config = configuration(mod, stack)
    for key in mod.VOLUMES:
        state.config["volumes"][key]["name"] = "legacy_" + key
    old = tmp_path / "legacy.yml"
    old.write_text("services: {}\n")
    def not_ready():
        raise mod.StackError("not ready")
    monkeypatch.setattr(stack, "wait_ready", not_ready)
    with pytest.raises(mod.StackError):
        stack.migrate("n-agent", tmp_path / "backups", old)
    assert not state.target_exists
    monkeypatch.setattr(stack, "wait_ready", lambda: None)
    stack.migrate("n-agent", tmp_path / "backups", old)
    assert state.target_exists and len(stack.mapping()) == 8


def test_compose_lifecycle_commands_cannot_bypass_lifecycle_lock(engine, monkeypatch):
    mod, stack, state = engine
    monkeypatch.setattr(mod, "__file__", str(stack.directory / "manage-stack.py"))
    monkeypatch.setattr(mod.sys, "argv", ["manage-stack.py", "compose", "stop"])
    with (stack.directory / ".lifecycle.lock").open("a") as lock:
        mod.fcntl.flock(lock, mod.fcntl.LOCK_EX | mod.fcntl.LOCK_NB)
        assert mod.main() == 1
    assert not state.calls


def test_compose_cannot_override_project_or_file(engine, monkeypatch):
    mod, _, state = engine
    monkeypatch.setattr(mod.sys, "argv", ["manage-stack.py", "compose", "--project-name", "n-agent", "down"])
    with pytest.raises(SystemExit) as result:
        mod.main()
    assert result.value.code == 2 and not state.calls
