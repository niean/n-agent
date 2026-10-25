"""py-spy profile sampling and Pyroscope ingest (Infrastructure).

Single-worker, single-subprocess serial window loop per Python PID
(spec-261024-observability, Components 5; plan-261024 item 17):

- Target is the application's own Python PID only; never a privileged or
  host PID space. The py-spy command line is fixed:
  ``py-spy record --pid <self> --format raw --rate 49 --duration <window>
  --output <tmp>`` and ``--locals`` is never passed (no variable values).
- Window length equals ``profiling_interval_seconds``; a new window starts
  only after the previous one finished uploading. No parallel windows, no
  backlog catch-up; short upload gaps are allowed.
- Private temp files live under the system temp dir (never the workspace):
  directory 0700, files 0600. The 16 MiB size cap is enforced DURING
  sampling (the subprocess is terminated when exceeded), not after.
- Raw output is parsed as folded stacks and projected onto safe frames
  (function name + relative source position). Dynamic-code filenames,
  thread names and exception-sampling segments are dropped; workspace/user
  home prefixes are stripped. If every frame is dropped the window reports
  a safe ``empty_profile`` and the original file is never uploaded.
- Upload goes to Pyroscope ``/ingest`` via httpx with the spike-verified
  contract: ``name=<bare service name>&format=folded&units=samples&
  aggregationType=sum&sampleRate=100&from=<unix>&until=<unix>``, 3 second
  timeout. The upload URL is registered in the same httpx self-traffic
  exclusion mechanism used by instrumentors.py, so automatic
  instrumentation never spans it.
- Failures (attach failure, nonzero exit, size overrun, watchdog timeout,
  non-success ingest response) drop the window and back off 10s, then
  30s; the third consecutive failure disables this process's sampler and
  reports degraded. A successful window resets the counter. Temp files
  are cleaned up on success and failure alike; at most one window is in
  flight; stderr is read bounded and never projected raw into status.
- Watchdog: window + 5 seconds. Shutdown: terminate -> kill/wait within
  the 2 second budget (included in the observability handle's overall 5s
  budget via ``register_stop_callback``).
- A missing py-spy binary is a degraded profiling signal, never an app
  failure.

Only imports ``app.config`` and ``app.infrastructure.observability``
modules plus stdlib/httpx -- never interfaces/domain/business modules.
"""
from __future__ import annotations

import os
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.config import Settings
from app.infrastructure.observability.setup import SERVICE_NAME

# Spike-verified ingest contract (T9 S1, 2026-10-24, py-spy 0.4.2 /
# grafana/pyroscope:1.21.1): POST /ingest with folded stacks, samples
# units, sum aggregation; queries use the {service_name="..."} selector.
INGEST_FORMAT = "folded"
INGEST_UNITS = "samples"
INGEST_AGGREGATION_TYPE = "sum"
INGEST_SAMPLE_RATE = 100

UPLOAD_TIMEOUT_SECONDS = 3.0
WATCHDOG_SLACK_SECONDS = 5.0
STOP_BUDGET_SECONDS = 2.0
MAX_PROFILE_BYTES = 16 * 1024 * 1024  # 16 MiB, enforced during sampling
MAX_STDERR_BYTES = 4096  # bounded stderr read; never projected raw
MAX_FILENAME_CHARS = 256
PYSPY_RATE = 49
TERMINATE_GRACE_SECONDS = 0.5

# Consecutive-failure backoff: first -> 10s, second -> 30s, third -> stop.
BACKOFF_SCHEDULE: tuple = (10.0, 30.0)
MAX_CONSECUTIVE_FAILURES = 3

# Special window outcome: sampling aborted because shutdown was requested;
# neither a success nor a failure (the sampler exits either way).
_SHUTDOWN_ABORTED = "__shutdown_aborted__"

_AUTO_RESOLVE_BINARY = object()

_FRAME_RE = re.compile(r"^(?P<func>.+) \((?P<file>[^()]*):(?P<line>\d+)\)$")
# Dynamic-code filenames (<string>, <frozen ...>, eval/exec targets) can
# carry arbitrary text (potential secrets) -> never projected.
_DYNAMIC_FILENAME_RE = re.compile(r"<[^>]*>")


# ---------------------------------------------------------------------------
# Safe folded-stack projection
# ---------------------------------------------------------------------------


def _strip_prefixes(filename: str, prefixes: Sequence[str]) -> str:
    for prefix in prefixes:
        if not prefix:
            continue
        normalized = str(prefix).rstrip("/") + "/"
        if filename.startswith(normalized):
            stripped = filename[len(normalized):]
            if stripped:
                return stripped
    return filename


