"""Tests for py-spy profile sampling and Pyroscope ingest (T7 of
plan-261024-otel-observability).

Covers (spec Components 5, plan item 17):
- serial window loop: no parallel windows, no backlog catch-up
- failure backoff 10/30 seconds, third consecutive failure disables the
  sampler and reports degraded; success resets the failure counter
- watchdog fires at window + 5s
- private temp files: dir 0700, file 0600, system temp (never workspace),
  16 MiB cap enforced DURING sampling (subprocess terminated, not recorded
  to completion)
- folded-stack safe projection: dynamic-code filenames, thread names and
  exception-sampling segments dropped; workspace/user prefixes stripped;
  all-empty projection reports empty_profile and never uploads the
  original file
- shutdown: terminate -> kill/wait within the 2s budget
- one independent sampler per PID (multi-worker isolation)
- fixed py-spy command line (--pid <self> --format raw --rate 49
  --duration <window>) and never --locals
- upload contract: 3s timeout, HTTP non-success / attach failure / nonzero
  exit all count as failures, success and failure both clean up temp
  files, at most one window in flight, stderr bounded and safely
  projected, spike-verified ingest params (name/format/units/
  aggregationType/sampleRate/from/until), window length == interval
- missing py-spy binary: degraded profiling signal, never an app failure
- handle lifecycle: profiler stop registered via register_stop_callback
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from app.config import Settings
from app.infrastructure.observability import profiling
from app.infrastructure.observability.profiling import (
    MAX_PROFILE_BYTES,
    UPLOAD_TIMEOUT_SECONDS,
    ProfilingSampler,
    get_or_create_profiler,
    project_folded_stacks,
)


SAFE_FOLDED = (
    "app.main;handle_request (app/main.py:100) 5\n"
    "app.main;handle_request;run_tools (app/main.py:104) 3\n"
)


# ---------------------------------------------------------------------------
# Fakes: subprocess factory / process / uploader
# ---------------------------------------------------------------------------


class FakeProcess:
    """Duck-typed subprocess.Popen driven by a behavior callable.

    Behavior runs in a daemon thread; it writes to stdout/stderr files and
    must call ``proc.finish(rc)``. A hanging behavior only completes when
    ``kill()`` releases it (``terminate()`` alone does not)."""

    def __init__(self, *, stdout, stderr, behavior, recorder):
        self.pid = recorder.next_pid()
        self._stdout = stdout
        self._stderr = stderr
        self._behavior = behavior
        self._recorder = recorder
        self._rc: int | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._killed = False
        self._terminated = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        behavior = self._behavior
        if behavior is not None:
            behavior(self)
        if self._rc is None:
            # Hanging behavior: survives until kill() (terminate() alone
            # does not finish it).
            self._done.wait()

    def finish(self, rc: int = 0) -> None:
        with self._lock:
            self._rc = rc
        self._done.set()

    def write_stdout(self, data: bytes) -> None:
        self._stdout.write(data)
        self._stdout.flush()

    def write_stderr(self, data: bytes) -> None:
        self._stderr.write(data)
        self._stderr.flush()

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self._rc

    @property
    def returncode(self):
        return self._rc

    def terminate(self) -> None:
        self._terminated = True
        self._recorder.record_terminate(self)

    def kill(self) -> None:
        self._killed = True
        self.finish(-9)

    @property
    def killed(self) -> bool:
        return self._killed


class FakeFactory:
    """Records the exact command line and temp-file permissions observed at
    spawn time; pops per-window behaviors in order."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pid = 4242
        self.commands: list[list[str]] = []
        self.spawn_records: list[dict] = []
        self.behaviors: deque = deque()
        self.active = 0
        self.max_active = 0
        self.processes: list[FakeProcess] = []

    def queue(self, *behaviors) -> None:
        self.behaviors.extend(behaviors)

    def next_pid(self) -> int:
        with self._lock:
            self._pid += 1
            return self._pid

    def record_terminate(self, proc: FakeProcess) -> None:
        pass

    def __call__(self, command, *, stdout, stderr):
        stdout.flush()
        # The output path is the --output argument (os.fdopen file objects
        # expose an int fd as .name, not the path).
        stdout_path = command[command.index("--output") + 1]
        record = {
            "command": list(command),
            "stdout_path": stdout_path,
            "stdout_mode": os.stat(stdout_path).st_mode & 0o777,
            "parent_dir": os.path.dirname(os.path.abspath(stdout_path)),
            "parent_mode": os.stat(os.path.dirname(os.path.abspath(stdout_path))).st_mode
            & 0o777,
        }
        behavior = self.behaviors.popleft() if self.behaviors else None
        with self._lock:
            self.commands.append(record["command"])
            self.spawn_records.append(record)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        proc = FakeProcess(
            stdout=stdout, stderr=stderr, behavior=behavior, recorder=self
        )

        def _track_exit() -> None:
            proc._done.wait()
            with self._lock:
                self.active -= 1

        threading.Thread(target=_track_exit, daemon=True).start()
        self.processes.append(proc)
        return proc


