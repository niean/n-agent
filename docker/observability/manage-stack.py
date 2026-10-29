#!/usr/bin/env python3
"""Host-only lifecycle for the shared stack. No application imports or secrets."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

SERVICES = ("otel-collector", "tempo", "prometheus", "grafana", "pyroscope", "loki")
VOLUMES = {
    "tempo-data": ("tempo", "/var/tempo", "N_OBSERVABILITY_TEMPO_VOLUME"),
    "prometheus-data": ("prometheus", "/prometheus", "N_OBSERVABILITY_PROMETHEUS_VOLUME"),
    "grafana-data": ("grafana", "/var/lib/grafana", "N_OBSERVABILITY_GRAFANA_VOLUME"),
    "loki-data": ("loki", "/loki", "N_OBSERVABILITY_LOKI_VOLUME"),
    "pyroscope-data": ("pyroscope", "/var/lib/pyroscope", "N_OBSERVABILITY_PYROSCOPE_VOLUME"),
    "pyroscope-storage": ("pyroscope", "/data", "N_OBSERVABILITY_PYROSCOPE_DATA_VOLUME"),
    "pyroscope-compactor": ("pyroscope", "/data-compactor", "N_OBSERVABILITY_PYROSCOPE_COMPACTOR_VOLUME"),
    "pyroscope-metastore": ("pyroscope", "/data-metastore", "N_OBSERVABILITY_PYROSCOPE_METASTORE_VOLUME"),
}
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
PROGRESS_INTERVAL_SECONDS = 5


class StackError(RuntimeError):
    pass


class Stack:
    def __init__(self, directory, *, progress=True):
        self.directory = Path(directory).resolve()
        self.mapping_file = self.directory / ".volumes.json"
        self.progress = progress

    def report(self, message):
        if self.progress:
            print(f"[observability] {message}", file=sys.stderr, flush=True)

    def mapping(self):
        if not self.mapping_file.exists():
            return {}
        try:
            result = json.loads(self.mapping_file.read_text())
        except (ValueError, OSError) as exc:
            raise StackError("invalid .volumes.json") from exc
        if not isinstance(result, dict) or set(result) != set(VOLUMES) or any(
            not isinstance(v, str) or not NAME.fullmatch(v) for v in result.values()
        ):
            raise StackError("invalid or incomplete .volumes.json")
        return result

    def save_mapping(self, mapping):
        # Same-directory replace: interrupted writes cannot truncate the map.
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as out:
            json.dump(mapping, out, indent=2, ensure_ascii=False)
            out.write("\n")
            temporary = Path(out.name)
        temporary.replace(self.mapping_file)

    def run(self, *args, check=True, env=None, live=False, label=None):
        # Do not echo full arguments or resolved Compose config: diagnostic
        # queries may contain credentials. Lifecycle output can stream as-is.
        label = label or f"docker {args[0]}"
        started = time.monotonic()
        stopped = threading.Event()
        def heartbeat():
            while not stopped.wait(PROGRESS_INTERVAL_SECONDS):
                self.report(f"仍在执行 {label}（已等待 {int(time.monotonic() - started)}s）")
        reporter = threading.Thread(target=heartbeat, daemon=True) if self.progress else None
        if reporter is not None:
            reporter.start()
        try:
            return subprocess.run(["docker", *args], text=True, capture_output=not live,
                                  check=check, env=env, timeout=240)
        except subprocess.CalledProcessError as exc:
            # Docker output for this stack contains no application env values.
            raise StackError(f"Docker command failed: {label} (exit {exc.returncode}): {(exc.stderr or '').strip()}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StackError(f"Docker command unavailable or timed out: {label}") from exc
        finally:
            stopped.set()
            if reporter is not None:
                reporter.join()

    def compose(self, *args, mapping=None, check=True, live=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
        selected = self.mapping() if mapping is None else mapping
        env.update({VOLUMES[k][2]: v for k, v in selected.items()})
        env_file = self.directory / ".env"
        if not env_file.is_file():
            env_file = self.directory / ".env.example"
        return self.run("compose", "--project-name", "n-observability",
                        "--project-directory", str(self.directory),
                        "--env-file", str(env_file), "--file", str(self.directory / "docker-compose.yml"),
                        *args, env=env, check=check, live=live, label=f"docker compose {args[0]}")

    def preflight(self, mapping=None):
        self.report("检查 Compose 配置、网络及持久卷映射")
        try:
            config = json.loads(self.compose("config", "--format", "json", mapping=mapping).stdout)
        except ValueError as exc:
            raise StackError("Compose config is not JSON") from exc
        if set(config.get("services", {})) != set(SERVICES) or set(config.get("volumes", {})) != set(VOLUMES):
            raise StackError("unexpected shared services or volumes")
        network = config.get("networks", {}).get("telemetry", {})
        if network.get("name") != "n-telemetry" or not network.get("external"):
            raise StackError("n-telemetry must be external")
        for service, definition in config["services"].items():
            expected = {"default", "telemetry"} if service in {"otel-collector", "pyroscope"} else {"default"}
            if set(definition.get("networks", {})) != expected or definition.get("profiles"):
                raise StackError(f"invalid network/profile for {service}")
            for mount in definition.get("volumes", []):
                if mount["type"] == "bind" and (not Path(mount["source"]).exists() or not mount.get("read_only")):
                    raise StackError(f"missing or writable config mount for {service}")
        for logical, (owner, target, _) in VOLUMES.items():
            volume = config["volumes"][logical]
            if not volume.get("external") or not NAME.fullmatch(volume.get("name", "")):
                raise StackError(f"invalid external volume: {logical}")
            if mapping and volume["name"] != mapping[logical]:
                raise StackError(f"volume map not applied: {logical}")
            mounts = config["services"][owner].get("volumes", [])
            if not any(m.get("source") == logical and m.get("target") == target and m.get("type") == "volume" for m in mounts):
                raise StackError(f"missing persistent mount: {owner}:{target}")
        result = self.run("network", "inspect", "n-telemetry", check=False)
        if result.returncode == 0 and json.loads(result.stdout)[0]["Driver"] != "bridge":
            raise StackError("n-telemetry exists but is not a bridge")
        selected = self.mapping() if mapping is None else mapping
        for name in selected.values():
            if self.run("volume", "inspect", name, check=False).returncode:
                raise StackError(f"mapped volume missing: {name}; refusing empty replacement")
        self.report("预检通过")
        return config

    def ensure_resources(self, config, *, allow_create_volumes=True):
        self.report("准备共享网络与持久卷")
        if self.run("network", "inspect", "n-telemetry", check=False).returncode:
            created = self.run("network", "create", "--driver", "bridge", "n-telemetry", check=False)
            if created.returncode:
                # Another entrypoint may have created it since our inspect.
                self.run("network", "inspect", "n-telemetry")
        for volume in config["volumes"].values():
            if self.run("volume", "inspect", volume["name"], check=False).returncode:
                if not allow_create_volumes:
                    raise StackError(f"mapped volume missing: {volume['name']}")
                self.run("volume", "create", volume["name"])

    def check_other_writers(self, volume_names, allowed):
        for name in volume_names:
            ids = self.run("ps", "-q", "--filter", f"volume={name}").stdout.split()
            if set(ids) - set(allowed):
                raise StackError(f"active container outside migration owns volume {name}")

    def wait_ready(self):
        started = time.monotonic()
        deadline = started + 150
        last = "containers not yet created"
        while time.monotonic() < deadline:
            ids = self.compose("ps", "--all", "--quiet").stdout.split()
            if len(ids) == len(SERVICES):
                items = json.loads(self.run("inspect", *ids).stdout)
                ready = all(c["State"].get("Running") and c["State"].get("Health", {}).get("Status", "healthy") == "healthy" for c in items)
                if ready:
                    # Tempo/Pyroscope lack image healthchecks; query from the
                    # backend network without publishing additional host ports.
                    probes = ("http://tempo:3200/ready", "http://pyroscope:4040/ready")
                    if all(self.compose("exec", "-T", "prometheus", "wget", "-q", "-T", "5", "-O", "-", u, check=False).returncode == 0 for u in probes):
                        self.report("服务就绪检查通过")
                        return
                    self.report("容器已运行，等待 Tempo/Pyroscope 就绪探针")
                last = ", ".join(f"{c['Name']}:{c['State'].get('Status')}:{c['State'].get('Health', {}).get('Status', 'no-healthcheck')}" for c in items)
            else:
                last = f"容器数量 {len(ids)}/{len(SERVICES)}"
            self.report(f"waiting（{int(time.monotonic() - started)}/150s）：{last}")
            time.sleep(2)
        raise StackError(f"shared stack readiness timed out: {last}")

    def deploy(self):
        self.report("开始部署/重启共享观测栈 n-observability")
        config = self.preflight()
        self.report("检查旧观测容器与持久卷写入冲突")
        # Refuse silent fresh deployment over still-running legacy services.
        ids = self.run("ps", "-aq", "--filter", "label=com.docker.compose.project=n-agent").stdout.split()
        if ids:
            items = json.loads(self.run("inspect", *ids).stdout)
            if any(c["Config"]["Labels"].get("com.docker.compose.service") in SERVICES for c in items):
                raise StackError("legacy observability containers exist; use restart.sh migrate first")
        own_ids = self.compose("ps", "--all", "--quiet").stdout.split()
        self.check_other_writers([v["name"] for v in config["volumes"].values()], own_ids + [i[:12] for i in own_ids])
        self.ensure_resources(config, allow_create_volumes=not self.mapping_file.exists())
        # Bind-mounted configuration contents are not part of Compose's
        # container config hash. Recreate this project to apply config edits.
        self.report("启动/重建观测服务（实时显示 Docker 输出）")
        self.compose("up", "-d", "--force-recreate", live=True)
        self.report("等待服务就绪（轮询窗口 150s）")
        self.wait_ready()
        self.compose("ps", live=True)
        self.report("启动完成")

    def stop(self):
        self.report("停止观测服务，保留数据与网络")
        self.compose("stop", "--timeout", "30", live=True)
        self.report("停止完成")

    def migrate(self, project, backups, legacy_compose):
        self.report("检查旧观测栈及迁移条件")
        if not NAME.fullmatch(project) or project == "n-observability":
            raise StackError("invalid legacy project")
        ids = self.run("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").stdout.split()
        items = json.loads(self.run("inspect", *ids).stdout) if ids else []
        old = {}
        for item in items:
            labels = item["Config"].get("Labels", {})
            service = labels.get("com.docker.compose.service")
            if service not in SERVICES:
                continue
            if labels.get("com.docker.compose.project") != project or service in old:
                raise StackError("duplicate or mismatched legacy service labels")
            old[service] = item
        if set(old) != set(SERVICES):
            raise StackError("expected exactly six legacy observability services; no changes made")
        mapping = {}
        for key, (owner, target, _) in VOLUMES.items():
            mount = next((m for m in old[owner]["Mounts"] if m["Destination"] == target), None)
            if not mount or mount["Type"] != "volume" or not NAME.fullmatch(mount.get("Name", "")):
                raise StackError(f"missing volume mount: {owner}:{target}")
            mapping[key] = mount["Name"]
        config = self.preflight(mapping)
        if self.compose("ps", "--all", "--quiet").stdout.strip():
            raise StackError("target observability project already exists; use restart.sh instead")
        old_ids = [c["Id"] for c in old.values()]
        # Full IDs avoid prefix mismatches from Docker's normal ps output.
        self.check_other_writers(mapping.values(), old_ids + [i[:12] for i in old_ids])
        legacy_compose = Path(legacy_compose).resolve()
        if not legacy_compose.is_file():
            raise StackError("legacy Compose file required for recovery snapshot")
        backups = Path(backups)
        backups.mkdir(parents=True, exist_ok=True)
        snapshot = Path(tempfile.mkdtemp(prefix="observability-migration-", dir=backups))
        shutil.copyfile(legacy_compose, snapshot / "legacy-compose.yml")
        manifest = {s: {k: c.get(k) for k in ("Id", "Name", "Mounts", "State", "NetworkSettings")} for s, c in old.items()}
        (snapshot / "mounts.json").write_text(json.dumps(manifest, indent=2) + "\n")
        previous = self.mapping_file.read_bytes() if self.mapping_file.exists() else None
        (snapshot / "volume-map.json").write_text(json.dumps(mapping, indent=2) + "\n")
        restore = {"services": {}, "volumes": {}}
        for key, (owner, target, _) in VOLUMES.items():
            restore["volumes"][key] = {"external": True, "name": mapping[key]}
            restore["services"].setdefault(owner, {"volumes": []})["volumes"].append(
                {"type": "volume", "source": key, "target": target})
        (snapshot / "recovery-volumes.json").write_text(json.dumps(restore, indent=2) + "\n")
        # Retain a project-aware restore command after successful removal of
        # the old containers. --project-directory preserves relative binds.
        recovery = ["docker", "compose", "--project-name", project, "--project-directory", str(legacy_compose.parent),
                    "--env-file", str(legacy_compose.parent / ".env") if (legacy_compose.parent / ".env").exists() else "/dev/null",
                    "--file", str(snapshot / "legacy-compose.yml"), "--file", str(snapshot / "recovery-volumes.json"),
                    "--profile", "observability", "up", "-d", *SERVICES]
        (snapshot / "recovery.txt").write_text(
            "Stop n-observability and verify no writers before restoring legacy services.\n" + shlex.join(recovery) + "\n")
        print(f"Migration snapshot: {snapshot}", flush=True)
        self.ensure_resources(config, allow_create_volumes=False)
        try:
            self.report("停止旧观测服务（实时显示 Docker 输出）")
            for item in old.values():
                budget = max(15, int(item["Config"].get("StopTimeout") or 15))
                self.run("stop", "--time", str(budget), item["Id"], live=True)
            self.save_mapping(mapping)
            self.report("启动新观测栈，沿用原持久卷")
            self.compose("up", "-d", live=True)
            self.report("等待服务就绪（轮询窗口 150s）")
            self.wait_ready()
        except (StackError, OSError, ValueError) as exc:
            self.report("迁移失败，停止新写入者并恢复旧服务")
            try:
                self.stop()
            except StackError as stop_error:
                raise StackError(f"manual recovery required: cannot stop new writers; snapshot {snapshot}") from stop_error
            # stop success is not sufficient if an unrelated writer appeared.
            self.check_other_writers(mapping.values(), old_ids + [i[:12] for i in old_ids])
            cleanup = self.compose("rm", "--force", check=False)
            if previous is None:
                self.mapping_file.unlink(missing_ok=True)
            else:
                self.mapping_file.write_bytes(previous)
            running = [c["Id"] for c in old.values() if c["State"].get("Running")]
            if running:
                self.run("start", *running, live=True)
            if cleanup.returncode:
                raise StackError(f"legacy services restored; stopped target containers need manual recovery: {snapshot}") from exc
            raise StackError(f"migration failed; legacy services restored; snapshot {snapshot}: {exc}") from exc
        # No -v: retain even formerly anonymous volumes, now explicitly mapped.
        self.report("清理旧容器，保留原持久卷")
        self.run("rm", *old_ids, live=True)
        print("Migration complete; legacy volumes retained and mapped in .volumes.json", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "stop", "migrate", "compose"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "compose" and (not args.args or args.args[0].startswith("-")):
        parser.error("pass a Compose subcommand; global project/file options are fixed")
    previous_umask = os.umask(0o077)
    stack = Stack(Path(__file__).parent, progress=args.action != "compose")
    lock = None
    try:
        diagnostic = args.action == "compose" and args.args and args.args[0] in {
            "ps", "logs", "config", "exec", "images", "top", "version",
        }
        if not diagnostic:
            lock = (stack.directory / ".lifecycle.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StackError("another observability lifecycle operation is running") from exc
        if args.action == "compose":
            result = stack.compose(*args.args, check=False)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode
        if args.action == "migrate":
            options = argparse.ArgumentParser()
            options.add_argument("--legacy-project", default="n-agent")
            selected = options.parse_args(args.args)
            root = stack.directory.parents[1]
            stack.migrate(selected.legacy_project, root / "locals/harness_tmp", root / "docker/docker-compose.yml")
        elif args.args:
            parser.error("unexpected arguments")
        else:
            getattr(stack, args.action)()
    except (StackError, OSError, ValueError) as exc:
        print(f"observability: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()
        os.umask(previous_umask)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