def project_folded_stacks(
    text: str,
    *,
    strip_prefixes: Sequence[str] = (),
    max_line_chars: int = 2048,
    max_output_bytes: int = MAX_PROFILE_BYTES,
) -> str:
    """Project raw py-spy folded stacks onto safe frames only.

    A segment survives only when it matches ``func (path:line)`` with a
    non-dynamic filename; everything else (thread names, GIL/exception
    sampling markers, dynamic-code frames) is dropped, matching the spec
    rule "discard frames/lines that cannot be projected" rather than a
    prefix-only redaction. The result is bounded in line length and total
    bytes. Returns folded text ending in a newline (empty when no safe
    frame survived)."""
    out: List[str] = []
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, count = line.rpartition(" ")
        if not sep or not count.isdigit():
            continue
        kept: List[str] = []
        for segment in head.split(";"):
            segment = segment.strip()
            if not segment:
                continue
            match = _FRAME_RE.match(segment)
            if match is None:
                continue
            filename = match.group("file").strip()
            func = match.group("func").strip()
            if not func or not filename:
                continue
            if _DYNAMIC_FILENAME_RE.search(filename):
                continue
            if any(ord(char) < 32 or char == ";" for char in filename + func):
                continue
            filename = _strip_prefixes(filename, strip_prefixes)[
                :MAX_FILENAME_CHARS
            ]
            if not filename:
                continue
            kept.append(f"{func} ({filename}:{match.group('line')})")
        if not kept:
            continue
        out_line = ";".join(kept) + " " + count
        if len(out_line) > max_line_chars:
            continue
        # Single UTF-8 encode per line (hot path over up to 16 MiB).
        encoded = (out_line + "\n").encode("utf-8")
        if total + len(encoded) > max_output_bytes:
            break
        total += len(encoded)
        out.append(out_line)
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# Default subprocess / upload adapters
# ---------------------------------------------------------------------------


def _popen_factory(
    command: List[str], *, stdout: Any, stderr: Any
) -> subprocess.Popen:
    return subprocess.Popen(command, stdout=stdout, stderr=stderr)