class FakeUploader:
    """Duck-typed upload client; results are status codes, exceptions, or
    default 200."""

    def __init__(self, *results):
        self.calls: list[dict] = []
        self.results = deque(results)

    def upload(self, *, endpoint, params, content, timeout):
        self.calls.append(
            {
                "endpoint": endpoint,
                "params": dict(params),
                "content": bytes(content),
                "timeout": timeout,
            }
        )
        result = self.results.popleft() if self.results else 200
        if isinstance(result, Exception):
            raise result
        return result


def make_settings(**overrides) -> Settings:
    defaults = dict(
        profiling_enabled=True,
        profiling_pyroscope_endpoint="http://pyroscope:4040",
        profiling_interval_seconds=10,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_sampler(factory, uploader, *, interval=1, **kwargs):
    defaults = dict(
        instance_id="inst-test-1",
        pyroscope_endpoint="http://pyroscope:4040",
        interval_seconds=interval,
        pyspy_binary="py-spy",
        subprocess_factory=factory,
        uploader=uploader,
        poll_interval=0.01,
    )
    defaults.update(kwargs)
    return ProfilingSampler(**defaults)


def wait_until(predicate, timeout=5.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Fixed command line / target PID
# ---------------------------------------------------------------------------


class TestFixedCommandLine:
    def test_fixed_command_line_and_no_locals(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=3)
        sampler.start()
        assert wait_until(lambda: len(factory.commands) >= 1)
        sampler.stop()
        command = factory.commands[0]
        pid = os.getpid()
        assert command == [
            "py-spy",
            "record",
            "--pid",
            str(pid),
            "--format",
            "raw",
            "--rate",
            "49",
            "--duration",
            "3",
            "--output",
            factory.spawn_records[0]["stdout_path"],
        ]
        assert "--locals" not in command

    def test_targets_only_current_python_pid(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        sampler.start()
        assert wait_until(lambda: bool(factory.commands))
        sampler.stop()
        assert factory.commands[0][3] == str(os.getpid())


# ---------------------------------------------------------------------------
# Serial windows / no backlog
# ---------------------------------------------------------------------------


class TestSerialWindows:
    def test_no_parallel_windows_and_no_backlog(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        started = threading.Event()

        def slow_window(proc: FakeProcess) -> None:
            started.set()
            time.sleep(0.3)
            proc.write_stdout(SAFE_FOLDED.encode())
            proc.finish(0)

        factory.queue(slow_window)
        sampler.start()
        assert started.wait(timeout=5)
        # The next window must not start before the slow one finished:
        # exactly one window exists concurrently (no parallel), and no
        # queue of catch-up windows is built while the first runs.
        assert len(factory.commands) == 1
        sampler.stop()
        assert factory.max_active == 1
        # Windows are attempted strictly one at a time (serial loop); a
        # fresh attempt only happens after the previous window ended.
        assert sampler.windows_attempted == len(factory.commands)


# ---------------------------------------------------------------------------
# Failure backoff / disable / success reset
# ---------------------------------------------------------------------------


class TestFailurePolicy:
    def test_backoff_10_30_then_third_failure_disables(self):
        factory, uploader = FakeFactory(), FakeUploader(500, 500)
        sleeps: list[float] = []
        sampler = make_sampler(
            factory,
            uploader,
            interval=1,
            sleeper=sleeps.append,
        )
        # Windows 1 and 2 produce a valid profile but the ingest returns
        # 500 -> failure; window 3 exits nonzero -> failure.
        produced = lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        failed_exit = lambda p: (p.write_stderr(b"error: boom\n"), p.finish(1))
        factory.queue(produced, produced, failed_exit)
        sampler.start()
        assert wait_until(
            lambda: sampler.disabled and len(factory.commands) >= 3, timeout=10
        )
        time.sleep(0.2)
        sampler.stop()
        assert sampler.disabled
        assert sampler.status == "degraded"
        assert sampler.current_error == "sampler_disabled"
        assert sleeps == [10.0, 30.0]
        # Third failure stopped the sampler: no further windows.
        assert len(factory.commands) == 3

    def test_success_resets_failure_counter(self):
        factory, uploader = FakeFactory(), FakeUploader(500, 500)
        sleeps: list[float] = []
        sampler = make_sampler(
            factory,
            uploader,
            interval=1,
            sleeper=sleeps.append,
        )
        # fail, fail, SUCCESS (resets) -> healthy; the third upload hits
        # the uploader's default 200.
        produced = lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        factory.queue(produced, produced, produced)
        sampler.start()
        assert wait_until(
            lambda: sampler.windows_completed >= 1 and sampler.consecutive_failures == 0,
            timeout=10,
        )
        sampler.stop()
        assert sampler.windows_completed == 1
        assert sampler.consecutive_failures == 0
        assert sampler.status == "healthy"
        assert not sampler.disabled


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class TestWatchdog:
    def test_watchdog_fires_at_window_plus_5s(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)

        def hang(proc: FakeProcess) -> None:
            time.sleep(30)  # far beyond the watchdog

        factory.queue(hang)
        sampler.start()
        started = time.monotonic()
        # Wait until the watchdog failure is recorded (not just terminate
        # observed) to avoid racing the status transition.
        assert wait_until(
            lambda: sampler.current_error == "watchdog_timeout", timeout=15
        )
        elapsed = time.monotonic() - started
        # watchdog = window (1s) + 5s slack
        assert 5.0 <= elapsed <= 9.0, elapsed
        assert factory.processes[0]._terminated
        assert sampler.consecutive_failures == 1
        sampler.stop()


# ---------------------------------------------------------------------------
# Temp files: permissions, location, 16MiB cap during operation
# ---------------------------------------------------------------------------


class TestTempFiles:
    def test_private_temp_file_permissions_0700_0600(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, temp_root=None)
        sampler.start()
        assert wait_until(lambda: bool(factory.spawn_records))
        sampler.stop()
        record = factory.spawn_records[0]
        assert record["stdout_mode"] == 0o600
        assert record["parent_mode"] == 0o700

    def test_no_workspace_temp_files_system_temp_only(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        sampler.start()
        assert wait_until(lambda: bool(factory.spawn_records))
        sampler.stop()
        parent = Path(factory.spawn_records[0]["parent_dir"]).resolve()
        system_temp = Path(tempfile.gettempdir()).resolve()
        assert parent == system_temp or system_temp in parent.parents
        repo_root = Path(__file__).resolve().parents[3]
        assert repo_root not in parent.parents and parent != repo_root

    def test_16mib_cap_enforced_during_operation(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=30)

        killed_during_operation = threading.Event()

        def grower(proc: FakeProcess) -> None:
            chunk = b"x" * (1024 * 1024)
            while not proc._killed:
                proc.write_stdout(chunk)
                time.sleep(0.005)
            killed_during_operation.set()
            proc.finish(-9)

        factory.queue(grower)
        sampler.start()
        started = time.monotonic()
        assert wait_until(
            lambda: killed_during_operation.is_set(), timeout=10
        )
        elapsed = time.monotonic() - started
        sampler.stop()
        # Terminated during sampling (cap ~16MiB at 1MiB/5ms), long before
        # the 30s window + 5s watchdog would have fired.
        assert elapsed < 20.0, elapsed
        assert sampler.current_error == "profile_too_large"
        assert factory.processes[0]._terminated
        assert uploader.calls == []  # never uploaded


# ---------------------------------------------------------------------------
# Stack projection
# ---------------------------------------------------------------------------


class TestStackProjection:
    def test_drops_dynamic_filenames_thread_names_exception_frames(self):
        raw = (
            # safe frames
            "app.main;handle_request (app/main.py:100) 5\n"
            # dynamic-code filenames
            "do_eval (<string>:1) 3\n"
            "bootstrap (<frozen importlib._bootstrap>:219) 1\n"
            # thread-name / exception-sampling segments
            "main;worker (app/w.py:9) 4\n"
            "Thread 0x7f88 (active);worker (app/w.py:9) 2\n"
            "[GIL active];worker (app/w.py:9) 1\n"
        )
        projected = project_folded_stacks(raw, strip_prefixes=("/ws",))
        lines = projected.splitlines()
        # "app.main" is a bare segment (no file:line) -> dropped like a
        # thread name; only frames with a safe file:line survive.
        assert "handle_request (app/main.py:100) 5" in lines
        assert "worker (app/w.py:9) 4" in lines
        assert "worker (app/w.py:9) 2" in lines
        assert "worker (app/w.py:9) 1" in lines
        joined = "\n".join(lines)
        assert "app.main" not in joined
        assert "<string>" not in joined
        assert "<frozen" not in joined
        assert "main;" not in joined
        assert "Thread" not in joined
        assert "GIL" not in joined

    def test_strips_workspace_and_user_prefixes(self):
        raw = "fn (/Users/me/agent/app/main.py:12) 2\n"
        projected = project_folded_stacks(raw, strip_prefixes=("/Users/me/agent",))
        assert "(app/main.py:12) 2" in projected
        assert "/Users/me" not in projected

    def test_all_empty_projection_reports_empty_profile_no_upload(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)

        def only_unsafe(proc: FakeProcess) -> None:
            proc.write_stdout(b"do_eval (<string>:1) 9\nmain;f (<string>:2) 3\n")
            proc.finish(0)

        factory.queue(only_unsafe)
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "empty_profile"
        assert uploader.calls == []

    def test_upload_content_is_projected_bounded_text(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        factory.queue(
            lambda p: (
                p.write_stdout(
                    b"app.main;h (app/main.py:1) 2\ndo_eval (<string>:1) 1\n"
                ),
                p.finish(0),
            )
        )
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        content = uploader.calls[0]["content"].decode()
        assert "h (app/main.py:1) 2" in content
        assert "app.main" not in content
        assert "<string>" not in content


# ---------------------------------------------------------------------------
# Shutdown: terminate -> kill/wait within 2 seconds
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_terminate_then_kill_within_2s(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=30)
        factory.queue(lambda p: time.sleep(60))  # hangs
        sampler.start()
        assert wait_until(lambda: bool(factory.processes))
        proc = factory.processes[0]
        started = time.monotonic()
        sampler.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, elapsed
        assert proc._terminated
        assert proc.killed
        assert not sampler.is_running()

    def test_registers_stop_callback_on_handle(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader)
        registered: list = []

        class FakeHandle:
            def register_stop_callback(self, callback):
                registered.append(callback)
                return True

        assert sampler.register_with_handle(FakeHandle()) is True
        assert registered == [sampler.stop]


# ---------------------------------------------------------------------------
# Multi-worker: one independent sampler per PID
# ---------------------------------------------------------------------------


class TestPerPidSamplers:
    def test_same_pid_returns_same_sampler(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings()
            first = get_or_create_profiler(settings, instance_id="i-1")
            second = get_or_create_profiler(settings, instance_id="i-1")
            assert first is second
        finally:
            profiling._reset_for_tests()

    def test_forked_child_pid_gets_independent_sampler(self):
        profiling._reset_for_tests()
        try:
            settings = make_settings()
            parent = get_or_create_profiler(settings, instance_id="i-1")
            original_getpid = os.getpid
            os.getpid = lambda: 987654
            try:
                child = get_or_create_profiler(settings, instance_id="i-2")
            finally:
                os.getpid = original_getpid
            assert child is not parent
            assert child.instance_id == "i-2"
            assert child.target_pid == 987654
        finally:
            profiling._reset_for_tests()


# ---------------------------------------------------------------------------
# Upload contract (plan item 17)
# ---------------------------------------------------------------------------


class TestUploadContract:
    def _run_one_window(self, factory, uploader, **kwargs):
        sampler = make_sampler(factory, uploader, interval=2, **kwargs)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )
        sampler.start()
        assert wait_until(lambda: bool(uploader.calls), timeout=10)
        sampler.stop()
        return sampler

    def test_upload_3s_timeout_and_spike_verified_params(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        call = uploader.calls[0]
        assert call["timeout"] == UPLOAD_TIMEOUT_SECONDS == 3.0
        params = call["params"]
        assert params["name"] == "n-agent"
        assert params["format"] == "folded"
        assert params["units"] == "samples"
        assert params["aggregationType"] == "sum"
        assert params["sampleRate"] == 100
        assert params["until"] - params["from"] == 2  # window == interval
        assert call["endpoint"] == "http://pyroscope:4040/ingest"

    def test_http_non_success_counts_as_failure_and_cleans_up(self):
        factory, uploader = FakeFactory(), FakeUploader(503)
        sampler = self._run_one_window(factory, uploader)
        assert sampler.consecutive_failures == 1
        assert sampler.current_error == "upload_failed"
        for record in factory.spawn_records:
            assert not os.path.exists(record["stdout_path"])
            assert not os.path.exists(record["parent_dir"])

    def test_transport_exception_counts_as_failure(self):
        factory, uploader = FakeFactory(), FakeUploader(
            RuntimeError("connection refused")
        )
        sampler = self._run_one_window(factory, uploader)
        assert sampler.consecutive_failures == 1
        assert sampler.current_error == "upload_failed"
        assert not os.path.exists(factory.spawn_records[0]["parent_dir"])

    def test_nonzero_exit_counts_as_failure(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        factory.queue(lambda p: (p.write_stderr(b"error: no space left\n"), p.finish(1)))
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "subprocess_failed"
        assert uploader.calls == []
        assert not os.path.exists(factory.spawn_records[0]["parent_dir"])

    def test_attach_failure_classified_from_bounded_stderr(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        secret = b"SECRET-STDERR-MARKER failed to attach: Operation not permitted\n"
        factory.queue(lambda p: (p.write_stderr(secret + b"x" * 1024 * 1024), p.finish(1)))
        sampler.start()
        assert wait_until(lambda: sampler.windows_attempted >= 1, timeout=10)
        time.sleep(0.2)
        sampler.stop()
        assert sampler.current_error == "attach_failed"
        # stderr stays bounded and is never projected raw into status
        snapshot = sampler.status_snapshot()
        assert "SECRET-STDERR-MARKER" not in str(snapshot)
        assert "x" * 1000 not in str(snapshot)

    def test_success_and_failure_both_clean_up_temp_files(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        for record in factory.spawn_records:
            assert not os.path.exists(record["stdout_path"])
            assert not os.path.exists(record["parent_dir"])

    def test_at_most_one_window_in_flight(self):
        factory, uploader = FakeFactory(), FakeUploader()
        self._run_one_window(factory, uploader)
        assert factory.max_active == 1


# ---------------------------------------------------------------------------
# Missing py-spy binary -> degraded signal, never an app failure
# ---------------------------------------------------------------------------


class TestMissingBinary:
    def test_missing_pyspy_reports_degraded_without_spawning(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, pyspy_binary=None)
        assert sampler.start() is False
        assert sampler.status == "degraded"
        assert sampler.current_error == "pyspy_missing"
        assert factory.commands == []
        sampler.stop()

    def test_missing_binary_is_not_an_app_failure(self):
        # start() must return a plain False (degraded signal), never raise.
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, pyspy_binary="")
        try:
            result = sampler.start()
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"missing py-spy raised: {exc!r}")
        assert result is False


# ---------------------------------------------------------------------------
# Robustness: unexpected failures degrade the signal, never kill the
# worker silently and never raise into the application
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_factory_raising_reports_attach_failed(self):
        def broken_factory(command, *, stdout, stderr):
            raise OSError("spawn failed")

        sampler = make_sampler(broken_factory, FakeUploader(), interval=1)
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "attach_failed", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_mkdtemp_raising_reports_temp_failed(self, monkeypatch):
        sampler = make_sampler(FakeFactory(), FakeUploader(), interval=1)

        def broken_mkdtemp(*args, **kwargs):
            raise OSError("no usable temp dir")

        monkeypatch.setattr(profiling.tempfile, "mkdtemp", broken_mkdtemp)
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "temp_failed", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_fdopen_failure_reports_temp_failed_and_closes_raw_fd(self, monkeypatch):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        real_open = os.open
        opened: list[int] = []

        def fake_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            opened.append(fd)
            if len(opened) == 2:
                # Second file (stderr) cannot be opened.
                os.close(fd)
                raise OSError("fd exhausted")
            return fd

        monkeypatch.setattr(profiling.os, "open", fake_open)
        sampler.start()
        assert wait_until(lambda: sampler.current_error == "temp_failed", timeout=10)
        sampler.stop()
        # No subprocess was spawned and the already-opened raw fd was
        # closed (no fd leak).
        assert factory.commands == []
        with pytest.raises(OSError):
            os.fstat(opened[0])

    def test_projection_raising_reports_projection_failed(self, monkeypatch):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=1)
        factory.queue(
            lambda p: (p.write_stdout(SAFE_FOLDED.encode()), p.finish(0))
        )

        def broken_project(*args, **kwargs):
            raise ValueError("projection exploded")

        monkeypatch.setattr(profiling, "project_folded_stacks", broken_project)
        sampler.start()
        assert wait_until(
            lambda: sampler.current_error == "projection_failed", timeout=10
        )
        sampler.stop()
        # Projection failure never uploads (no raw-file fallback).
        assert uploader.calls == []

    def test_unexpected_window_exception_reports_sampler_error(self, monkeypatch):
        real_clock = time.monotonic
        calls = {"n": 0}

        def flaky_clock():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("clock exploded")
            return real_clock()

        sampler = make_sampler(
            FakeFactory(), FakeUploader(), interval=1, clock=flaky_clock
        )
        sampler.start()
        # The top-level guard degrades the signal instead of the worker
        # thread dying silently while status stays healthy.
        assert wait_until(lambda: sampler.current_error == "sampler_error", timeout=10)
        sampler.stop()
        assert sampler.status == "degraded"
        assert not sampler.disabled

    def test_thread_start_failure_reports_degraded_never_raises(self, monkeypatch):
        sampler = make_sampler(FakeFactory(), FakeUploader())

        def broken_start(inner):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", broken_start)
        assert sampler.start() is False
        assert sampler.current_error == "sampler_error"
        assert sampler.status == "degraded"
        assert not sampler.is_running()
        sampler.stop()

    def test_stop_during_window_no_upload_no_spurious_failure(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=30)

        def output_then_hang(proc: FakeProcess) -> None:
            proc.write_stdout(SAFE_FOLDED.encode())
            proc._done.wait()  # ignores terminate; stop() kills it

        factory.queue(output_then_hang)
        sampler.start()
        assert wait_until(lambda: bool(factory.commands))
        time.sleep(0.05)
        assert sampler.stop() is True
        # A stopped window is neither uploaded nor misclassified as a
        # failure (the child may exit 0/nonzero on SIGTERM).
        assert uploader.calls == []
        assert sampler.current_error is None
        assert sampler.consecutive_failures == 0

    def test_start_after_stop_returns_false(self):
        factory, uploader = FakeFactory(), FakeUploader()
        sampler = make_sampler(factory, uploader, interval=30)
        factory.queue(lambda p: time.sleep(60))
        assert sampler.start() is True
        sampler.stop()
        # Restart after stop is unsupported; a fresh sampler must be
        # created via get_or_create_profiler instead.
        assert sampler.start() is False
        assert not sampler.is_running()

    def test_register_with_handle_returns_false_when_registration_fails(self):
        class DecliningHandle:
            def register_stop_callback(self, callback):
                return False

        sampler = make_sampler(FakeFactory(), FakeUploader())
        assert sampler.register_with_handle(DecliningHandle()) is False
