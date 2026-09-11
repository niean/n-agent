"""Host-side tests for the config bundle shell scripts (plan T6).

Everything here runs on the host with plain pytest: no docker, no N-Agent
service, no container. Only the branches that do not need docker are
exercised -- ``--preflight-only`` for argument/output-location validation and
``N_AGENT_SOURCE_ONLY=1`` dot-sourcing for the pure functions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docker" / "config-export.sh"
HELPER = ROOT / "docker" / "config_bundle_files.py"

CANARY = "sk-canary-must-never-be-printed"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         stdin: str | None = None) -> subprocess.CompletedProcess:
    process_env = dict(os.environ)
    # A caller-inherited deployment context must never leak into the script.
    for leaked in ("N_AGENT_INSTALL_ROOT", "N_AGENT_CODE_ROOT", "COMPOSE_FILE",
                   "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES"):
        process_env.pop(leaked, None)
    if env:
        process_env.update(env)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(cwd or ROOT),
        env=process_env,
        input=stdin,
    )


def _export(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
            script: Path | None = None) -> subprocess.CompletedProcess:
    return _run(["sh", str(script or SCRIPT), *args], cwd=cwd, env=env)


def _source_only(snippet: str, *, stdin: str | None = None,
                 cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Dot-source the script to reach a single function.

    ``N_AGENT_SOURCE_ONLY=1`` rather than a ``--source-only`` argument: passing
    positional parameters to ``.`` is unspecified in POSIX and dash errors out.
    """
    return _run(
        ["sh", "-c", f". ./docker/config-export.sh; {snippet}"],
        cwd=cwd or ROOT,
        env={"N_AGENT_SOURCE_ONLY": "1"},
        stdin=stdin,
    )


