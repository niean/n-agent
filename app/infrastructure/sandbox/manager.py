"""SandboxManager — session-level sandbox reuse + per-call staging + idle reaper.

Concurrency model:
- One asyncio.Lock per session_id; acquire_session_lock returns it
- `_releasing` set marks sessions being released; new execution raises
- `release` waits for the lock (up to release_wait_timeout_seconds); on
  success it holds the lock for the entire cleanup, then releases ONLY the
  lock it acquired (tracked via `acquired` flag). Timeout => skip this round.
- `force_release` is the Dashboard path: timeout => docker kill -f, still
  without releasing a lock it never acquired (avoid corrupting lock state).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.sandbox import ActiveSandboxInfo, ReleasedSandboxInfo, Sandbox
from app.infrastructure.sandbox.docker import DockerSandbox
from app.infrastructure.sandbox.local import LocalSandbox


logger = logging.getLogger(__name__)

_SAFE_SESSION_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_session_segment(session_id: str) -> str:
    """Normalize arbitrary session_id to a path-safe segment.

    ChatCompletionService accepts session_id from headers/metadata; it may
    contain ../, /, :, spaces — all of which can escape scratch paths or
    break Docker container names. Normalize to [a-zA-Z0-9_-], prefix sess-,
    truncate to 24 chars.

    Length cap is 24 (not 64) so the resulting UDS socket path stays under
    the kernel's UNIX_PATH_MAX=108 bytes once scratch_root + call-uuid +
    'rpc.sock' are appended. Worst case: scratch_root(50) + '/sess-'(6) +
    24 + '/call-'(6) + 8 + '/rpc.sock'(9) = 103 bytes.
    """
    cleaned = _SAFE_SESSION_ID_RE.sub("-", session_id).strip("-")[:24]
    if not cleaned:
        cleaned = "anon"
    return f"sess-{cleaned}"


class SandboxManager:
    def __init__(
        self,
        sandbox_type: str,
        workspace_root: Path,
        idle_seconds: int,
        settings,
        callback_registry,
        scratch_root: Path,
        release_wait_timeout_seconds: int = 30,
        host_workspace_root: Path | None = None,
        host_scratch_root: Path | None = None,
        released_registry=None,
    ) -> None:
        if sandbox_type not in ("local", "docker"):
            raise ValueError(
                f"invalid sandbox_type: {sandbox_type!r} (must be 'local' or 'docker')"
            )
        self.sandbox_type = sandbox_type
        self.workspace_root = workspace_root
        # scratch is independent of workspace: workspace is read-only working area,
        # scratch is writable execution area. Docker mounts them separately
        # (workspace:ro + scratch:rw). Never nest scratch inside workspace.
        self.scratch_root = scratch_root
        self.idle_seconds = idle_seconds
        if self.idle_seconds <= 0:
            raise ValueError("idle_seconds must be > 0")
        self.settings = settings
        self.callback_registry = callback_registry
        self.release_wait_timeout_seconds = release_wait_timeout_seconds
        self.host_workspace_root = host_workspace_root or workspace_root
        self.host_scratch_root = host_scratch_root or scratch_root
        self.released_registry = released_registry
        self._sandboxes: dict[str, Sandbox] = {}
        self._scratch_roots: dict[str, Path] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._releasing: set[str] = set()
        self._created_at: dict[str, datetime] = {}
        self._last_used: dict[str, datetime] = {}
        self._reaper_task: asyncio.Task | None = None
        self._reaper_stop: asyncio.Event | None = None
        # Released sandbox history (most-recent-first). Bounded ring buffer;
        # older entries dropped once cap reached.
        self._released: list[ReleasedSandboxInfo] = []
        self._released_cap = 100
        self.scratch_root.mkdir(parents=True, exist_ok=True)

    def acquire_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id in self._releasing:
            raise RuntimeError(f"session {session_id} is being released")
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def default_workdir(self, session_id: str) -> str:
        """Return the default workdir for the terminal tool, before sandbox creation.

        Reuses `_safe_session_segment` so the path is path-safe and bounded.
        Callable BEFORE `get_or_create()` — terminal tool may resolve a workdir
        first, then acquire the session lock and call `get_or_create()`.

        - Docker: returns container path `/scratch/<safe>`. Also ensures the
          host-side `scratch_root / safe` exists so the bind-mounted scratch
          subdir is present (avoids Docker creating it as root-owned).
        - Local: returns host absolute path `str(scratch_root / safe)` and
          creates the directory.
        """
        safe = _safe_session_segment(session_id)
        host_session_scratch = self.scratch_root / safe
        host_session_scratch.mkdir(parents=True, exist_ok=True)
        if self.sandbox_type == "docker":
            return f"/scratch/{safe}"
        return str(host_session_scratch)

    async def get_or_create(self, session_id: str) -> Sandbox:
        if session_id in self._releasing:
            raise RuntimeError(f"session {session_id} is being released")
        if session_id in self._sandboxes:
            self._last_used[session_id] = datetime.now(timezone.utc)
            return self._sandboxes[session_id]
        safe = _safe_session_segment(session_id)
        session_scratch = self.scratch_root / safe
        session_scratch.mkdir(parents=True, exist_ok=True)
        if self.sandbox_type == "docker":
            sandbox = DockerSandbox(
                registry=self.callback_registry,
                workspace_root=self.workspace_root,
                image=self.settings.sandbox_docker_image,
                cpus=self.settings.sandbox_docker_cpus,
                memory_mb=self.settings.sandbox_docker_memory_mb,
                session_container_name=f"nagent-sandbox-{safe}-{uuid4().hex[:8]}",
                network=self.settings.sandbox_docker_network,
                host_workspace_root=self.host_workspace_root,
                host_scratch_root=self.host_scratch_root,
                max_stdout_bytes=self.settings.sandbox_max_stdout_bytes,
                max_stderr_bytes=self.settings.sandbox_max_stderr_bytes,
            )
        else:
            sandbox = LocalSandbox(
                registry=self.callback_registry,
                workspace_root=self.workspace_root,
                max_stdout_bytes=self.settings.sandbox_max_stdout_bytes,
                max_stderr_bytes=self.settings.sandbox_max_stderr_bytes,
            )
        self._sandboxes[session_id] = sandbox
        self._scratch_roots[session_id] = session_scratch
        self._created_at[session_id] = datetime.now(timezone.utc)
        self._last_used[session_id] = datetime.now(timezone.utc)
        return sandbox

    def new_call_staging(self, session_id: str) -> Path:
        scratch_root = self._scratch_roots[session_id]
        # 8 hex (32-bit) is enough entropy to avoid collisions within a single
        # session's concurrent calls; full uuid4 would push UDS path over limit.
        staging = scratch_root / f"call-{uuid4().hex[:8]}"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def list_active(self) -> list[ActiveSandboxInfo]:
        now = datetime.now(timezone.utc)
        result = []
        for sid, sandbox in self._sandboxes.items():
            result.append(ActiveSandboxInfo(
                session_id=sid,
                sandbox_type=self.sandbox_type,
                scratch_root=self._scratch_roots[sid],
                created_at=self._created_at[sid],
                last_used_at=self._last_used[sid],
                idle_seconds=int((now - self._last_used[sid]).total_seconds()),
                container_status=getattr(sandbox, "container_status", None),
                sandbox_id=getattr(sandbox, "session_container_name", None),
            ))
        return result

    def list_released(self) -> list[ReleasedSandboxInfo]:
        if self.released_registry is not None:
            try:
                return self.released_registry.list_recent(limit=self._released_cap)
            except Exception:
                logger.warning("list released sandbox history failed", exc_info=True)
        return list(self._released)

    def delete_released(self, entry_id: str) -> bool:
        if not entry_id:
            return False
        if self.released_registry is not None:
            try:
                return self.released_registry.delete(entry_id)
            except Exception:
                logger.warning("delete released sandbox history failed", exc_info=True)
                return False
        for i, info in enumerate(self._released):
            if info.id == entry_id:
                del self._released[i]
                return True
        return False

    def _record_release(self, session_id: str, sandbox, reason: str) -> None:
        created = self._created_at.get(session_id)
        if created is None:
            return
        entry = ReleasedSandboxInfo(
            session_id=session_id,
            sandbox_type=self.sandbox_type,
            sandbox_id=getattr(sandbox, "session_container_name", None) if sandbox is not None else None,
            created_at=created,
            released_at=datetime.now(timezone.utc),
            reason=reason,
        )
        if self.released_registry is not None:
            try:
                self.released_registry.record(entry)
                return
            except Exception:
                logger.warning("record released sandbox history failed", exc_info=True)
        self._released.insert(0, entry)
        if len(self._released) > self._released_cap:
            del self._released[self._released_cap:]

    async def release(self, session_id: str, reason: str = "idle") -> None:
        """Cooperative release — waits for in-flight execution to finish.

        Timeout => skip this round (don't tear down a running sandbox).
        Dashboard "release" button uses `force_release` for forced teardown.
        `reason` is recorded into the released-sandbox history for audit:
        "idle" (reaper), "session" (session deleted).
        """
        self._releasing.add(session_id)
        lock = self._locks.get(session_id)
        acquired = False
        try:
            if lock is not None:
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=self.release_wait_timeout_seconds)
                    acquired = True
                except asyncio.TimeoutError:
                    logger.warning(
                        "sandbox release wait timeout for session %s, skip this round", session_id,
                    )
                    return
            try:
                sandbox = self._sandboxes.pop(session_id, None)
                if sandbox is not None:
                    self._record_release(session_id, sandbox, reason)
                    if hasattr(sandbox, "cleanup_container"):
                        try:
                            await sandbox.cleanup_container()
                        except Exception:
                            logger.warning("cleanup_container failed for %s", session_id, exc_info=True)
                scratch = self._scratch_roots.pop(session_id, None)
                if scratch is not None and scratch.exists():
                    shutil.rmtree(scratch, ignore_errors=True)
                self._created_at.pop(session_id, None)
                self._last_used.pop(session_id, None)
            finally:
                if acquired and lock is not None:
                    lock.release()
                self._locks.pop(session_id, None)
        finally:
            self._releasing.discard(session_id)

    async def force_release(self, session_id: str, reason: str = "manual") -> None:
        """Forced teardown — kills container even if an execution is in flight.

        Used by Dashboard "release" button. Does NOT release a lock it never
        acquired (avoids corrupting lock state for any in-flight execution
        that survives the kill). `reason` is recorded into the released-sandbox
        history: "manual" (dashboard button).
        """
        self._releasing.add(session_id)
        try:
            sandbox = self._sandboxes.pop(session_id, None)
            if sandbox is not None:
                self._record_release(session_id, sandbox, reason)
                if hasattr(sandbox, "cleanup_container"):
                    try:
                        await sandbox.cleanup_container()
                    except Exception:
                        logger.warning("force cleanup_container failed for %s", session_id, exc_info=True)
            scratch = self._scratch_roots.pop(session_id, None)
            if scratch is not None and scratch.exists():
                shutil.rmtree(scratch, ignore_errors=True)
            self._created_at.pop(session_id, None)
            self._last_used.pop(session_id, None)
            self._locks.pop(session_id, None)
        finally:
            self._releasing.discard(session_id)

    def start_reaper(self) -> None:
        if self._reaper_task is not None:
            return
        self._reaper_stop = asyncio.Event()
        self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def cleanup_orphan_containers(self) -> int:
        """Kill + remove leftover sibling sandbox containers from prior runs.

        Called at lifespan startup. When n-agent restarts, its in-memory
        `_sandboxes` dict is empty but docker daemon still holds sibling
        containers created before the restart. Without cleanup these become
        orphans: not reachable via Dashboard, consuming resources, and never
        reclaimed by the reaper (which only knows about `_sandboxes` keys).
        """
        if self.sandbox_type != "docker":
            return 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-a",
                "--filter", "name=nagent-sandbox-",
                "--format", "{{.Names}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception:
            logger.warning("cleanup_orphan_containers: docker ps failed", exc_info=True)
            return 0
        names = [n for n in stdout_b.decode("utf-8", errors="replace").splitlines() if n.strip()]
        if not names:
            return 0
        killed = 0
        for name in names:
            try:
                for args in (["kill", name], ["rm", "-f", name]):
                    p = await asyncio.create_subprocess_exec(
                        "docker", *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(p.communicate(), timeout=10)
                killed += 1
            except Exception:
                logger.warning("cleanup_orphan_containers: failed to remove %s", name, exc_info=True)
        logger.warning("cleanup_orphan_containers: removed %d/%d leftover sandbox containers", killed, len(names))
        return killed

    async def stop_reaper(self) -> None:
        if self._reaper_task is None:
            return
        self._reaper_stop.set() if self._reaper_stop is not None else None
        try:
            await asyncio.wait_for(self._reaper_task, timeout=5)
        except asyncio.TimeoutError:
            self._reaper_task.cancel()
        self._reaper_task = None

    async def _reaper_loop(self) -> None:
        assert self._reaper_stop is not None
        while not self._reaper_stop.is_set():
            try:
                await asyncio.wait_for(self._reaper_stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            if self._reaper_stop.is_set():
                break
            await self._reap_once()

    async def _reap_once(self) -> None:
        """Single reaper pass: release sandboxes idle longer than idle_seconds."""
        now = datetime.now(timezone.utc)
        for sid in list(self._sandboxes.keys()):
            last = self._last_used.get(sid)
            if last is None:
                continue
            idle = (now - last).total_seconds()
            if idle > self.idle_seconds:
                try:
                    await self.release(sid)
                except Exception:
                    logger.warning("reaper release failed for %s", sid, exc_info=True)
