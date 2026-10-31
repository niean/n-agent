"""Exercise the E2E environment lifecycle with Docker/restart stubs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sandbox(tmp_path):
    repo = tmp_path / "repo with spaces"
    scripts = repo / "tests/e2e"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "tests/e2e/environment.sh", scripts)
    docker_dir = repo / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")
    (docker_dir / "restart.sh").write_text('echo "restart" >> "$TEST_LOG"\n')
    bins = tmp_path / "bin"
    bins.mkdir()
    docker = bins / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["compose", "config", "--environment"]:
    print("COMPOSE_FILE=" + os.environ["COMPOSE_FILE"] if os.environ.get("COMPOSE_FILE") else "OTHER=value")
    sys.exit(0)
files = os.environ.get("COMPOSE_FILE", "").split(":")
overlay = pathlib.Path(files[-1])
entry = {"args": args, "files": files}
if overlay.suffix == ".json":
    entry["overlay"] = json.loads(overlay.read_text())
with open(os.environ["TEST_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
sys.exit(int(os.environ.get("RESTORE_RC", "0")) if args[:2] == ["compose", "up"] else 0)
''')
    docker.chmod(0o755)
    log = tmp_path / "log"
    env = {**os.environ, "PATH": f"{bins}:{os.environ['PATH']}", "TEST_LOG": str(log)}
    env.pop("COMPOSE_FILE", None)
    env.pop("N_AGENT_E2E_ENVIRONMENT", None)
    return scripts, log, env


def run_case(sandbox, mode="default", rc=0, restore_rc=0, compose_file=None):
    scripts, log, env = sandbox
    script = scripts / "case.sh"
    script.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/environment.sh"
e2e_environment {mode} "$0" "$@"
# A nested suite must reuse the environment without another restart/restore.
e2e_environment {mode} "$0" "$@"
docker record-test
exit {rc}
''')
    env["RESTORE_RC"] = str(restore_rc)
    if compose_file:
        env["COMPOSE_FILE"] = compose_file
    result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True)
    entries = log.read_text().splitlines()
    records = [json.loads(line) for line in entries if line.startswith("{")]
    return result, entries, records


def test_default_disables_evolution_and_restores_once(sandbox):
    result, entries, records = run_case(sandbox)
    assert result.returncode == 0, result.stderr
    assert entries.count("restart") == 1
    test, restore = records
    assert test["overlay"]["services"]["n-agent"]["environment"] == {
        "N_AGENT_SKILLS_BACKGROUND_REVIEW_ENABLED": "false",
        "N_AGENT_SKILLS_CURATOR_ENABLED": "false",
    }
    assert restore["args"] == ["compose", "up", "-d", "--force-recreate", "--no-deps", "--wait", "n-agent"]
    assert restore["files"] == test["files"][:-1]
    assert not Path(test["files"][-1]).exists()


def test_dedicated_case_can_enable_review(sandbox):
    result, _, records = run_case(sandbox, mode="skill-evolution")
    assert result.returncode == 0, result.stderr
    assert records[0]["overlay"]["services"]["n-agent"]["environment"]["N_AGENT_SKILLS_BACKGROUND_REVIEW_ENABLED"] == "true"


@pytest.mark.parametrize("rc,restore_rc,expected", [(7, 0, 7), (0, 9, 1), (7, 9, 7)])
def test_failure_status_and_restore_failure(sandbox, rc, restore_rc, expected):
    result, _, records = run_case(sandbox, rc=rc, restore_rc=restore_rc)
    assert result.returncode == expected
    assert len(records) == 2
    if restore_rc:
        assert "failed to restore" in result.stderr


def test_existing_compose_selection_survives(sandbox):
    result, _, records = run_case(sandbox, compose_file="custom.yml:telemetry.yml")
    assert result.returncode == 0, result.stderr
    assert [Path(p).name for p in records[1]["files"]] == ["custom.yml", "telemetry.yml"]


@pytest.mark.parametrize("name", ["run.sh", "artifacts.sh", "conversations.sh", "otel_smoke.sh"])
def test_every_entry_uses_default_isolation(name):
    source = (ROOT / "tests/e2e" / name).read_text()
    assert 'e2e_environment default "$0"' in source


def test_invalid_suite_does_not_touch_docker(sandbox):
    scripts, log, env = sandbox
    shutil.copy2(ROOT / "tests/e2e/run.sh", scripts)
    result = subprocess.run(["bash", str(scripts / "run.sh"), "invalid-suite"], env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert not log.exists()


def test_migration_entry_reuses_docker_isolation():
    source = (ROOT / "tests/migrate/config-bundle.sh").read_text()
    assert '/../e2e/environment.sh"' in source
    assert 'e2e_environment default "$0"' in source