class _HttpxUploader:
    """Pyroscope /ingest uploader reusing the instrumentors.py httpx
    self-traffic exclusion (a single mechanism; no second one)."""

    def __init__(self, pyroscope_endpoint: str):
        self._endpoint = pyroscope_endpoint
        # Keep the ingest URL excluded from httpx auto-spans even if this
        # uploader is constructed before instrument_app ran with these
        # settings (idempotent; preserves existing patterns). A real
        # Settings instance (not a fabricated namespace) is passed so the
        # helper reads the same attribute shape it always does; the import
        # is lazy so importing profiling never loads the instrumentation
        # modules in the disabled state.
        try:
            shim = Settings(
                otel_exporter_otlp_endpoint="",
                profiling_pyroscope_endpoint=pyroscope_endpoint,
            )
        except Exception:
            return
        try:
            from app.infrastructure.observability.instrumentors import (
                _set_httpx_self_traffic_exclusion,
            )

            _set_httpx_self_traffic_exclusion(shim)
        except Exception:
            pass

    def upload(
        self,
        *,
        endpoint: str,
        params: Dict[str, Any],
        content: bytes,
        timeout: float,
    ) -> int:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.post(endpoint, params=params, content=content)
            return response.status_code


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ProfilingSampler:
    """Serial py-spy window loop with Pyroscope ingest for one PID.

    All side effects (subprocess spawning, uploading, sleeping, clocks)
    are injectable so tests run with fakes and no real py-spy traffic."""

    def __init__(
        self,
        *,
        instance_id: str,
        pyroscope_endpoint: str,
        interval_seconds: int,
        pyspy_binary: Any = _AUTO_RESOLVE_BINARY,
        target_pid: Optional[int] = None,
        service_name: str = SERVICE_NAME,
        uploader: Any = None,
        subprocess_factory: Any = None,
        clock: Callable[[], float] = time.monotonic,
        epoch_clock: Callable[[], float] = time.time,
        sleeper: Optional[Callable[[float], None]] = None,
        poll_interval: float = 0.5,
        temp_root: Optional[str] = None,
        strip_prefixes: Optional[Sequence[str]] = None,
        on_status: Optional[Callable[[str, Optional[str]], None]] = None,
    ):
        self.instance_id = instance_id
        self.pyroscope_endpoint = pyroscope_endpoint
        self.interval_seconds = int(interval_seconds)
        self.service_name = service_name
        self.target_pid = target_pid if target_pid is not None else os.getpid()
        # Resolved once (not per window): workspace/user-home prefixes are
        # stripped from projected stack filenames.
        self._strip_prefixes = (
            tuple(strip_prefixes)
            if strip_prefixes is not None
            else (os.getcwd(), str(Path.home()))
        )
        self._pyspy_requested = pyspy_binary
        self._uploader = uploader
        self._factory = (
            subprocess_factory if subprocess_factory is not None else _popen_factory
        )
        self._clock = clock
        self._epoch_clock = epoch_clock
        self._sleeper = sleeper if sleeper is not None else self._default_sleeper
        self._poll_interval = poll_interval
        self._temp_root = temp_root
        self._on_status = on_status

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        self._proc_lock = threading.Lock()
        self._proc: Any = None
        self._started = False

        self._lock = threading.Lock()
        self._current_error: Optional[str] = None
        self._consecutive_failures = 0
        self._total_failures = 0
        self._windows_attempted = 0
        self._windows_completed = 0
        self._last_success: Optional[str] = None
        self._last_failure: Optional[str] = None
        self._disabled = False
        self._diagnostic = ""

    # -- state --------------------------------------------------------------

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def current_error(self) -> Optional[str]:
        return self._current_error

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def windows_attempted(self) -> int:
        return self._windows_attempted

    @property
    def windows_completed(self) -> int:
        return self._windows_completed

    @property
    def status(self) -> str:
        if self._current_error or self._disabled:
            return "degraded"
        return "healthy"

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status_snapshot(self) -> Dict[str, Any]:
        """Pure-JSON, bounded status (safe codes only, never raw stderr)."""
        with self._lock:
            return {
                "enabled": True,
                "status": self.status,
                "disabled": self._disabled,
                "instance_id": self.instance_id,
                "target_pid": self.target_pid,
                "service_name": self.service_name,
                "interval_seconds": self.interval_seconds,
                "current_error": self._current_error,
                "consecutive_failures": self._consecutive_failures,
                "total_failures": self._total_failures,
                "windows_attempted": self._windows_attempted,
                "windows_completed": self._windows_completed,
                "last_success": self._last_success,
                "last_failure": self._last_failure,
            }

    # -- lifecycle ------------------------------------------------------------

    def _resolve_binary(self) -> Optional[str]:
        if self._pyspy_requested is _AUTO_RESOLVE_BINARY:
            try:
                return shutil.which("py-spy")
            except Exception:
                return None
        return self._pyspy_requested or None

    def start(self) -> bool:
        """Start the serial sampling loop.

        Returns False (and reports a degraded profiling signal, never an
        application failure) when py-spy is unavailable, when the worker
        thread cannot be started, or when this sampler was already
        stopped: restart after ``stop()`` is unsupported -- create a fresh
        sampler via ``get_or_create_profiler`` instead."""
        if self._stopped:
            return False
        if self._started:
            return True
        if self._resolve_binary() is None:
            self._set_error("pyspy_missing")
            return False
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, name="nagent-profiling-sampler", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            # e.g. RuntimeError("can't start new thread"): degraded signal,
            # never raised into the application.
            self._started = False
            self._thread = None
            self._set_error("sampler_error")
            return False
        return True

    def register_with_handle(self, handle: Any) -> bool:
        """Register profiler recovery (terminate -> kill/wait within the
        2s budget) as a stop callback on the observability handle; the 2s
        is included in the handle's overall shutdown budget."""
        return bool(handle.register_stop_callback(self.stop))

    def _safe_now(self) -> float:
        """Injected clock with a monotonic fallback: subprocess recovery in
        ``stop()`` must never fail because the clock misbehaves."""
        try:
            return self._clock()
        except Exception:
            return time.monotonic()

    def stop(self, timeout: float = STOP_BUDGET_SECONDS) -> bool:
        """Stop sampling and recover the subprocess within ``timeout``
        (default 2s): terminate -> kill -> wait, then join the loop."""
        self._stop_event.set()
        self._stopped = True
        deadline = self._safe_now() + timeout
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(
                    timeout=min(
                        TERMINATE_GRACE_SECONDS,
                        max(0.0, deadline - self._safe_now()),
                    )
                )
            except Exception:
                pass
            if getattr(proc, "poll", lambda: None)() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=max(0.0, deadline - self._safe_now()))
                except Exception:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, deadline - self._safe_now()))
        return not self.is_running()

    def _default_sleeper(self, seconds: float) -> None:
        # Interruptible backoff: shutdown cuts the wait short.
        self._stop_event.wait(seconds)

    # -- window loop ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                code = self._run_window()
            except Exception:
                # Top-level guard: an unexpected failure inside the window
                # (e.g. temp-dir creation raising) must degrade the signal
                # instead of silently killing the worker thread while the
                # status stays healthy.
                code = "sampler_error"
            if code == "":
                self._record_success()
            elif code == _SHUTDOWN_ABORTED:
                break
            else:
                self._record_failure(code)
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self._disable()
                    break
                self._sleeper(
                    BACKOFF_SCHEDULE[
                        min(self._consecutive_failures, len(BACKOFF_SCHEDULE)) - 1
                    ]
                )

    def _build_command(self, output_path: str) -> List[str]:
        return [
            "py-spy",
            "record",
            "--pid",
            str(self.target_pid),
            "--format",
            "raw",
            "--rate",
            str(PYSPY_RATE),
            "--duration",
            str(self.interval_seconds),
            "--output",
            output_path,
        ]

    def _run_window(self) -> str:
        """Run one sample window; returns "" on success, a safe error code
        on failure, or _SHUTDOWN_ABORTED when shutdown interrupted it."""
        self._diagnostic = ""
        try:
            tmp_dir = tempfile.mkdtemp(
                prefix="nagent-profiling-", dir=self._temp_root
            )
            # mkdtemp is already 0700; chmod keeps it deterministic under
            # any umask. Files below are created 0600 by os.open.
            os.chmod(tmp_dir, 0o700)
        except Exception as exc:
            self._diagnostic = str(exc)
            return "temp_failed"
        raw_path = os.path.join(tmp_dir, "raw.folded")
        err_path = os.path.join(tmp_dir, "pyspy.stderr")
        # Per-resource open/close: a failure on the second file must not
        # leak the first one's fd.
        raw_f: Optional[Any] = None
        err_f: Optional[Any] = None
        try:
            raw_fd = os.open(raw_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                raw_f = os.fdopen(raw_fd, "w+b")
            except Exception:
                os.close(raw_fd)
                raise
            err_fd = os.open(err_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                err_f = os.fdopen(err_fd, "w+b")
            except Exception:
                os.close(err_fd)
                raise
        except Exception:
            for handle in (raw_f, err_f):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            self._cleanup(tmp_dir)
            return "temp_failed"
        try:
            command = self._build_command(raw_path)
            try:
                with self._proc_lock:
                    proc = self._factory(command, stdout=raw_f, stderr=err_f)
                    self._proc = proc
            except Exception as exc:
                self._diagnostic = str(exc)
                return "attach_failed"
            self._windows_attempted += 1
            outcome = self._wait_for_process(proc, raw_f)
            if outcome == _SHUTDOWN_ABORTED:
                return _SHUTDOWN_ABORTED
            if outcome is not None:
                return outcome  # profile_too_large / watchdog_timeout
            return self._collect_and_upload(proc, raw_f, err_f)
        finally:
            with self._proc_lock:
                self._proc = None
            for handle in (raw_f, err_f):
                try:
                    handle.close()
                except Exception:
                    pass
            # Success and failure both clean up; at most one window's temp
            # files exist at any time.
            self._cleanup(tmp_dir)

    def _wait_for_process(self, proc: Any, raw_f: Any) -> Optional[str]:
        """Poll the subprocess until exit; enforces the 16 MiB cap DURING
        sampling and the window+5s watchdog. Returns an error code, or
        None when the process exited on its own."""
        start = self._clock()
        deadline = start + self.interval_seconds + WATCHDOG_SLACK_SECONDS
        while True:
            if self._stop_event.is_set():
                self._terminate_and_kill(proc)
                return _SHUTDOWN_ABORTED
            rc = proc.poll()
            if rc is not None:
                return None
            try:
                size = os.fstat(raw_f.fileno()).st_size
            except Exception:
                size = 0
            if size > MAX_PROFILE_BYTES:
                self._terminate_and_kill(proc)
                return "profile_too_large"
            if self._clock() >= deadline:
                self._terminate_and_kill(proc)
                return "watchdog_timeout"
            # Poll waits are interruptible by shutdown and never recorded
            # as backoff sleeps.
            self._stop_event.wait(self._poll_interval)

    @staticmethod
    def _terminate_and_kill(proc: Any) -> None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except Exception:
            pass

    @staticmethod
    def _read_bounded(fileobj: Any, limit: int = MAX_STDERR_BYTES) -> bytes:
        try:
            fileobj.seek(0)
            return fileobj.read(limit)
        except Exception:
            return b""

    @staticmethod
    def _cleanup(tmp_dir: str) -> None:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def _classify_exit(self, stderr_head: bytes, rc: int) -> str:
        lowered = stderr_head.lower()
        attach_markers = (
            b"failed to attach",
            b"could not attach",
            b"permission",
            b"not permitted",
            b"ptrace",
        )
        if any(marker in lowered for marker in attach_markers):
            return "attach_failed"
        return "subprocess_failed"

    def _collect_and_upload(
        self, proc: Any, raw_f: Any, err_f: Any
    ) -> str:
        # TOCTOU re-check: a stop-terminated window must never be
        # misclassified as a failure nor upload a partial profile (the
        # child may exit 0 on SIGTERM).
        if self._stop_event.is_set():
            return _SHUTDOWN_ABORTED
        rc = proc.poll()
        if rc != 0:
            stderr = self._read_bounded(err_f)
            self._diagnostic = stderr.decode("utf-8", errors="replace")
            return self._classify_exit(stderr, rc)
        # Projection failure never falls back to uploading the raw file.
        try:
            raw_f.seek(0)
            raw_text = raw_f.read(MAX_PROFILE_BYTES + 1).decode(
                "utf-8", errors="replace"
            )
            projected = project_folded_stacks(
                raw_text,
                strip_prefixes=self._strip_prefixes,
            )
        except Exception:
            return "projection_failed"
        if not projected.strip():
            return "empty_profile"
        try:
            self._upload(projected.encode("utf-8"))
        except Exception as exc:
            self._diagnostic = str(exc)
            return "upload_failed"
        return ""

    def _upload(self, content: bytes) -> None:
        endpoint = self.pyroscope_endpoint.rstrip("/") + "/ingest"
        window_start = int(self._epoch_clock())
        params = {
            "name": self.service_name,
            "format": INGEST_FORMAT,
            "units": INGEST_UNITS,
            "aggregationType": INGEST_AGGREGATION_TYPE,
            "sampleRate": INGEST_SAMPLE_RATE,
            "from": window_start,
            "until": window_start + self.interval_seconds,
        }
        uploader = self._uploader
        if uploader is None:
            uploader = _HttpxUploader(self.pyroscope_endpoint)
            self._uploader = uploader
        status = uploader.upload(
            endpoint=endpoint,
            params=params,
            content=content,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"pyroscope ingest returned HTTP {int(status)}")

    # -- status transitions ---------------------------------------------------

    def _set_error(self, code: str) -> None:
        self._current_error = code
        self._log_failure(code)
        if self._on_status is not None:
            try:
                self._on_status("degraded", code)
            except Exception:
                pass

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._windows_completed += 1
            self._last_success = _utc_now_iso()
            self._current_error = None
        if self._on_status is not None:
            try:
                self._on_status("healthy", None)
            except Exception:
                pass

    def _record_failure(self, code: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure = _utc_now_iso()
            self._current_error = code
        self._log_failure(code)
        if self._on_status is not None:
            try:
                self._on_status("degraded", code)
            except Exception:
                pass

    def _disable(self) -> None:
        with self._lock:
            self._disabled = True
            self._current_error = "sampler_disabled"
        if self._on_status is not None:
            try:
                self._on_status("degraded", "sampler_disabled")
            except Exception:
                pass

    def _log_failure(self, code):
        try:
            logging.getLogger(__name__).warning("profile sampling failed code=%s detail=%s", code, self._diagnostic)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-PID sampler registry (multi-worker: one sampler per Python PID)
# ---------------------------------------------------------------------------

_SAMPLERS: Dict[int, ProfilingSampler] = {}
_SAMPLERS_LOCK = threading.Lock()

# Fork invariant: the registry is keyed by os.getpid(), so a forked child
# (different PID) never inherits or reuses its parent's sampler.


def get_or_create_profiler(
    settings: Settings, *, instance_id: Optional[str] = None, **overrides: Any
) -> Optional[ProfilingSampler]:
    """Return this PID's sampler, creating it on first call.

    Each worker process (each Python PID after fork) gets its own
    independent sampler distinguished by ``instance_id``; within one PID
    at most one sampler exists. Returns None when profiling is disabled."""
    pid = os.getpid()
    with _SAMPLERS_LOCK:
        existing = _SAMPLERS.get(pid)
        if existing is not None:
            return existing
        if not settings.profiling_enabled:
            return None
        sampler = ProfilingSampler(
            instance_id=instance_id or uuid.uuid4().hex,
            pyroscope_endpoint=settings.profiling_pyroscope_endpoint,
            interval_seconds=settings.profiling_interval_seconds,
            **overrides,
        )
        _SAMPLERS[pid] = sampler
        return sampler


def _reset_for_tests() -> None:
    with _SAMPLERS_LOCK:
        _SAMPLERS.clear()