def _strip_secret_env(text: str) -> str:
    result = _source_only("_strip_secret_env", stdin=text)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _stub_docker(bin_dir: Path, log: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _fake_repo(tmp_path: Path, *, install_root_keys: bool = True,
               extra_env: str = "", install_root: Path | None = None,
               with_install_script: bool = True,
               docker_env: dict[str, str] | None = None,
               with_policy_template: bool = True,
               name: str = "repo") -> tuple[Path, Path]:
    """A throwaway checkout carrying only what the bundle scripts read.

    Returns ``(repo, install_root)``. Nothing here is inside a git worktree, so
    the default output directory ``<repo>/locals/install`` passes the location
    check without depending on this repository's .gitignore.

    ``docker_env`` replaces the generated deployment keys with an explicit
    mapping (T7 needs a target whose configured roots differ from the default).
    """
    repo = tmp_path / name
    (repo / "docker").mkdir(parents=True)
    for script in ("config-export.sh", "config-import.sh", "config_bundle_files.py"):
        source = ROOT / "docker" / script
        if source.exists():
            (repo / "docker" / script).write_bytes(source.read_bytes())
    if with_policy_template:
        template = ROOT / "docker" / "host-terminal-policy.yaml.example"
        (repo / "docker" / "host-terminal-policy.yaml.example").write_bytes(
            template.read_bytes() if template.exists() else b"host_root: /tmp\nrules: []\n"
        )
    if with_install_script:
        (repo / "docker" / "install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "docker" / "docker-compose.yml").write_text(
        "name: n-agent\nservices:\n  n-agent:\n    image: x\n", encoding="utf-8"
    )

    root = install_root if install_root is not None else tmp_path / "install-root"
    lines = [f"N_AGENT_OPENAI_API_KEY={CANARY}", "N_AGENT_LOG_LEVEL=INFO"]
    if docker_env is not None:
        lines = [f"N_AGENT_OPENAI_API_KEY={CANARY}", "N_AGENT_LOG_LEVEL=INFO"]
        lines += [f"{key}={value}" for key, value in docker_env.items()]
    elif install_root_keys:
        lines.append(f"N_AGENT_INSTALL_ROOT={root}")
        lines.append(f"N_AGENT_CODE_ROOT={tmp_path / 'code'}")
    if extra_env:
        lines.append(extra_env)
    (repo / "docker" / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repo, root


def _populate_install_root(root: Path, *, policy: bool = True, tokens: bool = True,
                           oss: bool = True, env_file: bool = True) -> None:
    (root / "locals").mkdir(parents=True, exist_ok=True)
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "skills").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "plugins").mkdir(parents=True, exist_ok=True)
    if env_file:
        (root / ".env").write_text("N_AGENT_HOST_TERMINAL_TOKEN=abc\n", encoding="utf-8")
    if policy:
        (root / "locals" / "host-terminal-policy.yaml").write_text("rules: []\n", encoding="utf-8")
    if tokens:
        (root / "locals" / "host-terminal.token").write_text("t1\n", encoding="utf-8")
        (root / "locals" / "host-browser.token").write_text("t2\n", encoding="utf-8")
    if oss:
        (root / "secrets" / "oss.env").write_text(f"OSS_ACCESS_KEY={CANARY}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# S1: script shape
# ---------------------------------------------------------------------------


def test_export_script_exists_and_is_posix_sh():
    assert SCRIPT.exists()
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text
    assert "umask 077" in text


def test_export_script_never_sources_or_evals_bundle_env():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^\s*eval\b", text, re.MULTILINE) is None
    assert re.search(r"^\s*\.\s+\"?\$\{?env", text, re.MULTILINE) is None
    assert re.search(r"^\s*source\b", text, re.MULTILINE) is None


def test_export_helper_never_imports_app():
    text = HELPER.read_text(encoding="utf-8")
    assert re.search(r"^\s*(import|from)\s+app\b", text, re.MULTILINE) is None


def test_export_compose_calls_pin_project_directory_env_file_and_compose_file():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--project-directory" in text
    assert "--env-file" in text
    assert 'docker-compose.yml' in text


# ---------------------------------------------------------------------------
# S3: output location validation
# ---------------------------------------------------------------------------


def test_export_rejects_output_dir_inside_repo_that_is_not_git_ignored():
    result = _export("--out", str(ROOT / ".harness"))
    assert result.returncode == 5
    assert "git" in (result.stderr + result.stdout).lower()


def test_export_rejects_nonexistent_unignored_repo_subdirectory():
    result = _export("--out", str(ROOT / ".harness" / "does-not-exist"))
    assert result.returncode == 5
    assert not (ROOT / ".harness" / "does-not-exist").exists()


def test_export_rejects_output_dir_in_another_git_worktree(tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
    result = _export("--preflight-only", "--out", str(other / "out"))
    assert result.returncode == 5
    assert not (other / "out").exists()


def test_export_rejects_symlink_escaping_to_unignored_path(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(ROOT / ".harness")
    result = _export("--preflight-only", "--out", str(link / "out"))
    assert result.returncode == 5


def test_export_accepts_git_ignored_output_dir_in_preflight():
    result = _export("--preflight-only", "--out", str(ROOT / "locals" / "install"))
    assert result.returncode == 0, result.stderr


def test_export_accepts_default_output_dir_in_preflight():
    result = _export("--preflight-only")
    assert result.returncode == 0, result.stderr
    assert "locals/install" in (result.stdout + result.stderr)


def test_export_accepts_output_dir_outside_any_worktree(tmp_path):
    result = _export("--preflight-only", "--out", str(tmp_path / "elsewhere"))
    assert result.returncode == 0, result.stderr


def test_preflight_creates_nothing(tmp_path):
    target = tmp_path / "elsewhere"
    result = _export("--preflight-only", "--out", str(target))
    assert result.returncode == 0
    assert not target.exists()


def test_export_rejects_unknown_option():
    result = _export("--preflight-only", "--nope")
    assert result.returncode == 2


def test_export_rejects_out_without_value():
    result = _export("--preflight-only", "--out")
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# S4: deployment roots and required source files
# ---------------------------------------------------------------------------


def test_export_fails_when_deployment_roots_missing_from_env(tmp_path):
    repo, _ = _fake_repo(tmp_path, install_root_keys=False)
    log = tmp_path / "docker.log"
    _stub_docker(tmp_path / "bin", log)
    result = _export(script=repo / "docker" / "config-export.sh", cwd=repo,
                     env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    assert result.returncode == 2
    assert "N_AGENT_INSTALL_ROOT" in result.stderr
    assert not log.exists()


def test_export_fails_when_only_code_root_is_missing(tmp_path):
    repo = tmp_path / "repo"
    repo_dir, root = _fake_repo(tmp_path, install_root_keys=False)
    env_path = repo_dir / "docker" / ".env"
    env_path.write_text(
        env_path.read_text(encoding="utf-8") + f"N_AGENT_INSTALL_ROOT={root}\n",
        encoding="utf-8",
    )
    result = _export(script=repo_dir / "docker" / "config-export.sh", cwd=repo_dir)
    assert result.returncode == 2
    assert "N_AGENT_CODE_ROOT" in result.stderr


def test_export_fails_before_docker_when_a_required_source_file_is_missing(tmp_path):
    repo, root = _fake_repo(tmp_path)
    _populate_install_root(root, policy=False)
    log = tmp_path / "docker.log"
    _stub_docker(tmp_path / "bin", log)
    result = _export(script=repo / "docker" / "config-export.sh", cwd=repo,
                     env={"PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    assert result.returncode == 3
    assert "host-terminal-policy.yaml" in result.stderr
    assert not log.exists(), "docker must not be called before the source files are complete"


def test_export_fails_when_install_script_is_absent(tmp_path):
    repo, root = _fake_repo(tmp_path, with_install_script=False)
    _populate_install_root(root)
    result = _export(script=repo / "docker" / "config-export.sh", cwd=repo)
    assert result.returncode == 3
    assert "install.sh" in result.stderr


def test_export_never_prints_secret_values_in_diagnostics(tmp_path):
    repo, root = _fake_repo(tmp_path)
    _populate_install_root(root, policy=False)
    result = _export(script=repo / "docker" / "config-export.sh", cwd=repo)
    assert result.returncode == 3
    assert CANARY not in result.stderr
    assert CANARY not in result.stdout


def test_export_publishes_nothing_when_collection_fails(tmp_path):
    repo, root = _fake_repo(tmp_path)
    _populate_install_root(root, policy=False)
    result = _export(script=repo / "docker" / "config-export.sh", cwd=repo)
    assert result.returncode == 3
    out_dir = repo / "locals" / "install"
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# S4: --no-secrets env key stripping rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,expected_stripped", [
    ("N_AGENT_OPENAI_API_KEY", True),
    ("N_AGENT_FEISHU_APP_SECRET", True),
    ("N_AGENT_DB_PASSWORD", True),
    ("N_AGENT_OSS_ACCESS_KEY", True),
    ("N_AGENT_SIGNING_PRIVATE_KEY", True),
    ("N_AGENT_HOST_TERMINAL_TOKEN", True),
    ("N_AGENT_HOST_TERMINAL_TOKEN_PATH", False),          # container path, not a credential
    ("N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH", False),
    ("N_AGENT_LOG_LEVEL", False),
    ("N_AGENT_WORKSPACE_ROOT", False),
])
def test_no_secrets_env_key_stripping_rule(key, expected_stripped):
    stripped = _strip_secret_env(f"{key}={CANARY}\n")
    assignments = dict(
        line.split("=", 1) for line in stripped.splitlines() if line and not line.startswith("#")
    )
    assert key in assignments, "no key may be dropped, only its value cleared"
    if expected_stripped:
        assert assignments[key] == ""
        assert CANARY not in stripped
    else:
        assert assignments[key] == CANARY


def test_strip_secret_env_keeps_comments_and_blank_lines():
    text = "# header\n\nN_AGENT_LOG_LEVEL=INFO\n"
    assert _strip_secret_env(text) == text


@pytest.mark.parametrize("value", [
    "plain",
    "with spaces",
    'has"double"quotes',
    "has'single'quotes",
    "has=equals=signs",
    "has$dollar${BRACES}",
    "has#hash",
    "has\\backslash",
    "trailing space ",
    "",
])
def test_env_parse_serialize_round_trip(tmp_path, value):
    """dotenv semantics, not sed/grep+cut: quoting and escaping survive."""
    source = tmp_path / "in.env"
    serialized = subprocess.run(
        ["python3", str(HELPER), "serialize-env"],
        input=json.dumps({"N_AGENT_LOG_LEVEL": value}),
        capture_output=True, text=True, check=True,
    ).stdout
    source.write_text(serialized, encoding="utf-8")
    parsed = json.loads(subprocess.run(
        ["python3", str(HELPER), "parse-env", str(source)],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert parsed["N_AGENT_LOG_LEVEL"] == value


@pytest.mark.parametrize("value", [
    "with spaces",
    'has"double"quotes',
    "has=equals=signs",
    "has$dollar",
    "has#hash",
    "has\\backslash",
])
def test_strip_secret_env_round_trips_non_secret_values(tmp_path, value):
    serialized = subprocess.run(
        ["python3", str(HELPER), "serialize-env"],
        input=json.dumps({"N_AGENT_LOG_LEVEL": value, "N_AGENT_OPENAI_API_KEY": CANARY}),
        capture_output=True, text=True, check=True,
    ).stdout
    stripped = _strip_secret_env(serialized)
    target = tmp_path / "out.env"
    target.write_text(stripped, encoding="utf-8")
    parsed = json.loads(subprocess.run(
        ["python3", str(HELPER), "parse-env", str(target)],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert parsed["N_AGENT_LOG_LEVEL"] == value
    assert parsed["N_AGENT_OPENAI_API_KEY"] == ""
    assert CANARY not in stripped


def test_env_parser_does_not_expand_variables(tmp_path):
    source = tmp_path / "in.env"
    source.write_text('A=literal-${HOME}\nB="quoted-$HOME"\n', encoding="utf-8")
    parsed = json.loads(subprocess.run(
        ["python3", str(HELPER), "parse-env", str(source)],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert parsed["A"] == "literal-${HOME}"
    assert parsed["B"] == "quoted-$HOME"


# ---------------------------------------------------------------------------
# S6: manifest generation (host-only, no docker)
# ---------------------------------------------------------------------------


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    (staging / "env").mkdir(parents=True)
    (staging / "db").mkdir(parents=True)
    (staging / "env" / "docker.env").write_text("A=1\n", encoding="utf-8")
    (staging / "db" / "config.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    (staging / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return staging


def _make_manifest(staging: Path, *, redacted: str = "false") -> dict:
    subprocess.run(
        ["python3", str(HELPER), "manifest", str(staging),
         "--hostname", "srcmac", "--install-root", "/i", "--code-root", "/c",
         "--redacted", redacted],
        capture_output=True, text=True, check=True,
    )
    return json.loads((staging / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_lists_every_regular_file_except_itself(tmp_path):
    staging = _staging(tmp_path)
    manifest = _make_manifest(staging)
    assert set(manifest["files"]) == {"env/docker.env", "db/config.json", "install.sh"}
    digest = hashlib.sha256((staging / "db" / "config.json").read_bytes()).hexdigest()
    assert manifest["files"]["db/config.json"] == digest


def test_manifest_carries_schema_version_source_and_timezone_aware_created_at(tmp_path):
    manifest = _make_manifest(_staging(tmp_path), redacted="true")
    assert manifest["schema_version"] == 1
    assert manifest["redacted"] is True
    assert manifest["source"] == {"hostname": "srcmac", "install_root": "/i", "code_root": "/c"}
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2}|Z)$",
                    manifest["created_at"]), manifest["created_at"]


def test_digest_command_detects_added_removed_and_modified_files(tmp_path):
    staging = _staging(tmp_path)
    def digest() -> dict:
        return json.loads(subprocess.run(
            ["python3", str(HELPER), "digest", str(staging)],
            capture_output=True, text=True, check=True,
        ).stdout)

    before = digest()
    (staging / "env" / "docker.env").write_text("A=2\n", encoding="utf-8")
    assert digest() != before
    (staging / "env" / "docker.env").write_text("A=1\n", encoding="utf-8")
    assert digest() == before
    (staging / "env" / "extra").write_text("x", encoding="utf-8")
    assert digest() != before


def test_manifest_json_is_written_0600(tmp_path):
    staging = _staging(tmp_path)
    _make_manifest(staging)
    assert oct((staging / "manifest.json").stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# S4-S7: the whole collection/packaging path, with docker stubbed out
# ---------------------------------------------------------------------------


DB_CANARY = "sk-db-canary-value"

_CONFIG_JSON = json.dumps({
    "schema_version": 1,
    "created_at": "2026-09-10T10:00:00+08:00",
    "source": {"hostname": "src", "install_root": "/i", "code_root": "/c"},
    "redacted": False,
    "sections": {
        "providers": [{"name": "p", "api_key": DB_CANARY}],
        "knowledge_bases": [],
        "mcp_sites": [],
        "external_memory_providers": [],
        "external_memory_global_config": None,
        "plugins": [],
        "skills": [],
        "scheduled_tasks": [],
        "gateway_home_targets": [],
        "task_config": None,
    },
})


class _Deployment:
    def __init__(self, repo: Path, root: Path, bin_dir: Path, log: Path):
        self.repo = repo
        self.root = root
        self.bin = bin_dir
        self.log = log
        self.script = repo / "docker" / "config-export.sh"
        self.out_dir = repo / "locals" / "install"

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return _export(*args, script=self.script, cwd=self.repo,
                       env={"PATH": f"{self.bin}:{os.environ['PATH']}"})

    def bundles(self) -> list[Path]:
        if not self.out_dir.exists():
            return []
        return sorted(self.out_dir.glob("n-agent-config-*.tar.gz"))

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


def _deployment(tmp_path: Path, *, mutate_during_exec: Path | None = None) -> _Deployment:
    repo, root = _fake_repo(tmp_path)
    _populate_install_root(root)
    (root / "workspace" / "skills" / "demo").mkdir()
    (root / "workspace" / "skills" / "demo" / "SKILL.md").write_text("x\n", encoding="utf-8")
    (root / "workspace" / "plugins" / "keep.txt").write_text("y\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(_CONFIG_JSON, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    mutation = (
        f'printf "changed\\n" >> "{mutate_during_exec}"\n' if mutate_during_exec else ""
    )
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        "case \"$*\" in\n"
        "  *' ps '*) printf 'n-agent\\n'; exit 0;;\n"
        f"  *exec*) {mutation}cat '{config}'; exit 0;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)
    return _Deployment(repo, root, bin_dir, log)


def _members(bundle: Path) -> list[str]:
    listing = subprocess.run(["tar", "tzf", str(bundle)], capture_output=True, text=True,
                             check=True).stdout.split()
    return [name[2:] for name in listing if not name.endswith("/")]


def _extract(bundle: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "xzf", str(bundle), "-C", str(destination)], check=True,
                   capture_output=True)
    return destination


def test_export_publishes_a_0600_bundle_and_a_0700_installer(tmp_path):
    deployment = _deployment(tmp_path)
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    bundles = deployment.bundles()
    assert len(bundles) == 1
    assert oct(bundles[0].stat().st_mode & 0o777) == "0o600"
    installer = deployment.out_dir / "install.sh"
    assert oct(installer.stat().st_mode & 0o777) == "0o700"
    assert bundles[0].name in result.stdout


def test_bundle_carries_manifest_plus_the_eleven_members(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0
    members = set(_members(deployment.bundles()[0]))
    assert members == {
        "manifest.json", "install.sh",
        "env/docker.env", "env/install-root.env", "env/docker-compose.yml",
        "locals/host-terminal-policy.yaml", "locals/host-terminal.token",
        "locals/host-browser.token", "secrets/oss.env",
        "workspace/skills.tar.gz", "workspace/plugins.tar.gz",
        "db/config.json",
    }


def test_a_freshly_exported_bundle_passes_the_importer_preflight(tmp_path):
    """The end-to-end invariant the two scripts exist to uphold.

    Every other export case inspects the archive through helpers that normalise
    the member list, and every import case is fed a fixture built member by
    member with tarfile. Nothing crossed the seam, so a bundle the exporter
    published and the importer would refuse passed the whole suite twice.
    """
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0, "export must succeed first"
    r = _preflight(tmp_path, deployment.bundles()[0])
    assert r.returncode == 0, r.stderr


def test_export_suppresses_macos_appledouble_members(tmp_path):
    """macOS bsdtar turns a file's xattrs into a sibling `._name` member that
    no manifest lists, so the importer rejects the bundle. Reproduced with a
    real xattr on macOS; elsewhere the packer setting is asserted instead.
    """
    deployment = _deployment(tmp_path)
    if sys.platform == "darwin":
        target = deployment.root / "locals" / "host-terminal.token"
        subprocess.run(["xattr", "-w", "com.apple.metadata:test", "1", str(target)],
                       check=True)
    assert deployment.run().returncode == 0
    with tarfile.open(deployment.bundles()[0], "r:gz") as archive:
        raw = archive.getnames()
    assert [n for n in raw if "._" in n] == []


def test_manifest_digests_match_every_packed_file(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0
    extracted = _extract(deployment.bundles()[0], tmp_path / "unpacked")
    manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    packed = {name for name in _members(deployment.bundles()[0]) if name != "manifest.json"}
    assert set(manifest["files"]) == packed
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((extracted / relative).read_bytes()).hexdigest() == digest
    assert manifest["source"]["install_root"] == str(deployment.root)
    assert manifest["redacted"] is False


def test_export_refuses_to_overwrite_an_existing_bundle_name(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0
    first = deployment.bundles()[0]
    original = first.read_bytes()
    second = deployment.run()
    # Same minute -> same name: the first bundle must survive untouched.
    if second.returncode == 0:
        assert len(deployment.bundles()) == 2
    else:
        assert second.returncode == 3
        assert first.read_bytes() == original


def test_export_pins_the_compose_context(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0
    calls = deployment.calls()
    assert f"--project-directory {deployment.repo}/docker" in calls
    assert f"--env-file {deployment.repo}/docker/.env" in calls
    assert f"-f {deployment.repo}/docker/docker-compose.yml" in calls


def test_default_export_warns_about_plaintext_secrets(tmp_path):
    deployment = _deployment(tmp_path)
    result = deployment.run()
    assert result.returncode == 0
    assert "PLAINTEXT" in result.stderr


def test_export_report_never_prints_secret_values(tmp_path):
    deployment = _deployment(tmp_path)
    result = deployment.run()
    assert result.returncode == 0
    assert CANARY not in result.stdout and CANARY not in result.stderr
    assert DB_CANARY not in result.stdout and DB_CANARY not in result.stderr


def test_no_secrets_export_omits_tokens_and_oss_and_clears_env_values(tmp_path):
    deployment = _deployment(tmp_path)
    result = deployment.run("--no-secrets")
    assert result.returncode == 0, result.stderr
    members = set(_members(deployment.bundles()[0]))
    assert "secrets/oss.env" not in members
    assert "locals/host-terminal.token" not in members
    assert "locals/host-browser.token" not in members

    extracted = _extract(deployment.bundles()[0], tmp_path / "unpacked")
    parsed = json.loads(subprocess.run(
        ["python3", str(HELPER), "parse-env", str(extracted / "env" / "docker.env")],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert parsed["N_AGENT_OPENAI_API_KEY"] == ""
    assert parsed["N_AGENT_LOG_LEVEL"] == "INFO"
    assert CANARY not in (extracted / "env" / "docker.env").read_text(encoding="utf-8")
    manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["redacted"] is True


def test_no_secrets_export_redacts_the_db_section_and_discloses_residual_risk(tmp_path):
    deployment = _deployment(tmp_path)
    result = deployment.run("--no-secrets")
    assert result.returncode == 0
    assert "--redact-secrets" in deployment.calls()
    lowered = result.stderr.lower()
    assert "workspace" in lowered and "url" in lowered
    assert "credential-free" in lowered or "残余" in result.stderr


def test_no_tokens_export_keeps_oss_but_drops_the_two_tokens(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run("--no-tokens").returncode == 0
    members = set(_members(deployment.bundles()[0]))
    assert "secrets/oss.env" in members
    assert "locals/host-terminal.token" not in members
    assert "locals/host-browser.token" not in members


def test_export_aborts_and_publishes_nothing_when_a_source_file_changes(tmp_path):
    policy = tmp_path / "install-root" / "locals" / "host-terminal-policy.yaml"
    deployment = _deployment(tmp_path, mutate_during_exec=policy)
    result = deployment.run()
    assert result.returncode == 3
    assert "changed" in result.stderr
    assert deployment.bundles() == []


def test_export_leaves_no_staging_or_temporary_artifacts(tmp_path):
    deployment = _deployment(tmp_path)
    assert deployment.run().returncode == 0
    leftovers = [p.name for p in deployment.out_dir.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_workspace_archives_exclude_backups_and_archive(tmp_path):
    deployment = _deployment(tmp_path)
    backups = deployment.root / "workspace" / "skills" / ".backups"
    backups.mkdir()
    (backups / "old.md").write_text("old\n", encoding="utf-8")
    assert deployment.run().returncode == 0
    extracted = _extract(deployment.bundles()[0], tmp_path / "unpacked")
    listing = subprocess.run(
        ["tar", "tzf", str(extracted / "workspace" / "skills.tar.gz")],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".backups" not in listing
    assert "skills/demo/SKILL.md" in listing


# ===========================================================================
# T7: docker/config-import.sh
#
# Same rules as above: host-only, plain pytest, no docker daemon, no N-Agent
# service, no E2E. Two seams make that possible --
#   --preflight-only  bundle validation only, creates nothing at all
#   --host-only       stops right after the host files have landed
# and a stub `docker` on PATH for the few cases that assert call ordering.
# ===========================================================================

import io
import tarfile
from datetime import datetime, timedelta, timezone

IMPORT_SCRIPT = ROOT / "docker" / "config-import.sh"

SRC_INSTALL_ROOT = "/Users/src/install/n-agent"
SRC_CODE_ROOT = "/Users/src/code"


class Symlink:
    def __init__(self, target: str):
        self.target = target


class Hardlink:
    def __init__(self, target: str):
        self.target = target


class Device:
    def __init__(self, kind: str = "chr"):
        self.kind = kind


class Fifo:
    pass


def _inner_archive(members: list[tuple[str, bytes]]) -> bytes:
    """A gzip tar built in memory -- workspace/skills.tar.gz & plugins.tar.gz."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for arcname, payload in members:
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _db_config(**overrides) -> bytes:
    document = {
        "schema_version": 1,
        "created_at": "2026-09-10T10:00:00+08:00",
        "source": {"hostname": "srcmac", "install_root": SRC_INSTALL_ROOT,
                   "code_root": SRC_CODE_ROOT},
        "redacted": False,
        "sections": {
            "providers": [{"name": "p", "api_key": DB_CANARY}],
            "knowledge_bases": [],
            "mcp_sites": [],
            "external_memory_providers": [],
            "external_memory_global_config": None,
            "plugins": [],
            "skills": [],
            "scheduled_tasks": [],
            "gateway_home_targets": [],
            "task_config": None,
        },
    }
    document.update(overrides)
    return (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")


# A bundled install.sh that would leave a trace if the importer ever ran it.
def _tripwire_installer(marker: Path) -> bytes:
    return f"#!/bin/sh\nprintf 'executed\\n' > {marker}\n".encode("utf-8")


def _valid_members(*, marker: Path | None = None,
                   docker_env: str | None = None) -> list[tuple[str, object]]:
    docker_env_text = docker_env if docker_env is not None else (
        f"N_AGENT_INSTALL_ROOT={SRC_INSTALL_ROOT}\n"
        f"N_AGENT_CODE_ROOT={SRC_CODE_ROOT}\n"
        f"N_AGENT_OPENAI_API_KEY={CANARY}\n"
        "N_AGENT_FROM_BUNDLE=yes\n"
        "N_AGENT_LOG_LEVEL=DEBUG\n"
    )
    installer = _tripwire_installer(marker) if marker else b"#!/bin/sh\nexit 0\n"
    return [
        ("install.sh", installer),
        ("env/docker.env", docker_env_text.encode("utf-8")),
        ("env/install-root.env", b"N_AGENT_HOST_TERMINAL_TOKEN=abc\n"),
        ("env/docker-compose.yml",
         b"name: n-agent\nservices:\n  n-agent:\n    image: x\n"),
        ("locals/host-terminal-policy.yaml",
         f"host_root: {SRC_INSTALL_ROOT}/workspace\nrules: []\n".encode("utf-8")),
        ("locals/host-terminal.token", b"terminal-token-from-source\n"),
        ("locals/host-browser.token", b"browser-token-from-source\n"),
        ("secrets/oss.env", f"OSS_ACCESS_KEY={CANARY}\n".encode("utf-8")),
        ("workspace/skills.tar.gz",
         _inner_archive([("skills/demo/SKILL.md", b"# demo\n")])),
        ("workspace/plugins.tar.gz",
         _inner_archive([("plugins/demo-plugin/plugin.json", b"{}\n")])),
        ("db/config.json", _db_config()),
    ]


def _default_manifest(members: list[tuple[str, object]]) -> dict:
    files = {
        arcname: hashlib.sha256(payload).hexdigest()
        for arcname, payload in members
        if isinstance(payload, bytes)
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source": {"hostname": "srcmac", "install_root": SRC_INSTALL_ROOT,
                   "code_root": SRC_CODE_ROOT},
        "redacted": False,
        "files": files,
    }


def _make_bundle(tmp_path: Path, members: list[tuple[str, object]], *,
                 manifest: dict | None = None, add_manifest: bool = True,
                 name: str | None = None) -> Path:
    """Build a tar.gz fixture; ``members`` is a list of (arcname, bytes|Special)."""
    target = tmp_path / (name or f"bundle-{abs(hash(tuple(a for a, _ in members))) % 10**8}.tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    document = manifest if manifest is not None else _default_manifest(members)
    with tarfile.open(target, mode="w:gz") as archive:
        if add_manifest:
            payload = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
        for arcname, content in members:
            info = tarfile.TarInfo(arcname)
            info.mode = 0o600
            if isinstance(content, Symlink):
                info.type = tarfile.SYMTYPE
                info.linkname = content.target
                archive.addfile(info)
            elif isinstance(content, Hardlink):
                info.type = tarfile.LNKTYPE
                info.linkname = content.target
                archive.addfile(info)
            elif isinstance(content, Device):
                info.type = tarfile.CHRTYPE if content.kind == "chr" else tarfile.BLKTYPE
                info.devmajor, info.devminor = 1, 3
                archive.addfile(info)
            elif isinstance(content, Fifo):
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    target.chmod(0o600)
    return target


def _make_valid_bundle(tmp_path: Path, *, without: tuple[str, ...] | list[str] = (),
                       marker: Path | None = None, docker_env: str | None = None,
                       redacted: bool = False, name: str | None = None,
                       replace: dict[str, bytes] | None = None) -> Path:
    members = [
        (arcname, content)
        for arcname, content in _valid_members(marker=marker, docker_env=docker_env)
        if arcname not in set(without)
    ]
    if replace:
        members = [(a, replace.get(a, c)) for a, c in members]
    manifest = _default_manifest(members)
    manifest["redacted"] = redacted
    return _make_bundle(tmp_path, members, manifest=manifest,
                        name=name or f"valid-{len(members)}-{int(redacted)}.tar.gz")


def _make_bundle_with_inner(tmp_path: Path, *, inner_arcname: str = "../evil",
                            inner_kind: str = "file") -> Path:
    if inner_kind == "symlink":
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo(inner_arcname)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        inner = buffer.getvalue()
    else:
        inner = _inner_archive([(inner_arcname, b"pwn\n")])
    return _make_valid_bundle(tmp_path, name="inner-evil.tar.gz",
                              replace={"workspace/skills.tar.gz": inner})


def _import_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home)}
    env.update(extra or {})
    return env


def _preflight(tmp_path, bundle, *extra) -> subprocess.CompletedProcess:
    """Every landing path points below tmp_path so cases cannot pollute each other."""
    return _run(["sh", str(IMPORT_SCRIPT), str(bundle), "--preflight-only",
                 "--install-root", str(tmp_path / "never"), *extra],
                cwd=ROOT, env=_import_env(tmp_path))


def _run_import(repo: Path, bundle: Path, *args: str,
                env: dict[str, str] | None = None,
                tmp_path: Path | None = None) -> subprocess.CompletedProcess:
    base = tmp_path or repo.parent
    return _run(["sh", str(repo / "docker" / "config-import.sh"), str(bundle), *args],
                cwd=repo, env=_import_env(base, env))


def _read_env(repo: Path) -> dict[str, str]:
    return json.loads(subprocess.run(
        ["python3", str(HELPER), "parse-env", str(repo / "docker" / ".env")],
        capture_output=True, text=True, check=True,
    ).stdout)


def _snapshot(root: Path) -> dict:
    root = Path(root)
    out: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            out[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            out[relative] = ("dir", oct(info.st_mode & 0o777))
        else:
            out[relative] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                             info.st_mtime_ns, oct(info.st_mode & 0o777))
    return out


def _backups(root: Path) -> list[Path]:
    return sorted(Path(root).glob("**/*.bak-*"))


# ---------------------------------------------------------------------------
# T7 S1: script shape
# ---------------------------------------------------------------------------


def test_import_script_exists_and_is_posix_sh():
    assert IMPORT_SCRIPT.exists()
    text = IMPORT_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text
    assert "umask 077" in text


def test_import_script_never_sources_or_evals_bundle_content():
    text = IMPORT_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^\s*eval\b", text, re.MULTILINE) is None
    assert re.search(r"^\s*source\b", text, re.MULTILINE) is None
    assert re.search(r"^\s*\.\s+\"?\$\{?(staging|extract|bundle)", text,
                     re.MULTILINE | re.IGNORECASE) is None


def test_import_helper_never_imports_app():
    text = HELPER.read_text(encoding="utf-8")
    assert re.search(r"^\s*(import|from)\s+app\b", text, re.MULTILINE) is None


# ---------------------------------------------------------------------------
# T7 S1/S3: bundle preflight -- malicious archives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arcname", ["/etc/passwd", "../../etc/passwd", "env/../../x"])
def test_preflight_rejects_path_traversal_members(tmp_path, arcname):
    r = _preflight(tmp_path, _make_bundle(tmp_path, [(arcname, b"x")]))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_rejects_symlink_member(tmp_path):
    r = _preflight(tmp_path, _make_bundle(tmp_path, [("env/link", Symlink("/etc/passwd"))]))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


@pytest.mark.parametrize("special", [Hardlink("env/docker.env"), Device("chr"),
                                     Device("blk"), Fifo()])
def test_preflight_rejects_hardlink_and_device_members(tmp_path, special):
    bundle = _make_bundle(tmp_path, [("env/docker.env", b"A=1\n"), ("env/other", special)],
                          name=f"special-{type(special).__name__}-{id(special)}.tar.gz")
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_rejects_duplicate_members(tmp_path):
    r = _preflight(tmp_path, _make_bundle(
        tmp_path, [("env/docker.env", b"a"), ("env/docker.env", b"b")]))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_normalized_duplicate_members(tmp_path):
    r = _preflight(tmp_path, _make_bundle(
        tmp_path, [("env/docker.env", b"a"), ("./env/docker.env", b"b")],
        name="normdup.tar.gz"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_member_over_size_limit(tmp_path):
    """The threshold is lowered through the env seam: no 256 MiB file on disk."""
    bundle = _make_valid_bundle(tmp_path)
    r = _run(["sh", str(IMPORT_SCRIPT), str(bundle), "--preflight-only",
              "--install-root", str(tmp_path / "never")],
             cwd=ROOT, env=_import_env(tmp_path, {"N_AGENT_BUNDLE_MAX_FILE_BYTES": "16"}))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_rejects_a_limit_above_the_spec_hard_cap(tmp_path):
    r = _run(["sh", str(IMPORT_SCRIPT), str(_make_valid_bundle(tmp_path)),
              "--preflight-only", "--install-root", str(tmp_path / "never")],
             cwd=ROOT,
             env=_import_env(tmp_path, {"N_AGENT_BUNDLE_MAX_FILE_BYTES": str(1 << 40)}))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_too_many_members(tmp_path):
    bundle = _make_valid_bundle(tmp_path)
    r = _run(["sh", str(IMPORT_SCRIPT), str(bundle), "--preflight-only",
              "--install-root", str(tmp_path / "never")],
             cwd=ROOT, env=_import_env(tmp_path, {"N_AGENT_BUNDLE_MAX_MEMBERS": "3"}))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


def test_member_budget_is_cumulative_across_the_inner_archives(tmp_path):
    """Outer + inner members share one budget; a nested layer must not reset it."""
    outer_only = len(_valid_members()) + 1          # + manifest.json
    inner_count = 2                                  # skills/demo/SKILL.md + plugin.json
    bundle = _make_valid_bundle(tmp_path)
    between = str(outer_only + inner_count - 1)
    r = _run(["sh", str(IMPORT_SCRIPT), str(bundle), "--preflight-only",
              "--install-root", str(tmp_path / "never")],
             cwd=ROOT, env=_import_env(tmp_path, {"N_AGENT_BUNDLE_MAX_MEMBERS": between}))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_manifest_checksum_mismatch(tmp_path):
    members = [("env/docker.env", b"tampered")]
    manifest = _default_manifest(members)
    manifest["files"]["env/docker.env"] = hashlib.sha256(b"the original").hexdigest()
    r = _preflight(tmp_path, _make_bundle(tmp_path, members, manifest=manifest,
                                          name="badsum.tar.gz"))
    assert r.returncode == 2, r.stderr
    assert "sha256" in (r.stdout + r.stderr).lower() or "checksum" in (r.stdout + r.stderr).lower()


def test_preflight_rejects_file_missing_from_manifest(tmp_path):
    members = [("env/docker.env", b"A=1\n"), ("env/extra.env", b"B=2\n")]
    manifest = _default_manifest(members)
    del manifest["files"]["env/extra.env"]
    r = _preflight(tmp_path, _make_bundle(tmp_path, members, manifest=manifest,
                                          name="unlisted.tar.gz"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_manifest_entry_without_a_file(tmp_path):
    members = [("env/docker.env", b"A=1\n")]
    manifest = _default_manifest(members)
    manifest["files"]["env/ghost.env"] = hashlib.sha256(b"").hexdigest()
    r = _preflight(tmp_path, _make_bundle(tmp_path, members, manifest=manifest,
                                          name="ghost.tar.gz"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_a_bundle_without_a_manifest(tmp_path):
    r = _preflight(tmp_path, _make_bundle(tmp_path, [("env/docker.env", b"A=1\n")],
                                          add_manifest=False, name="nomanifest.tar.gz"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_unsupported_schema_version(tmp_path):
    members = _valid_members()
    manifest = _default_manifest(members)
    manifest["schema_version"] = 2
    r = _preflight(tmp_path, _make_bundle(tmp_path, members, manifest=manifest,
                                          name="v2.tar.gz"))
    assert r.returncode == 2, r.stderr
    assert "schema_version" in (r.stdout + r.stderr)


def test_preflight_rejects_created_at_without_a_timezone(tmp_path):
    members = _valid_members()
    manifest = _default_manifest(members)
    manifest["created_at"] = "2026-09-10T10:00:00"
    r = _preflight(tmp_path, _make_bundle(tmp_path, members, manifest=manifest,
                                          name="naive-time.tar.gz"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_db_schema_version_disagreeing_with_the_manifest(tmp_path):
    bundle = _make_valid_bundle(tmp_path, name="db-v2.tar.gz",
                                replace={"db/config.json": _db_config(schema_version=2)})
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_a_db_section_that_is_missing(tmp_path):
    document = json.loads(_db_config())
    del document["sections"]["providers"]
    bundle = _make_valid_bundle(
        tmp_path, name="missing-section.tar.gz",
        replace={"db/config.json": (json.dumps(document) + "\n").encode("utf-8")})
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr
    assert "providers" in (r.stdout + r.stderr)


def test_preflight_rejects_duplicate_natural_keys_inside_a_section(tmp_path):
    document = json.loads(_db_config())
    document["sections"]["providers"] = [{"name": "p"}, {"name": "p"}]
    bundle = _make_valid_bundle(
        tmp_path, name="dup-key.tar.gz",
        replace={"db/config.json": (json.dumps(document) + "\n").encode("utf-8")})
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_more_than_one_active_provider(tmp_path):
    document = json.loads(_db_config())
    document["sections"]["providers"] = [{"name": "a", "is_active": True},
                                         {"name": "b", "is_active": True}]
    bundle = _make_valid_bundle(
        tmp_path, name="two-active.tar.gz",
        replace={"db/config.json": (json.dumps(document) + "\n").encode("utf-8")})
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr


def test_preflight_applies_same_checks_to_inner_skills_archive(tmp_path):
    """A ../ member inside workspace/skills.tar.gz must be rejected as well."""
    r = _preflight(tmp_path, _make_bundle_with_inner(tmp_path, inner_arcname="../evil"))
    assert r.returncode == 2, r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_rejects_symlinks_inside_the_inner_archive(tmp_path):
    r = _preflight(tmp_path, _make_bundle_with_inner(tmp_path, inner_arcname="skills/link",
                                                     inner_kind="symlink"))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_absolute_member_inside_the_inner_archive(tmp_path):
    r = _preflight(tmp_path, _make_bundle_with_inner(tmp_path, inner_arcname="/etc/shadow"))
    assert r.returncode == 2, r.stderr


def test_preflight_accepts_well_formed_bundle_without_writing_anything(tmp_path):
    r = _preflight(tmp_path, _make_valid_bundle(tmp_path))
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "never").exists()


def _repack_like_the_exporter(bundle: Path, out: Path) -> Path:
    """Rebuild `bundle` the way docker/config-export.sh actually packs: unpack
    into a staging directory and run `tar -czf <out> -C <staging> .`.

    Every other fixture here adds members one at a time via tarfile.addfile,
    which never produces the `./` root entry that `tar -C <dir> .` emits. That
    gap let a bundle that no importer would accept pass the whole suite.
    """
    staging = out.parent / f"{out.stem}-staging"
    staging.mkdir(parents=True)
    subprocess.run(["tar", "-xzf", str(bundle), "-C", str(staging)], check=True)
    subprocess.run(["tar", "-czf", str(out), "-C", str(staging), "."], check=True)
    return out


def test_preflight_accepts_a_bundle_packed_the_way_the_exporter_packs_it(tmp_path):
    """`tar -C <staging> .` emits a `./` root member; it is the archive root,
    not an unnamed file, so it must not be rejected as an empty member name.
    """
    repacked = _repack_like_the_exporter(
        _make_valid_bundle(tmp_path), tmp_path / "exporter-style.tar.gz")
    assert "./" in subprocess.run(["tar", "-tzf", str(repacked)],
                                  capture_output=True, text=True).stdout.split("\n")
    r = _preflight(tmp_path, repacked)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_still_rejects_a_regular_file_member_with_no_name(tmp_path):
    """The archive-root allowance must not soften the empty-name rule for files."""
    r = _preflight(tmp_path, _make_bundle(tmp_path, [(".", b"payload")]))
    assert r.returncode == 2
    assert "empty member name" in r.stderr
    assert not (tmp_path / "never").exists()


def test_preflight_does_not_unpack_or_create_temporary_directories(tmp_path):
    bundle = _make_valid_bundle(tmp_path)
    _import_env(tmp_path)                       # the fake HOME is fixture setup
    before = sorted(p.name for p in tmp_path.iterdir())
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_preflight_rejects_a_missing_or_unreadable_bundle(tmp_path):
    r = _preflight(tmp_path, tmp_path / "does-not-exist.tar.gz")
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_unknown_option(tmp_path):
    r = _preflight(tmp_path, _make_valid_bundle(tmp_path), "--nope")
    assert r.returncode == 2


def test_preflight_rejects_an_unknown_mode(tmp_path):
    r = _preflight(tmp_path, _make_valid_bundle(tmp_path), "--mode", "clobber")
    assert r.returncode == 2


def test_preflight_rejects_contradicting_schedule_switches(tmp_path):
    r = _preflight(tmp_path, _make_valid_bundle(tmp_path),
                   "--schedules-enabled", "--schedules-disabled")
    assert r.returncode == 2


def test_preflight_rejects_a_relative_install_root(tmp_path):
    r = _run(["sh", str(IMPORT_SCRIPT), str(_make_valid_bundle(tmp_path)),
              "--preflight-only", "--install-root", "relative/path"],
             cwd=ROOT, env=_import_env(tmp_path))
    assert r.returncode == 2, r.stderr


def test_preflight_rejects_a_code_root_nested_inside_the_workspace(tmp_path):
    root = tmp_path / "inst"
    r = _run(["sh", str(IMPORT_SCRIPT), str(_make_valid_bundle(tmp_path)),
              "--preflight-only", "--install-root", str(root),
              "--code-root", str(root / "workspace" / "code")],
             cwd=ROOT, env=_import_env(tmp_path))
    assert r.returncode == 2, r.stderr


def test_preflight_never_prints_secret_values(tmp_path):
    r = _preflight(tmp_path, _make_valid_bundle(tmp_path))
    assert r.returncode == 0, r.stderr
    assert CANARY not in (r.stdout + r.stderr)
    assert DB_CANARY not in (r.stdout + r.stderr)


def test_compose_value_with_newline_is_rejected(tmp_path):
    """spec line 171: a Compose value that cannot be represented is refused."""
    bundle = _make_valid_bundle(
        tmp_path, name="newline.tar.gz",
        docker_env=(f"N_AGENT_INSTALL_ROOT={SRC_INSTALL_ROOT}\n"
                    f"N_AGENT_CODE_ROOT={SRC_CODE_ROOT}\n"
                    'N_AGENT_BROKEN="line-one\\nline-two"\n'))
    r = _preflight(tmp_path, bundle)
    assert r.returncode == 2, r.stderr
    assert "N_AGENT_BROKEN" in (r.stdout + r.stderr)


# ---------------------------------------------------------------------------
# T7 S1/S4-S7: host landing (fake install-root + fake repo, no docker)
# ---------------------------------------------------------------------------


def test_refuses_to_silently_relocate_configured_install_root(tmp_path):
    """spec line 170: a non-empty target whose configured root differs from the
    default, without an explicit flag -> exit 2."""
    repo, _ = _fake_repo(tmp_path, docker_env={"N_AGENT_INSTALL_ROOT": "/existing/install",
                                               "N_AGENT_CODE_ROOT": "/existing/code"})
    r = _run_import(repo, _make_valid_bundle(tmp_path), "--host-only", tmp_path=tmp_path)
    assert r.returncode == 2, r.stderr
    assert "install-root" in (r.stdout + r.stderr)
    assert _read_env(repo)["N_AGENT_INSTALL_ROOT"] == "/existing/install"
    assert not (tmp_path / "home" / "install").exists()


def test_explicit_install_root_overrides_configured_value(tmp_path):
    repo, _ = _fake_repo(tmp_path, docker_env={"N_AGENT_INSTALL_ROOT": "/existing/install"})
    r = _run_import(repo, _make_valid_bundle(tmp_path), "--host-only",
                    "--install-root", str(tmp_path / "new"), tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout


def _target(tmp_path: Path, *, name: str = "repo", **kwargs) -> tuple[Path, Path, Path]:
    """(repo, install_root, code_root) with both roots passed explicitly."""
    repo, _ = _fake_repo(tmp_path, docker_env={}, name=name, **kwargs)
    install_root = tmp_path / f"{name}-install"
    code_root = tmp_path / f"{name}-code"
    return repo, install_root, code_root


def _land(repo: Path, bundle: Path, install_root: Path, code_root: Path,
          *args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    return _run_import(repo, bundle, "--host-only",
                       "--install-root", str(install_root),
                       "--code-root", str(code_root), *args, tmp_path=tmp_path)


def test_host_only_lands_the_files_and_creates_the_skeleton(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    for relative in ("locals", "logs", "workspace/skills", "workspace/plugins",
                     "browser-profiles", "secrets"):
        assert (install_root / relative).is_dir(), relative
    assert (install_root / ".env").is_file()
    assert (repo / "docker" / "docker-compose.yml").read_text(encoding="utf-8") == \
        "name: n-agent\nservices:\n  n-agent:\n    image: x\n"


def test_merge_only_fills_missing_env_keys(tmp_path):
    """AC18: merge keeps the value already on the target and only adds what is missing."""
    repo, install_root, code_root = _target(tmp_path)
    (repo / "docker" / ".env").write_text(
        "N_AGENT_LOG_LEVEL=INFO\nN_AGENT_LOCAL_ONLY=keep-me\n", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    values = _read_env(repo)
    assert values["N_AGENT_LOG_LEVEL"] == "INFO"        # bundle says DEBUG, target wins
    assert values["N_AGENT_LOCAL_ONLY"] == "keep-me"    # not in the bundle, not removed
    assert values["N_AGENT_FROM_BUNDLE"] == "yes"       # missing key gets filled


def test_overwrite_updates_carried_keys_and_keeps_the_rest(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (repo / "docker" / ".env").write_text(
        "N_AGENT_LOG_LEVEL=INFO\nN_AGENT_LOCAL_ONLY=keep-me\n", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
              "--mode", "overwrite", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    values = _read_env(repo)
    assert values["N_AGENT_LOG_LEVEL"] == "DEBUG"
    assert values["N_AGENT_LOCAL_ONLY"] == "keep-me"


def test_overwrite_backs_up_before_changing_env(tmp_path):
    """AC18: overwrite leaves a .bak-* copy (0600) before it changes anything."""
    repo, install_root, code_root = _target(tmp_path)
    (repo / "docker" / ".env").write_text("N_AGENT_LOG_LEVEL=INFO\n", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
              "--mode", "overwrite", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    backups = _backups(install_root)
    assert backups, "an overwrite that changes a file must leave a backup"
    for backup in backups:
        assert oct(backup.stat().st_mode & 0o777) == "0o600"
        assert CANARY not in backup.name and "api_key" not in backup.name.lower()
    saved = [b for b in backups if b.read_text(encoding="utf-8") == "N_AGENT_LOG_LEVEL=INFO\n"]
    assert saved, [b.name for b in backups]


def test_backups_live_outside_the_scanned_workspace(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (repo / "docker" / ".env").write_text("N_AGENT_LOG_LEVEL=INFO\n", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
              "--mode", "overwrite", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    backups = _backups(install_root)
    assert backups, "the overwritten .env must have produced a backup"
    for backup in backups:
        assert "workspace/skills" not in backup.as_posix()
        assert "workspace/plugins" not in backup.as_posix()


def test_two_dotenv_files_get_two_distinct_backups(tmp_path):
    """The checkout's docker/.env and <install-root>/.env both slug to "env".

    They carry different content -- the checkout side is the one holding the
    provider API key -- so collapsing them onto one backup name silently
    destroys the rollback copy of whichever is written first.
    """
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(tmp_path)
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    # Make each file differ from the bundle, on a key the bundle does carry.
    (repo / "docker" / ".env").write_text(
        "N_AGENT_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    (install_root / ".env").write_text(
        "N_AGENT_HOST_TERMINAL_TOKEN=stale-and-must-be-recoverable\n", encoding="utf-8")
    r = _land(repo, bundle, install_root, code_root,
              "--mode", "overwrite", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    backups = _backups(install_root)
    bodies = {b.read_text(encoding="utf-8") for b in backups}
    assert len(backups) == len({b.name for b in backups}), \
        f"backup names collided: {[b.name for b in backups]}"
    assert "N_AGENT_LOG_LEVEL=DEBUG\n" in bodies, \
        f"the checkout docker/.env backup was lost: {[b.name for b in backups]}"
    assert "N_AGENT_HOST_TERMINAL_TOKEN=stale-and-must-be-recoverable\n" in bodies


def test_identical_content_is_not_rewritten_and_not_backed_up(tmp_path):
    """AC10/AC19: same content -> no rewrite, no backup, no restart marker."""
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(tmp_path)
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    before = _snapshot(install_root)
    repo_before = _snapshot(repo)
    second = _land(repo, bundle, install_root, code_root, tmp_path=tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert _snapshot(install_root) == before
    assert _snapshot(repo) == repo_before
    assert _backups(install_root) == []
    assert "restart" not in second.stdout.lower()


def test_token_is_generated_when_bundle_carries_none_and_stays_stable(tmp_path):
    """AC19: no token in the bundle -> a fresh valid token, stable across reruns."""
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(tmp_path, without=["locals/host-terminal.token"],
                                name="no-terminal-token.tar.gz")
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    token = (install_root / "locals" / "host-terminal.token").read_text(encoding="utf-8")
    assert len(token.strip()) >= 32
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    assert (install_root / "locals" / "host-terminal.token").read_text(encoding="utf-8") == token


def test_a_carried_token_is_never_landed_empty(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(tmp_path, name="empty-token.tar.gz",
                                replace={"locals/host-terminal.token": b"   \n"})
    _land(repo, bundle, install_root, code_root, tmp_path=tmp_path)
    token = install_root / "locals" / "host-terminal.token"
    assert token.is_file()
    assert token.read_text(encoding="utf-8").strip() != ""


def test_missing_policy_falls_back_to_the_receiving_template(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(tmp_path, without=["locals/host-terminal-policy.yaml"],
                                name="no-policy.tar.gz")
    r = _land(repo, bundle, install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    policy = install_root / "locals" / "host-terminal-policy.yaml"
    assert policy.is_file() and policy.read_text(encoding="utf-8").strip() != ""


def test_missing_policy_and_no_template_refuses_to_start(tmp_path):
    """AC19: no policy in the bundle and no template on the target -> refuse."""
    repo, install_root, code_root = _target(tmp_path, with_policy_template=False)
    bundle = _make_valid_bundle(tmp_path, without=["locals/host-terminal-policy.yaml"],
                                name="no-policy.tar.gz")
    r = _land(repo, bundle, install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 3, r.stderr + r.stdout
    assert "host-terminal-policy" in (r.stdout + r.stderr)
    assert not (install_root / "locals" / "host-terminal-policy.yaml").exists()


def test_ro_mount_sources_are_created_as_files_before_any_compose_call(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    for name in ("host-terminal-policy.yaml", "host-terminal.token", "host-browser.token"):
        assert (install_root / "locals" / name).is_file()


def test_existing_directory_at_ro_mount_path_exits_4_without_deleting(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (install_root / "locals" / "host-terminal.token").mkdir(parents=True)
    (install_root / "locals" / "host-terminal.token" / "inside").write_text("x", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 4, r.stderr + r.stdout
    assert (install_root / "locals" / "host-terminal.token").is_dir()
    assert (install_root / "locals" / "host-terminal.token" / "inside").exists()


def test_a_symlinked_target_or_ancestor_is_refused(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (install_root / "locals").mkdir(parents=True)
    (install_root / "locals" / "host-browser.token").symlink_to(tmp_path / "elsewhere")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 4, r.stderr + r.stdout
    assert (install_root / "locals" / "host-browser.token").is_symlink()
    assert not (tmp_path / "elsewhere").exists()


def test_oss_env_is_restored_0600_and_kept_when_bundle_omits_it(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    oss = install_root / "secrets" / "oss.env"
    assert oss.is_file()
    assert oct(oss.stat().st_mode & 0o777) == "0o600"
    original = oss.read_bytes()

    without = _make_valid_bundle(tmp_path, without=["secrets/oss.env"], name="no-oss.tar.gz")
    assert _land(repo, without, install_root, code_root, tmp_path=tmp_path).returncode == 0
    assert oss.read_bytes() == original, "an omitted oss.env must never clear the target"


def test_env_and_secret_files_land_0600(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    for path in (install_root / ".env", install_root / "secrets" / "oss.env",
                 install_root / "locals" / "host-terminal.token",
                 install_root / "locals" / "host-browser.token",
                 repo / "docker" / ".env"):
        assert oct(path.stat().st_mode & 0o777) == "0o600", path


def test_both_roots_are_written_as_local_values(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    values = _read_env(repo)
    assert values["N_AGENT_INSTALL_ROOT"] == str(install_root)
    assert values["N_AGENT_CODE_ROOT"] == str(code_root)
    assert SRC_INSTALL_ROOT not in (repo / "docker" / ".env").read_text(encoding="utf-8")


def test_install_root_containing_spaces_works(tmp_path):
    repo, _, code_root = _target(tmp_path)
    spaced = tmp_path / "a b c"
    r = _land(repo, _make_valid_bundle(tmp_path), spaced, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert (spaced / "locals" / "host-terminal.token").is_file()
    assert _read_env(repo)["N_AGENT_INSTALL_ROOT"] == str(spaced)


def test_known_host_path_fields_are_remapped_by_path_segment(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    policy = (install_root / "locals" / "host-terminal-policy.yaml").read_text(encoding="utf-8")
    assert f"{install_root}/workspace" in policy
    assert SRC_INSTALL_ROOT not in policy


@pytest.mark.parametrize("foreign", ["/Users/src/code-old/x", "/Users/src/installer/y",
                                     "https://example.com/Users/src/code/api"])
def test_adjacent_prefixes_and_free_text_are_never_rewritten(tmp_path, foreign):
    repo, install_root, code_root = _target(tmp_path)
    bundle = _make_valid_bundle(
        tmp_path, name=f"foreign-{abs(hash(foreign)) % 10000}.tar.gz",
        replace={"locals/host-terminal-policy.yaml":
                 f"host_root: {SRC_INSTALL_ROOT}/workspace\nnote: {foreign}\n".encode("utf-8")})
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    policy = (install_root / "locals" / "host-terminal-policy.yaml").read_text(encoding="utf-8")
    assert foreign in policy


def test_workspace_merge_keeps_existing_top_level_dirs(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    existing = install_root / "workspace" / "skills" / "demo"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "# mine\n"


def test_workspace_overwrite_replaces_the_directory_and_clears_residue(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    existing = install_root / "workspace" / "skills" / "demo"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (existing / "stale.md").write_text("old version residue\n", encoding="utf-8")
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 "--mode", "overwrite", tmp_path=tmp_path).returncode == 0
    assert (existing / "SKILL.md").read_text(encoding="utf-8") == "# demo\n"
    assert not (existing / "stale.md").exists()
    assert _backups(install_root), "the replaced directory must be backed up"


def test_workspace_restore_lands_plugins_too(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    assert (install_root / "workspace" / "plugins" / "demo-plugin" / "plugin.json").is_file()


def test_the_bundled_install_script_is_never_executed(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    marker = tmp_path / "installer-ran"
    bundle = _make_valid_bundle(tmp_path, marker=marker, name="tripwire.tar.gz")
    assert _land(repo, bundle, install_root, code_root, tmp_path=tmp_path).returncode == 0
    assert not marker.exists(), "config-import.sh must never execute the bundled install.sh"
    text = IMPORT_SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"^[^#\n]*(sh|bash|exec)\s+\"?\$\{?\w*(stag|extract|unpack)\w*\}?/?install",
                         text, re.MULTILINE | re.IGNORECASE)


def test_host_landing_never_prints_secret_values(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert CANARY not in (r.stdout + r.stderr)
    assert DB_CANARY not in (r.stdout + r.stderr)


def test_no_output_or_backup_file_name_carries_a_key(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (repo / "docker" / ".env").write_text("N_AGENT_LOG_LEVEL=INFO\n", encoding="utf-8")
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
              "--mode", "overwrite", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert _backups(install_root), "nothing was backed up, the assertion below is vacuous"
    for path in install_root.rglob("*"):
        assert CANARY not in path.name
        assert "api_key" not in path.name.lower()


def test_host_only_leaves_no_staging_directory_behind(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    assert _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
                 tmp_path=tmp_path).returncode == 0
    leftovers = [p.name for p in install_root.iterdir() if p.name.startswith(".n-agent")]
    assert leftovers == []


def test_preflight_failure_leaves_the_target_completely_untouched(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    install_root.mkdir()
    before = _snapshot(install_root)
    repo_before = _snapshot(repo)
    bad = _make_bundle_with_inner(tmp_path, inner_arcname="../evil")
    r = _land(repo, bad, install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 2, r.stderr + r.stdout
    assert _snapshot(install_root) == before
    assert _snapshot(repo) == repo_before


def test_workspace_scripts_carrying_the_source_path_are_reported(tmp_path):
    """The exporter remaps the five config files it owns, but never rewrites a
    user's skill scripts -- rewriting their contents is riskier than leaving
    them. A silently stale absolute path breaks the skill on the new machine,
    so the importer has to say so. Paths only: never a line of the file."""
    repo, install_root, code_root = _target(tmp_path)
    stale = _inner_archive([
        ("skills/demo/SKILL.md", b"# demo\n"),
        ("skills/demo/scripts/upload.py",
         f'ENV = "{SRC_INSTALL_ROOT}/secrets/oss.env"\n'.encode("utf-8")),
    ])
    bundle = _make_valid_bundle(tmp_path, name="stale-path.tar.gz",
                                replace={"workspace/skills.tar.gz": stale})
    r = _land(repo, bundle, install_root, code_root, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    output = r.stdout + r.stderr
    assert "skills/demo/scripts/upload.py" in output, output
    assert SRC_INSTALL_ROOT in output, output
    # The finding must not leak the file's contents, only its path.
    assert "ENV =" not in output, output
    # Non-vacuity: the same landing with no stale path in the payload stays
    # quiet, so the assertions above cannot be satisfied by a blanket warning.
    repo2, install2, code2 = _target(tmp_path, name="repo2")
    clean = _land(repo2, _make_valid_bundle(tmp_path), install2, code2,
                  tmp_path=tmp_path)
    assert clean.returncode == 0, clean.stderr + clean.stdout
    assert "still name the source machine" not in (clean.stdout + clean.stderr)


def test_dry_run_changes_nothing_on_the_host(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    install_root.mkdir()
    before = _snapshot(install_root)
    repo_before = _snapshot(repo)
    r = _land(repo, _make_valid_bundle(tmp_path), install_root, code_root,
              "--dry-run", tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert _snapshot(install_root) == before
    assert _snapshot(repo) == repo_before


# ---------------------------------------------------------------------------
# T7 S6-S9: docker interaction, with a stub `docker` on PATH
# ---------------------------------------------------------------------------


_EMPTY_TARGET_EXPORT = json.dumps({
    "schema_version": 1,
    "created_at": "2026-09-10T10:00:00+08:00",
    "source": {"hostname": "dst", "install_root": "/i", "code_root": "/c"},
    "redacted": True,
    "sections": {
        "providers": [], "knowledge_bases": [], "mcp_sites": [],
        "external_memory_providers": [], "external_memory_global_config": None,
        "plugins": [{"key": "demo-plugin"}], "skills": [{"name": "demo"}],
        "scheduled_tasks": [], "gateway_home_targets": [], "task_config": None,
    },
})


def _report(*actions: str) -> str:
    items = [
        {"section": "providers", "natural_key": f"p{index}", "outcome": "degraded",
         "action": action, "reasons": ["secret redacted"]}
        for index, action in enumerate(actions)
    ]
    counts = {"created": 0, "updated": 0, "skipped": 0, "degraded": len(items), "failed": 0}
    return json.dumps({"mode": "merge", "exit_code": 0, "counts": counts, "items": items})


def _stub_docker_daemon(bin_dir: Path, log: Path, *, running: bool = True,
                        network_exists: bool = True, import_report: str | None = None,
                        import_rc: int = 0, target_export: str | None = None,
                        export_rc: int = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    report = import_report if import_report is not None else _report("none")
    export = target_export if target_export is not None else _EMPTY_TARGET_EXPORT
    # `docker compose exec` against a stopped service fails -- a stub that answers
    # it anyway lets the importer pass tests it would fail against a real daemon.
    # The restart stub drops a marker, so "stopped, then started" behaves for real.
    started = log.parent / "service-started"
    script = f"""#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ {1 if running else 0} -eq 1 ] || [ -f "{started}" ]; then up=1; else up=0; fi
case "$*" in
  *"config import"*) cat >/dev/null
    [ "$up" -eq 1 ] || {{ printf 'service "n-agent" is not running\\n' >&2; exit 1; }}
    printf '%s\\n' '{report}'; exit {import_rc};;
  *"config export"*) cat >/dev/null 2>/dev/null
    [ "$up" -eq 1 ] || exit 1
    printf '%s\\n' '{export}'; exit {export_rc};;
  *" ps "*) [ "$up" -eq 1 ] && printf 'n-agent\\n'; exit 0;;
  *"network inspect"*) exit {0 if network_exists else 1};;
esac
exit 0
"""
    (bin_dir / "docker").write_text(script, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)


def _stub_restart(repo: Path, log: Path, *, exit_code: int = 0) -> None:
    marker = log.parent / "service-started"
    touch = f': > "{marker}"\n' if exit_code == 0 else ""
    (repo / "docker" / "restart.sh").write_text(
        f"#!/bin/sh\nprintf 'restart\\n' >> \"{log}\"\n{touch}exit {exit_code}\n",
        encoding="utf-8")
    (repo / "docker" / "restart.sh").chmod(0o755)


def _full_import(tmp_path: Path, *args: str, docker_kwargs: dict | None = None,
                 restart_exit: int = 0, bundle: Path | None = None,
                 ) -> tuple[subprocess.CompletedProcess, Path, Path, Path]:
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, docker_log, **(docker_kwargs or {}))
    _stub_restart(repo, restart_log, exit_code=restart_exit)
    result = _run_import(
        repo, bundle or _make_valid_bundle(tmp_path),
        "--install-root", str(install_root), "--code-root", str(code_root), *args,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    return result, docker_log, restart_log, install_root


def _calls(log: Path) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_full_import_pins_the_compose_context(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = _calls(docker_log)
    assert "--project-directory" in calls
    assert "--env-file" in calls
    assert "-f " in calls


def test_pre_existing_scan_runs_before_any_host_write(tmp_path):
    """S6: the read-only scan must happen while the old deployment is intact."""
    result, docker_log, _, _ = _full_import(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = _calls(docker_log).splitlines()
    export_index = next(i for i, line in enumerate(calls) if "config export" in line)
    import_index = next(i for i, line in enumerate(calls) if "config import" in line)
    assert export_index < import_index


def test_existing_db_with_unreadable_container_exits_3_without_importing(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (install_root / "locals").mkdir(parents=True)
    (install_root / "locals" / "sessions.db").write_bytes(b"SQLite format 3\0")
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    _stub_docker_daemon(bin_dir, docker_log, running=False)
    _stub_restart(repo, tmp_path / "restart.log")
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 3, result.stderr + result.stdout
    assert "config import" not in _calls(docker_log)


def test_a_new_install_uses_an_explicit_empty_pre_existing_set(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path, docker_kwargs={"running": False})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "config import" in _calls(docker_log)


def test_missing_kb_network_is_created_and_reported(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path, docker_kwargs={"network_exists": False})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "network create n-kb_default" in _calls(docker_log)
    assert "N-KB" in (result.stdout + result.stderr)


def test_dry_run_never_creates_the_network_or_restarts(tmp_path):
    result, docker_log, restart_log, install_root = _full_import(
        tmp_path, "--dry-run", docker_kwargs={"network_exists": False})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "network create" not in _calls(docker_log)
    assert _calls(restart_log) == ""
    assert not install_root.exists()


def test_dry_run_against_a_stopped_service_reports_the_db_diff_as_unknown(tmp_path):
    """S12: dry-run must never start anything, so with the service down the
    database diff is genuinely unknowable. Saying so is the honest answer --
    an empty report plus exit 1 would read as "nothing to import"."""
    result, docker_log, restart_log, _ = _full_import(
        tmp_path, "--dry-run", docker_kwargs={"running": False})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "config import" not in _calls(docker_log)
    assert _calls(restart_log) == ""
    assert "unknown" in result.stdout, result.stdout


def test_dry_run_passes_dry_run_through_to_the_container_cli(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr + result.stdout
    line = next(l for l in _calls(docker_log).splitlines() if "config import" in l)
    assert "--dry-run" in line


def test_db_import_uses_the_internal_stdin_envelope(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    line = next(l for l in _calls(docker_log).splitlines() if "config import" in l)
    assert "--stdin" in line and "--internal-context" in line and "--json" in line
    assert "--mode merge" in line


def test_schedules_enabled_adds_with_schedules(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path, "--schedules-enabled")
    assert result.returncode == 0, result.stderr + result.stdout
    line = next(l for l in _calls(docker_log).splitlines() if "config import" in l)
    assert "--with-schedules" in line


def test_schedules_are_disabled_by_default(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path)
    assert result.returncode == 0
    line = next(l for l in _calls(docker_log).splitlines() if "config import" in l)
    assert "--with-schedules" not in line


def test_restart_is_driven_by_action_not_by_degraded_outcome(tmp_path):
    """S9: every item is degraded but action=none -> no restart at all."""
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, docker_log, import_report=_report("none", "none"))
    _stub_restart(repo, restart_log)
    bundle = _make_valid_bundle(tmp_path)
    first = _run_import(repo, bundle, "--install-root", str(install_root),
                        "--code-root", str(code_root),
                        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert first.returncode == 0, first.stderr + first.stdout
    restart_log.write_text("", encoding="utf-8")   # host files already landed
    second = _run_import(repo, bundle, "--install-root", str(install_root),
                         "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert _calls(restart_log) == "", "degraded must not be mistaken for a write"


def test_an_actual_write_refreshes_the_running_service(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, docker_log, import_report=_report("none", "updated"))
    _stub_restart(repo, restart_log)
    bundle = _make_valid_bundle(tmp_path)
    assert _run_import(repo, bundle, "--install-root", str(install_root),
                       "--code-root", str(code_root),
                       env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
                       tmp_path=tmp_path).returncode == 0
    restart_log.write_text("", encoding="utf-8")
    second = _run_import(repo, bundle, "--install-root", str(install_root),
                         "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "restart" in _calls(restart_log)


def test_partial_record_failure_keeps_exit_1_and_still_applies_the_rest(tmp_path):
    failing = json.dumps({
        "mode": "merge", "exit_code": 1,
        "counts": {"created": 1, "updated": 0, "skipped": 0, "degraded": 0, "failed": 1},
        "items": [
            {"section": "providers", "natural_key": "ok", "outcome": "created",
             "action": "created", "reasons": []},
            {"section": "plugins", "natural_key": "bad", "outcome": "failed",
             "action": "none", "reasons": ["registry error"]},
        ],
    })
    result, _, restart_log, _ = _full_import(
        tmp_path, docker_kwargs={"import_report": failing, "import_rc": 1})
    assert result.returncode == 1, result.stderr + result.stdout
    assert "restart" in _calls(restart_log), "committed items must still be made effective"


def test_start_failure_before_the_db_import_exits_3_and_never_calls_the_cli(tmp_path):
    result, docker_log, _, _ = _full_import(tmp_path, restart_exit=1)
    assert result.returncode == 3, result.stderr + result.stdout
    assert "config import" not in _calls(docker_log)


def test_refresh_failure_after_the_db_commit_reports_applied_but_not_effective(tmp_path):
    """The first restart succeeds, the post-commit refresh fails -> exit 3 + a
    report that says the config is in the DB but not yet effective."""
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, docker_log, import_report=_report("updated"))
    (repo / "docker" / "restart.sh").write_text(
        f'#!/bin/sh\nprintf "restart\\n" >> "{restart_log}"\n'
        f'if [ "$(wc -l < "{restart_log}" | tr -d " ")" -gt 1 ]; then exit 1; fi\nexit 0\n',
        encoding="utf-8")
    (repo / "docker" / "restart.sh").chmod(0o755)
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 3, result.stderr + result.stdout
    combined = (result.stdout + result.stderr).lower()
    assert "not effective" in combined or "未生效" in (result.stdout + result.stderr)
    assert "config import" in _calls(docker_log)


def test_a_failed_import_never_wipes_the_target_database(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    (install_root / "locals").mkdir(parents=True)
    database = install_root / "locals" / "sessions.db"
    database.write_bytes(b"SQLite format 3\0precious")
    bin_dir = tmp_path / "bin"
    _stub_docker_daemon(bin_dir, tmp_path / "docker.log")
    _stub_restart(repo, tmp_path / "restart.log", exit_code=1)
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 3
    assert database.read_bytes() == b"SQLite format 3\0precious"


def test_docker_missing_exits_3_before_touching_the_target(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{empty_bin}:/usr/bin:/bin"}, tmp_path=tmp_path)
    assert result.returncode == 3, result.stderr + result.stdout
    assert not install_root.exists()


def test_the_report_lists_the_follow_up_actions(tmp_path):
    result, _, _, _ = _full_import(tmp_path)
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    for hint in ("MCP", "172.19.0.0/16"):
        assert hint in combined, hint


def test_full_import_never_prints_secret_values(tmp_path):
    result, _, _, _ = _full_import(tmp_path)
    assert result.returncode == 0, result.stderr
    assert CANARY not in (result.stdout + result.stderr)
    assert DB_CANARY not in (result.stdout + result.stderr)


def test_restart_script_can_be_launched_with_plain_sh():
    """S8: `sh docker/restart.sh` must work, so the Bash-only file needs a
    POSIX-safe re-exec prelude ahead of arrays and pipefail."""
    text = (ROOT / "docker" / "restart.sh").read_text(encoding="utf-8")
    prelude, _, remainder = text.partition("set -euo pipefail")
    assert "bash" in prelude.lower(), "no bash re-exec prelude before `set -o pipefail`"
    assert "exec" in prelude
    assert remainder, "restart.sh no longer contains the bash options it needs"


# ---------------------------------------------------------------------------
# T7 S8: the migration maintenance window
# ---------------------------------------------------------------------------


def _stub_restart_recording_env(repo: Path, log: Path) -> None:
    """Records, on every start, whether the maintenance switch was set."""
    (repo / "docker" / "restart.sh").write_text(
        f'#!/bin/sh\n'
        f'if grep -q "^N_AGENT_MIGRATION_MAINTENANCE=" "{repo}/docker/.env" 2>/dev/null; then\n'
        f'  printf "restart maintenance\\n" >> "{log}"\n'
        f'else\n'
        f'  printf "restart normal\\n" >> "{log}"\n'
        f'fi\nexit 0\n', encoding="utf-8")
    (repo / "docker" / "restart.sh").chmod(0o755)


def test_the_import_runs_inside_a_maintenance_window_and_leaves_it(tmp_path):
    """S8: the service is parked while the tables are rewritten, and the
    switch is removed again before the final refresh."""
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, tmp_path / "docker.log", import_report=_report("updated"))
    _stub_restart_recording_env(repo, restart_log)
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert _calls(restart_log).splitlines() == ["restart maintenance", "restart normal"]
    assert "N_AGENT_MIGRATION_MAINTENANCE" not in _read_env(repo)


def test_the_maintenance_switch_is_removed_even_when_the_start_fails(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    _stub_docker_daemon(bin_dir, tmp_path / "docker.log")
    _stub_restart(repo, tmp_path / "restart.log", exit_code=1)
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 3
    assert "N_AGENT_MIGRATION_MAINTENANCE" not in _read_env(repo)


def test_a_rerun_after_the_fault_is_removed_only_touches_what_is_incomplete(tmp_path):
    """S14: a partial failure exits 1 with the host side already landed. Once
    the fault is gone, rerunning the same bundle must be a no-op -- no rewrite,
    no backup, no restart -- not a second full application."""
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    docker_log = tmp_path / "docker.log"
    restart_log = tmp_path / "restart.log"
    bundle = _make_valid_bundle(tmp_path)
    common = ("--install-root", str(install_root), "--code-root", str(code_root))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    failing = json.loads(_report("created"))
    failing["items"].append({"section": "providers", "natural_key": "broken",
                             "outcome": "failed", "action": "none",
                             "reasons": ["registry rejected the record"]})
    failing["counts"]["failed"] = 1
    failing["exit_code"] = 1
    _stub_docker_daemon(bin_dir, docker_log, import_report=json.dumps(failing), import_rc=1)
    _stub_restart(repo, restart_log)
    first = _run_import(repo, bundle, *common, env=env, tmp_path=tmp_path)
    assert first.returncode == 1, first.stderr + first.stdout
    landed = _snapshot(install_root)
    assert landed, "the host side did not land, the no-op assertion below is vacuous"

    # Fault removed: the records that did commit are now reported as unchanged.
    _stub_docker_daemon(bin_dir, docker_log, import_report=_report("none", "none"))
    restart_log.write_text("", encoding="utf-8")
    before_backups = _backups(install_root)
    second = _run_import(repo, bundle, *common, env=env, tmp_path=tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert _snapshot(install_root) == landed
    assert _backups(install_root) == before_backups
    assert _calls(restart_log) == ""


def test_a_fully_skipped_rerun_never_enters_the_maintenance_window(tmp_path):
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, tmp_path / "docker.log", import_report=_report("none"))
    _stub_restart_recording_env(repo, restart_log)
    bundle = _make_valid_bundle(tmp_path)
    common = ("--install-root", str(install_root), "--code-root", str(code_root))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    assert _run_import(repo, bundle, *common, env=env, tmp_path=tmp_path).returncode == 0
    restart_log.write_text("", encoding="utf-8")
    second = _run_import(repo, bundle, *common, env=env, tmp_path=tmp_path)
    assert second.returncode == 0, second.stderr + second.stdout
    assert _calls(restart_log) == ""
    assert "N_AGENT_MIGRATION_MAINTENANCE" not in _read_env(repo)


# ---------------------------------------------------------------------------
# T7 S8: the migration start mode of docker/restart.sh
#
# docker, curl and sleep are stubbed on PATH: no daemon, no service, no
# network, no waiting. The real docker/restart.sh is executed as-is.
# ---------------------------------------------------------------------------


RESTART_SCRIPT = ROOT / "docker" / "restart.sh"
DEFAULT_PUBLIC_HEALTH = "nagent.localhost"
DEFAULT_HOST_HEALTH = "127.0.0.1:8201/health"


def _stub_restart_environment(bin_dir: Path, *, docker_log: Path, curl_log: Path,
                              curl_fail: tuple[str, ...] = (),
                              container_health_rc: int = 0) -> None:
    """A fake docker/curl/sleep trio for docker/restart.sh.

    ``curl_fail`` lists URL substrings whose probe must fail (exit 7, the real
    "could not connect" code), which is how a machine without the public
    reverse proxy behaves.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    cases = "".join(f"  *{pattern}*) exit 7;;\n" for pattern in curl_fail)
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$*" >> "{docker_log}"\n'
        'case "$* " in\n'
        f'  *"exec -T n-agent python"*) cat >/dev/null 2>/dev/null; exit {container_health_rc};;\n'
        'esac\n'
        "exit 0\n",
        encoding="utf-8")
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        "url=\n"
        'for argument in "$@"; do\n'
        '  case "$argument" in http://*|https://*) url=$argument;; esac\n'
        "done\n"
        f'printf \'%s\\n\' "$url" >> "{curl_log}"\n'
        'case "$url" in\n'
        f"{cases}"
        "esac\n"
        "printf '%s\\n' '{\"status\":\"ok\"}'\n"
        "exit 0\n",
        encoding="utf-8")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for name in ("docker", "curl", "sleep"):
        (bin_dir / name).chmod(0o755)


def _run_restart(tmp_path: Path, *, env: dict[str, str] | None = None,
                 curl_fail: tuple[str, ...] = (), container_health_rc: int = 0,
                 launcher: str = "sh",
                 ) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "restart-bin"
    docker_log = tmp_path / "restart-docker.log"
    curl_log = tmp_path / "restart-curl.log"
    _stub_restart_environment(bin_dir, docker_log=docker_log, curl_log=curl_log,
                              curl_fail=curl_fail, container_health_rc=container_health_rc)
    process_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "N_AGENT_CONTAINER_HEALTH_ATTEMPTS": "1",
        "N_AGENT_HOST_HEALTH_ATTEMPTS": "1",
        "N_AGENT_PUBLIC_HEALTH_ATTEMPTS": "1",
    }
    process_env.update(env or {})
    result = _run([launcher, str(RESTART_SCRIPT)], cwd=ROOT, env=process_env)
    return result, docker_log, curl_log


def test_migration_start_skips_public_health_without_a_configured_proxy(tmp_path):
    """S8: on a brand-new machine nagent.localhost does not exist yet; the
    migration start must still succeed and must never probe it."""
    result, _, curl_log = _run_restart(
        tmp_path, curl_fail=(DEFAULT_PUBLIC_HEALTH,),
        env={"N_AGENT_MIGRATION_START": "1"})
    assert result.returncode == 0, result.stderr + result.stdout
    probes = _calls(curl_log)
    assert DEFAULT_PUBLIC_HEALTH not in probes, probes
    assert DEFAULT_HOST_HEALTH in probes, probes


def test_migration_start_always_validates_the_host_port(tmp_path):
    """S8: the host /health gate is explicit and unconditional in migration
    mode, even when the very first probe already answers."""
    result, _, _ = _run_restart(tmp_path, curl_fail=(DEFAULT_PUBLIC_HEALTH,),
                                env={"N_AGENT_MIGRATION_START": "1"})
    assert result.returncode == 0, result.stderr + result.stdout
    assert "host port health ready" in result.stdout, result.stdout


def test_migration_start_fails_when_the_host_port_never_answers(tmp_path):
    result, _, curl_log = _run_restart(
        tmp_path, curl_fail=("127.0.0.1", DEFAULT_PUBLIC_HEALTH),
        env={"N_AGENT_MIGRATION_START": "1"})
    assert result.returncode == 1, result.stderr + result.stdout
    assert DEFAULT_HOST_HEALTH in _calls(curl_log)


def test_migration_start_still_requires_an_explicitly_configured_proxy(tmp_path):
    """S8: configuring N_AGENT_PUBLIC_HEALTH_URL is the signal that a public
    proxy exists, so its failure must fail the start."""
    result, _, curl_log = _run_restart(
        tmp_path, curl_fail=("proxy.example",),
        env={"N_AGENT_MIGRATION_START": "1",
             "N_AGENT_PUBLIC_HEALTH_URL": "http://proxy.example/health"})
    assert result.returncode == 1, result.stderr + result.stdout
    assert "proxy.example" in _calls(curl_log)


def test_a_plain_restart_still_requires_the_default_public_health(tmp_path):
    """Regression lock: existing deployments run `sh docker/restart.sh` with no
    migration switch and must keep the unconditional public health gate."""
    for launcher in ("sh", "bash"):
        result, _, curl_log = _run_restart(tmp_path, curl_fail=(DEFAULT_PUBLIC_HEALTH,),
                                           launcher=launcher)
        assert result.returncode == 1, result.stderr + result.stdout
        assert DEFAULT_PUBLIC_HEALTH in _calls(curl_log)


def test_migration_start_keeps_the_compose_call_order(tmp_path):
    """S8: fake docker call order -- down, rm, up, ps, then the container
    health probe; the migration mode changes health gating only."""
    result, docker_log, _ = _run_restart(
        tmp_path, curl_fail=(DEFAULT_PUBLIC_HEALTH,),
        env={"N_AGENT_MIGRATION_START": "1"})
    assert result.returncode == 0, result.stderr + result.stdout
    calls = _calls(docker_log).splitlines()

    def index(needle: str) -> int:
        return next(i for i, line in enumerate(calls) if needle in line)

    assert index("compose down") < index("compose rm") < index("compose up") \
        < index("compose ps") < index("exec -T n-agent python")


def test_migration_start_fails_when_the_container_never_becomes_healthy(tmp_path):
    result, docker_log, _ = _run_restart(
        tmp_path, container_health_rc=1, curl_fail=("127.0.0.1", DEFAULT_PUBLIC_HEALTH),
        env={"N_AGENT_MIGRATION_START": "1"})
    assert result.returncode == 1, result.stderr + result.stdout
    assert "exec -T n-agent python" in _calls(docker_log)


def test_config_import_starts_the_service_in_migration_mode(tmp_path):
    """S8: the importer is the caller that knows the target may be a new
    machine, so it is the one that turns the migration start mode on."""
    repo, install_root, code_root = _target(tmp_path)
    bin_dir = tmp_path / "bin"
    restart_log = tmp_path / "restart.log"
    _stub_docker_daemon(bin_dir, tmp_path / "docker.log", import_report=_report("updated"))
    (repo / "docker" / "restart.sh").write_text(
        f'#!/bin/sh\nprintf \'migration=%s\\n\' "${{N_AGENT_MIGRATION_START:-unset}}" '
        f'>> "{restart_log}"\nexit 0\n', encoding="utf-8")
    (repo / "docker" / "restart.sh").chmod(0o755)
    result = _run_import(repo, _make_valid_bundle(tmp_path),
                         "--install-root", str(install_root), "--code-root", str(code_root),
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    starts = _calls(restart_log).splitlines()
    assert starts and all(line == "migration=1" for line in starts), starts


# ===========================================================================
# T8: docker/install.sh -- the one-command entry point on a fresh machine
#
# Same rules as the sections above: plain pytest on the host, a stubbed
# `docker` on PATH, a throwaway checkout whose config-import.sh is a recorder
# script, and every artefact below tmp_path. install.sh itself must never
# write anything: it locates, checks and execs.
# ===========================================================================

import shutil

INSTALL_SCRIPT = ROOT / "docker" / "install.sh"

BUNDLE_NAME = "n-agent-config-srcmac-260910-1000.tar.gz"


def _standalone(tmp_path: Path, *, name: str = "standalone") -> Path:
    """The new machine's drop directory: install.sh plus bundles, nothing else."""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy(INSTALL_SCRIPT, directory / "install.sh")
    (directory / "install.sh").chmod(0o700)
    return directory


def _recording_repo(tmp_path: Path, *, name: str = "repo", exit_code: int = 7) -> Path:
    """A checkout whose config-import.sh only echoes what it received."""
    repo = tmp_path / name
    (repo / "docker").mkdir(parents=True, exist_ok=True)
    (repo / "docker" / "config-import.sh").write_text(
        "#!/bin/sh\n"
        'printf "ARGS:"\n'
        'for a in "$@"; do printf " [%s]" "$a"; done\n'
        'printf "\\n"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    (repo / "docker" / "config-import.sh").chmod(0o755)
    return repo


def _stub_docker_cli(bin_dir: Path, *, info_rc: int = 0, compose_rc: int = 0,
                     log: Path | None = None) -> Path:
    """A fake `docker` that answers the two probes install.sh is allowed to make."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    record = f'printf "%s\\n" "$*" >> "{log}"\n' if log is not None else ""
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        f"{record}"
        'case "$*" in\n'
        f"  info*) exit {info_rc};;\n"
        f"  compose\\ version*) exit {compose_rc};;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "docker").chmod(0o755)
    return bin_dir


def _install_env(tmp_path: Path, *, bin_dir: Path | None = None,
                 extra: dict[str, str] | None = None) -> dict[str, str]:
    """A HOME below tmp_path so a stray write is visible, and no inherited repo."""
    home = tmp_path / "install-home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "N_AGENT_REPO": ""}
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env.update(extra or {})
    return env


def _run_install(script: Path, *args: str, cwd: Path,
                 env: dict[str, str]) -> subprocess.CompletedProcess:
    process_env = dict(os.environ)
    for leaked in ("N_AGENT_INSTALL_ROOT", "N_AGENT_CODE_ROOT", "N_AGENT_REPO",
                   "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_ENV_FILES"):
        process_env.pop(leaked, None)
    process_env.update({k: v for k, v in env.items() if v != ""})
    return subprocess.run(["sh", str(script), *args], capture_output=True, text=True,
                          cwd=str(cwd), env=process_env)


def _output(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# T8 S1: script shape
# ---------------------------------------------------------------------------


def test_install_script_exists_and_is_posix_sh():
    assert INSTALL_SCRIPT.exists()
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "set -eu" in text


def test_install_script_never_unpacks_sources_or_evals_the_bundle():
    """install.sh locates, checks and execs; it never reads bundle content."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^\s*eval\b", text, re.MULTILINE) is None
    assert re.search(r"^\s*source\b", text, re.MULTILINE) is None
    assert re.search(r"^\s*\.\s+\"?\$\{?(bundle|staging|extract)", text,
                     re.MULTILINE | re.IGNORECASE) is None
    # No extraction at all: unpacking is config-import.sh's job.
    assert re.search(r"^[^#\n]*\btar\b[^\n]*(-x|--extract)", text, re.MULTILINE) is None


def test_install_execs_config_import_from_the_checkout_only():
    """The single exec target is <repo>/docker/config-import.sh -- never a
    path derived from the bundle or from the script's own directory copy."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    execs = [line.strip() for line in text.splitlines()
             if re.match(r"^[^#]*\bexec\b", line)]
    assert execs, "install.sh must hand over with exec so the exit code is the callee's"
    for line in execs:
        assert "config-import.sh" in line, line
    assert re.search(r"^[^#\n]*(sh|bash|exec)\s+\"?\$\{?\w*(stag|extract|unpack|bundle)\w*\}?/?",
                     text, re.MULTILINE | re.IGNORECASE) is None


# ---------------------------------------------------------------------------
# T8 S1: preflight order -- checkout, then docker, then exec
# ---------------------------------------------------------------------------


def test_install_fails_with_exit_3_when_no_checkout_can_be_located(tmp_path):
    """AC05: no compatible checkout anywhere above the drop directory."""
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    env = _install_env(tmp_path, bin_dir=bin_dir)
    result = _run_install(directory / "install.sh", cwd=directory, env=env)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "checkout" in _output(result)
    # zero landing: the default install root is not even created
    assert not (Path(env["HOME"]) / "install" / "n-agent").exists()
    assert sorted(p.name for p in directory.iterdir()) == ["install.sh", BUNDLE_NAME]


def test_install_exits_3_when_docker_daemon_is_unreachable(tmp_path):
    """AC05: daemon down -> exit 3, config-import.sh is never called.

    The checkout exists here, so a 3 caused by "no checkout" would be a
    different message -- asserted below to keep the two paths distinguishable.
    """
    repo = _recording_repo(tmp_path)
    marker = tmp_path / "called"
    (repo / "docker" / "config-import.sh").write_text(
        f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin", info_rc=1)
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "docker" in _output(result)
    assert "checkout" not in _output(result), "must not be confused with the no-checkout exit 3"
    assert not marker.exists()


def test_install_exits_3_when_the_compose_plugin_is_missing(tmp_path):
    repo = _recording_repo(tmp_path)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin", compose_rc=1)
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "compose" in _output(result)


def test_install_exits_3_when_docker_is_not_on_path_at_all(tmp_path):
    repo = _recording_repo(tmp_path)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          cwd=tmp_path,
                          env=_install_env(tmp_path, extra={"PATH": f"{empty_bin}:/usr/bin:/bin"}))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "docker" in _output(result)


def test_install_probes_docker_only_after_the_checkout_is_located(tmp_path):
    """Order lock: with no checkout the docker probes must not even run."""
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    docker_log = tmp_path / "docker.log"
    bin_dir = _stub_docker_cli(tmp_path / "bin", log=docker_log)
    result = _run_install(directory / "install.sh", cwd=directory,
                          env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 3, result.stdout + result.stderr
    assert not docker_log.exists(), _calls(docker_log)


def test_install_runs_both_docker_probes_before_handing_over(tmp_path):
    repo = _recording_repo(tmp_path)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    docker_log = tmp_path / "docker.log"
    bin_dir = _stub_docker_cli(tmp_path / "bin", log=docker_log)
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 7, result.stdout + result.stderr
    calls = _calls(docker_log)
    assert "info" in calls
    assert "compose version" in calls


# ---------------------------------------------------------------------------
# T8 S1: bundle candidate resolution
# ---------------------------------------------------------------------------


def test_install_uses_the_single_bundle_next_to_the_script(tmp_path):
    directory = _standalone(tmp_path)
    bundle = directory / BUNDLE_NAME
    bundle.write_bytes(b"")
    repo = _recording_repo(tmp_path)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 7, result.stdout + result.stderr
    assert f"[{bundle}]" in result.stdout, result.stdout


def test_install_rejects_multiple_bundle_candidates_without_explicit_arg(tmp_path):
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    (directory / "n-agent-config-srcmac-260911-1200.tar.gz").write_bytes(b"")
    repo = _recording_repo(tmp_path)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 2, result.stdout + result.stderr
    lowered = _output(result)
    assert "n-agent-config-srcmac-260910-1000.tar.gz" in lowered
    assert "n-agent-config-srcmac-260911-1200.tar.gz" in lowered
    # never "the newest one wins"
    assert "ARGS:" not in result.stdout


def test_install_rejects_zero_bundle_candidates(tmp_path):
    directory = _standalone(tmp_path)
    repo = _recording_repo(tmp_path)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "bundle" in _output(result)
    assert "ARGS:" not in result.stdout


def test_a_second_positional_bundle_is_rejected_by_the_real_importer(tmp_path):
    """install.sh owns no bundle semantics beyond picking the first argument:
    the real config-import.sh is the one that refuses two bundles (exit 2), and
    it does so while parsing arguments, before it touches anything."""
    first = tmp_path / BUNDLE_NAME
    first.write_bytes(b"")
    second = tmp_path / "n-agent-config-other-260911-1200.tar.gz"
    second.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(first), str(second), "--repo", str(ROOT),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "only one bundle" in result.stderr, result.stderr


def test_install_rejects_an_unreadable_bundle(tmp_path):
    repo = _recording_repo(tmp_path)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    missing = tmp_path / "n-agent-config-absent-260910-1000.tar.gz"
    result = _run_install(INSTALL_SCRIPT, str(missing), "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "ARGS:" not in result.stdout


# ---------------------------------------------------------------------------
# T8 S1: checkout location order
# ---------------------------------------------------------------------------


def test_install_prefers_the_repo_flag_over_the_environment(tmp_path):
    wanted = _recording_repo(tmp_path, name="wanted", exit_code=7)
    other = _recording_repo(tmp_path, name="other", exit_code=9)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(wanted),
                          cwd=tmp_path,
                          env=_install_env(tmp_path, bin_dir=bin_dir,
                                           extra={"N_AGENT_REPO": str(other)}))
    assert result.returncode == 7, result.stdout + result.stderr


def test_install_uses_n_agent_repo_when_no_flag_is_given(tmp_path):
    repo = _recording_repo(tmp_path, name="fromenv", exit_code=9)
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", cwd=directory,
                          env=_install_env(tmp_path, bin_dir=bin_dir,
                                           extra={"N_AGENT_REPO": str(repo)}))
    assert result.returncode == 9, result.stdout + result.stderr


def test_install_walks_up_from_the_script_to_find_the_checkout(tmp_path):
    """The exported install.sh sitting inside a checkout finds its own repo."""
    repo = _recording_repo(tmp_path, name="walkup", exit_code=9)
    directory = _standalone(repo / "locals" / "install")
    (directory / BUNDLE_NAME).write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", cwd=tmp_path,
                          env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 9, result.stdout + result.stderr


def test_install_walks_up_from_the_working_directory(tmp_path):
    repo = _recording_repo(tmp_path, name="cwdrepo", exit_code=9)
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    workdir = repo / "some" / "nested" / "dir"
    workdir.mkdir(parents=True)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", str(directory / BUNDLE_NAME),
                          cwd=workdir, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 9, result.stdout + result.stderr


def test_install_rejects_a_repo_flag_without_config_import(tmp_path):
    empty = tmp_path / "not-a-checkout"
    (empty / "docker").mkdir(parents=True)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(empty),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 3, result.stdout + result.stderr
    assert "checkout" in _output(result)


def test_install_rejects_repo_without_a_value(tmp_path):
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo",
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 2, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# T8 S1: argument and exit code pass-through
# ---------------------------------------------------------------------------


def test_install_forwards_all_arguments_to_config_import(tmp_path):
    repo = _recording_repo(tmp_path, exit_code=7)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          "--dry-run", "--install-root", "/tmp/x",
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 7, result.stdout + result.stderr
    assert result.stdout.startswith("ARGS:"), result.stdout
    assert f"[{bundle}]" in result.stdout
    assert "[--dry-run]" in result.stdout
    assert "[--install-root] [/tmp/x]" in result.stdout
    assert "--repo" not in result.stdout
    assert str(repo) not in result.stdout


def test_install_forwards_arguments_that_contain_spaces(tmp_path):
    repo = _recording_repo(tmp_path, exit_code=7)
    bundle = tmp_path / "n-agent-config-with space-260910-1000.tar.gz"
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          "--install-root", "/tmp/two words",
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 7, result.stdout + result.stderr
    assert f"[{bundle}]" in result.stdout
    assert "[/tmp/two words]" in result.stdout


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4])
def test_install_passes_the_callee_exit_code_through_unchanged(tmp_path, code):
    repo = _recording_repo(tmp_path, name=f"repo{code}", exit_code=code)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == code, result.stdout + result.stderr
    assert result.stdout.startswith("ARGS:"), result.stdout


def test_install_does_not_interpret_import_options_itself(tmp_path):
    """install.sh owns exactly one flag; everything else is opaque to it."""
    repo = _recording_repo(tmp_path, exit_code=7)
    bundle = tmp_path / BUNDLE_NAME
    bundle.write_bytes(b"")
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(INSTALL_SCRIPT, str(bundle), "--repo", str(repo),
                          "--mode", "overwrite", "--schedules-enabled", "--code-root", "/tmp/c",
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 7, result.stdout + result.stderr
    assert "[--mode] [overwrite]" in result.stdout
    assert "[--schedules-enabled]" in result.stdout
    assert "[--code-root] [/tmp/c]" in result.stdout


# ---------------------------------------------------------------------------
# T8 S1: the entry point never executes bundle content, never writes, never leaks
# ---------------------------------------------------------------------------


def test_install_never_executes_a_script_carried_by_the_bundle(tmp_path):
    """A real bundle carries its own install.sh copy; running it would be a
    remote-code-execution hole. The decoy writes a marker if it ever runs."""
    directory = _standalone(tmp_path)
    marker = tmp_path / "decoy-ran"
    bundle = _make_valid_bundle(tmp_path, marker=marker, name=BUNDLE_NAME)
    shutil.copy(bundle, directory / BUNDLE_NAME)
    repo = _recording_repo(tmp_path, exit_code=0)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=tmp_path, env=_install_env(tmp_path, bin_dir=bin_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("ARGS:"), result.stdout
    assert not marker.exists(), "install.sh must never run a script carried by the bundle"


def test_install_writes_nothing_of_its_own(tmp_path):
    """Everything that lands is config-import.sh's doing; the entry point is
    read-only, on the happy path and on every refusal."""
    directory = _standalone(tmp_path)
    (directory / BUNDLE_NAME).write_bytes(b"")
    repo = _recording_repo(tmp_path, exit_code=0)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    env = _install_env(tmp_path, bin_dir=bin_dir)
    before_dir = _snapshot(directory)
    before_repo = _snapshot(repo)
    before_home = _snapshot(Path(env["HOME"]))
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=directory, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(directory) == before_dir
    assert _snapshot(repo) == before_repo
    assert _snapshot(Path(env["HOME"])) == before_home


def test_install_never_prints_a_secret_value(tmp_path):
    directory = _standalone(tmp_path)
    bundle = _make_valid_bundle(tmp_path, name=BUNDLE_NAME)
    shutil.copy(bundle, directory / BUNDLE_NAME)
    repo = _recording_repo(tmp_path, exit_code=0)
    bin_dir = _stub_docker_cli(tmp_path / "bin")
    result = _run_install(directory / "install.sh", "--repo", str(repo),
                          cwd=tmp_path,
                          env=_install_env(tmp_path, bin_dir=bin_dir,
                                           extra={"N_AGENT_OPENAI_API_KEY": CANARY}))
    assert result.returncode == 0, result.stdout + result.stderr
    assert CANARY not in (result.stdout + result.stderr)
    assert DB_CANARY not in (result.stdout + result.stderr)
    assert "api_key" not in (result.stdout + result.stderr).lower()


def test_install_help_lists_the_entry_point_contract():
    result = subprocess.run(["sh", str(INSTALL_SCRIPT), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--repo" in result.stdout
    assert "config-import.sh" in result.stdout
