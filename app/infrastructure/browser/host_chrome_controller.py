"""Managed host Chrome controller for the restricted Browser Host Bridge.

The controller owns every Chrome process and every Playwright object it
creates.  Callers receive only opaque target capabilities; executable paths,
profile paths, CDP ports and websocket paths never cross this boundary.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import inspect
import math
import os
import platform
import re
import secrets
import stat
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
)
from app.infrastructure.browser.host_bridge import TargetClosed
from app.infrastructure.browser.host_protocol import (
    HOST_CDP_MAX_SCREENSHOT_BYTES,
)
from app.infrastructure.browser.playwright_driver import (
    PlaywrightBrowserBackend,
)
from app.infrastructure.browser.url_safety import UrlVerifier

_PROFILE_REF_RE = re.compile(r"bp-host_cdp-[0-9a-f]{12}\Z")
_WEBSOCKET_PATH_RE = re.compile(
    r"/devtools/browser/[A-Za-z0-9._~-]{1,256}\Z"
)
_MACOS_EXECUTABLES = (
    Path(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    ),
    Path(
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing"
    ),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)
_DEFAULT_PROFILE_ROOTS = (
    Path.home() / "Library/Application Support/Google/Chrome",
    Path.home()
    / "Library/Application Support/Google/Chrome for Testing",
    Path.home() / "Library/Application Support/Chromium",
)
_SINGLETON_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_DEVTOOLS_ACTIVE_PORT = "DevToolsActivePort"
_SIDE_EFFECTING_ACTIONS = frozenset({"navigate", "click", "type", "scroll"})
_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "navigate": frozenset({"url"}),
    "observe": frozenset({"max_text_chars", "max_elements"}),
    "click": frozenset({"element_ref", "document_revision"}),
    "type": frozenset(
        {"element_ref", "document_revision", "text", "clear_first"}
    ),
    "scroll": frozenset(
        {"element_ref", "document_revision", "dx", "dy"}
    ),
    "screenshot": frozenset({"full_page"}),
}
_MAX_SCROLL_DELTA = 1_000_000
_WAIT_POLL_SECONDS = 0.01


class HostChromeControllerError(RuntimeError):
    """Controller failure carrying only a stable public error code."""


@dataclass(frozen=True)
class HostChromeControllerConfig:
    """Trusted host configuration; none of these values come from Bridge IO."""

    profile_root: str | os.PathLike[str]
    chrome_executable: str | os.PathLike[str] | None = None
    startup_timeout_seconds: float = 15.0
    process_shutdown_timeout_seconds: float = 2.0
    cancellation_ack_timeout_seconds: float = 2.0
    max_queued_tasks: int = 64
    max_in_flight_tasks: int = 8
    max_screenshot_bytes: int = HOST_CDP_MAX_SCREENSHOT_BYTES
    navigation_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        profile_root = Path(self.profile_root)
        executable = (
            None
            if self.chrome_executable is None
            else Path(self.chrome_executable)
        )
        if not profile_root.is_absolute() or (
            executable is not None and not executable.is_absolute()
        ):
            raise ValueError("host_browser_path_must_be_absolute")
        numeric = (
            self.startup_timeout_seconds,
            self.process_shutdown_timeout_seconds,
            self.cancellation_ack_timeout_seconds,
            self.navigation_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in numeric
        ):
            raise ValueError("host_browser_limits_invalid")
        integer_limits = (
            self.max_queued_tasks,
            self.max_in_flight_tasks,
        )
        if (
            any(
                type(value) is not int or value <= 0
                for value in integer_limits
            )
            or type(self.max_screenshot_bytes) is not int
            or self.max_screenshot_bytes
            != HOST_CDP_MAX_SCREENSHOT_BYTES
        ):
            raise ValueError("host_browser_limits_invalid")


@dataclass
class _ProfileHandle:
    path: Path
    fd: int
    root_fd: int
    root_device: int
    root_inode: int
    device: int
    inode: int
    port_identity: tuple[int, int] | None = None


@dataclass
class _NativeResources:
    lock: Any = field(default_factory=threading.Lock)
    profile: _ProfileHandle | None = None
    process: Any | None = None
    cleaned: bool = False


@dataclass
class _ProvisionalCreation:
    creation_id: str
    native: _NativeResources = field(default_factory=_NativeResources)
    abandoned: bool = False


@dataclass
class _ManagedTarget:
    target_id: str
    profile_ref: str
    session: BrowserSession
    profile: _ProfileHandle
    process: Any
    browser: Any
    context: Any
    page: Any
    driver: PlaywrightBrowserBackend
    native: _NativeResources
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


@dataclass
class _SubmittedOperation:
    future: Future[Any]
    acknowledged: threading.Event


class HostChromeController:
    """Synchronous bridge-facing facade backed by one asyncio owner thread."""

    def __init__(
        self,
        config: HostChromeControllerConfig,
        *,
        popen_factory: Callable[..., Any] | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        driver_factory: Callable[..., PlaywrightBrowserBackend] | None = None,
        url_verifier: UrlVerifier | None = None,
        monotonic: Callable[[], float] | None = None,
        platform_system: Callable[[], str] | None = None,
        active_profile_detector: Callable[[Path], bool] | None = None,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory or subprocess.Popen
        self._playwright_factory = playwright_factory
        self._driver_factory = driver_factory or PlaywrightBrowserBackend
        self._url_verifier = url_verifier or UrlVerifier()
        self._monotonic = monotonic or time.monotonic
        self._platform_system = platform_system or platform.system
        self._test_platform_injection = platform_system is not None
        self._active_profile_detector = (
            active_profile_detector or self._default_active_profile_detector
        )
        self._uid = os.getuid()
        self._profile_root = Path(config.profile_root)
        self._executable = self._resolve_executable()
        self._validate_profile_root()

        self._lifecycle_lock = threading.RLock()
        self._submitted_lock = threading.Lock()
        self._submitted: dict[Future[Any], _SubmittedOperation] = {}
        self._owner_futures: set[Future[Any]] = set()
        self._create_acknowledgements: set[threading.Event] = set()
        capacity = config.max_queued_tasks + config.max_in_flight_tasks
        self._admission = threading.BoundedSemaphore(capacity)
        self._cleanup_admission = threading.BoundedSemaphore(
            config.max_in_flight_tasks
        )
        self._shutdown_started = False
        self._shutdown_result: bool | None = None
        self._shutdown_complete = threading.Event()
        self._healthy = True

        self._loop_ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._owner_thread_id: int | None = None
        self._targets: dict[str, _ManagedTarget] = {}
        self._closing_targets: set[str] = set()
        self._provisionals: dict[str, _ProvisionalCreation] = {}
        self._playwright: Any | None = None
        self._playwright_lock: asyncio.Lock | None = None
        self._in_flight: asyncio.Semaphore | None = None
        self._detached_tasks: set[asyncio.Task[Any]] = set()
        self._native_cleanup_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._owner_main,
            name="host-chrome-owner",
            daemon=True,
        )
        self._thread.start()
        if not self._loop_ready.wait(timeout=5):
            self._healthy = False
            raise HostChromeControllerError("target_unavailable")

    @property
    def healthy(self) -> bool:
        return self._healthy and not self._shutdown_started

    @property
    def owner_thread_alive(self) -> bool:
        return self._thread.is_alive()

    def create_target(self, profile_ref: str) -> str:
        if not isinstance(profile_ref, str) or not _PROFILE_REF_RE.fullmatch(
            profile_ref
        ):
            raise HostChromeControllerError("host_browser_profile_invalid")
        with self._lifecycle_lock:
            self._require_running()
            if not self._admission.acquire(blocking=False):
                raise HostChromeControllerError("host_bridge_busy")
            deadline = (
                self._monotonic() + self.config.startup_timeout_seconds
            )
            acknowledged = threading.Event()
            provisional = _ProvisionalCreation(
                creation_id=f"creation-{secrets.token_urlsafe(18)}"
            )
            with self._submitted_lock:
                self._create_acknowledgements.add(acknowledged)
            with self._native_cleanup_lock:
                self._provisionals[provisional.creation_id] = provisional

            async def create() -> str:
                try:
                    in_flight = self._in_flight
                    if in_flight is None:
                        raise HostChromeControllerError("target_closed")
                    async with in_flight:
                        self._require_startup_time(deadline)
                        return await self._create_target(
                            profile_ref, deadline, provisional
                        )
                finally:
                    self._admission.release()
                    acknowledged.set()
                    with self._submitted_lock:
                        self._create_acknowledgements.discard(
                            acknowledged
                        )
                    with self._native_cleanup_lock:
                        self._provisionals.pop(
                            provisional.creation_id, None
                        )

            try:
                future = self._submit_raw(create())
            except BaseException:
                self._admission.release()
                with self._submitted_lock:
                    self._create_acknowledgements.discard(acknowledged)
                self._abandon_provisional(provisional)
                raise
        try:
            return future.result(
                timeout=self.config.startup_timeout_seconds
                + self.config.process_shutdown_timeout_seconds * 2
                + self.config.cancellation_ack_timeout_seconds
                + 0.1
            )
        except HostChromeControllerError:
            raise
        except BaseException:
            self._abandon_provisional(provisional)
            future.cancel()
            if not acknowledged.wait(timeout=self._hard_cleanup_timeout()):
                self._healthy = False
            raise HostChromeControllerError("target_unavailable") from None

    def close_target(self, target_id: str) -> None:
        if not isinstance(target_id, str) or not target_id:
            return
        with self._lifecycle_lock:
            if self._shutdown_started or self._loop is None:
                return
            if not self._cleanup_admission.acquire(blocking=False):
                raise HostChromeControllerError("host_bridge_busy")

            async def close() -> None:
                try:
                    await self._close_target(target_id)
                finally:
                    self._cleanup_admission.release()

            try:
                future = self._submit_raw(close())
            except BaseException:
                self._cleanup_admission.release()
                raise
        try:
            future.result(
                timeout=self._hard_cleanup_timeout()
            )
        except Exception:
            self._healthy = False
            future.cancel()
            self._force_native_cleanup(target_id)
            raise HostChromeControllerError("target_unavailable") from None

    def execute_action(
        self,
        target_id: str,
        action_type: str,
        action: dict[str, Any],
        document_revision: int,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        domain_action = _convert_action(
            action_type, action, document_revision
        )
        return self._run_deadlined(
            target_id,
            action_type,
            self._execute_action(target_id, action_type, domain_action),
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def get_state(
        self,
        target_id: str,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        return self._run_deadlined(
            target_id,
            "state",
            self._get_state(target_id),
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def sync_after_takeover(
        self,
        target_id: str,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> bytes | None:
        """Start a fresh automation epoch after direct host interaction."""
        return self._run_deadlined(
            target_id,
            "takeover_release",
            self._sync_after_takeover(target_id),
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def shutdown(self) -> bool:
        with self._lifecycle_lock:
            if self._shutdown_started:
                completion = self._shutdown_complete
                first_shutdown = False
            else:
                self._shutdown_started = True
                completion = self._shutdown_complete
                first_shutdown = True
            loop = self._loop
        if not first_shutdown:
            completion.wait(timeout=self._hard_cleanup_timeout())
            with self._lifecycle_lock:
                return self._shutdown_result is True
        if loop is None:
            with self._lifecycle_lock:
                self._shutdown_result = False
                self._shutdown_complete.set()
            return False
        with self._submitted_lock:
            pending = tuple(self._submitted.values())
            owner_futures = tuple(self._owner_futures)
            create_acknowledgements = tuple(
                self._create_acknowledgements
            )
        with self._native_cleanup_lock:
            provisionals = tuple(self._provisionals.values())
        for provisional in provisionals:
            self._abandon_provisional(provisional)
        for future in owner_futures:
            future.cancel()
        for operation in pending:
            operation.future.cancel()
        for operation in pending:
            if not operation.acknowledged.wait(
                timeout=self.config.cancellation_ack_timeout_seconds
            ):
                self._healthy = False
        for acknowledged in create_acknowledgements:
            if not acknowledged.wait(timeout=self._hard_cleanup_timeout()):
                self._healthy = False
        for future in owner_futures:
            if future.done():
                continue
            try:
                future.result(
                    timeout=self.config.cancellation_ack_timeout_seconds
                )
            except Exception:
                pass
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._shutdown_owner(), loop
            )
            future.result(
                timeout=(
                    self.config.process_shutdown_timeout_seconds
                    * max(1, len(self._targets))
                    * 2
                    + self.config.cancellation_ack_timeout_seconds
                )
            )
        except Exception:
            self._healthy = False
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        self._thread.join(
            timeout=self._hard_cleanup_timeout() + 0.1
        )
        if self._thread.is_alive():
            self._healthy = False
        cleanup_confirmed = self._healthy and not self._thread.is_alive()
        with self._lifecycle_lock:
            self._shutdown_result = cleanup_confirmed
            self._shutdown_complete.set()
        return cleanup_confirmed

    # ------------------------------------------------------------------
    # Owner thread and operation submission
    # ------------------------------------------------------------------

    def _owner_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._owner_thread_id = threading.get_ident()
        self._in_flight = asyncio.Semaphore(
            self.config.max_in_flight_tasks
        )
        self._playwright_lock = asyncio.Lock()
        self._loop_ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                _, still_pending = loop.run_until_complete(
                    asyncio.wait(
                        pending, timeout=self._hard_cleanup_timeout()
                    )
                )
                if still_pending:
                    self._healthy = False
                    self._detached_tasks.update(still_pending)
                    for task in still_pending:
                        # Python cannot forcibly terminate a coroutine that
                        # deliberately suppresses cancellation. Native
                        # resources are already reclaimed; abandon the
                        # unusable owner loop without an unbounded join.
                        task._log_destroy_pending = False
                        task.add_done_callback(self._consume_detached_task)
            loop.close()
            self._loop = None

    def _submit_raw(self, coroutine: Coroutine[Any, Any, Any]) -> Future[Any]:
        loop = self._loop
        if loop is None or self._shutdown_started:
            coroutine.close()
            raise HostChromeControllerError("target_closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        with self._submitted_lock:
            self._owner_futures.add(future)

        def discard_owner(_: Future[Any]) -> None:
            with self._submitted_lock:
                self._owner_futures.discard(future)

        future.add_done_callback(discard_owner)
        return future

    def _run_deadlined(
        self,
        target_id: str,
        operation_name: str,
        coroutine: Coroutine[Any, Any, dict[str, Any]],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        if (
            not isinstance(deadline_monotonic, (int, float))
            or isinstance(deadline_monotonic, bool)
            or not math.isfinite(deadline_monotonic)
            or not isinstance(cancel_event, threading.Event)
        ):
            coroutine.close()
            raise HostChromeControllerError("host_bridge_invalid_request")
        if cancel_event.is_set() or self._monotonic() >= deadline_monotonic:
            coroutine.close()
            return _timeout_result(operation_name, begun=False)
        if not self._admission.acquire(blocking=False):
            coroutine.close()
            raise HostChromeControllerError("host_bridge_busy")
        acknowledged = threading.Event()
        operation_state = {"begun": False}

        async def guarded() -> dict[str, Any]:
            try:
                target = self._require_target(target_id)
                in_flight = self._in_flight
                if in_flight is None:
                    raise TargetClosed
                async with target.operation_lock:
                    async with in_flight:
                        if (
                            cancel_event.is_set()
                            or self._monotonic() >= deadline_monotonic
                        ):
                            return _timeout_result(
                                operation_name, begun=False
                            )
                        operation_state["begun"] = True
                        return await coroutine
            except asyncio.CancelledError:
                return _timeout_result(
                    operation_name, begun=operation_state["begun"]
                )
            finally:
                if not operation_state["begun"]:
                    coroutine.close()
                acknowledged.set()
                self._admission.release()

        try:
            future = self._submit_raw(guarded())
        except Exception:
            self._admission.release()
            coroutine.close()
            raise
        submitted = _SubmittedOperation(future, acknowledged)
        with self._submitted_lock:
            self._submitted[future] = submitted

        def discard(_: Future[Any]) -> None:
            with self._submitted_lock:
                self._submitted.pop(future, None)

        future.add_done_callback(discard)
        while True:
            if cancel_event.is_set() or self._monotonic() >= deadline_monotonic:
                cancel_event.set()
                self._cancel_and_confirm(
                    target_id, future, acknowledged
                )
                return _timeout_result(
                    operation_name, begun=operation_state["begun"]
                )
            if future.done():
                try:
                    return future.result()
                except TargetClosed:
                    raise
                except HostChromeControllerError:
                    raise
                except Exception:
                    if self._target_is_closed(target_id):
                        raise TargetClosed from None
                    raise HostChromeControllerError(
                        "target_unavailable"
                    ) from None
            remaining = deadline_monotonic - self._monotonic()
            threading.Event().wait(
                timeout=min(_WAIT_POLL_SECONDS, max(0.0, remaining))
            )

    def _cancel_and_confirm(
        self,
        target_id: str,
        future: Future[Any],
        acknowledged: threading.Event,
    ) -> None:
        future.cancel()
        if acknowledged.wait(
            timeout=self.config.cancellation_ack_timeout_seconds
        ):
            return
        try:
            cleanup = self._submit_raw(
                self._force_close_target(target_id)
            )
            cleanup.result(
                timeout=self._hard_cleanup_timeout()
            )
        except Exception:
            self._healthy = False
            self._force_native_cleanup(target_id)
        if not acknowledged.wait(timeout=self._hard_cleanup_timeout()):
            self._healthy = False
            self._force_native_cleanup(target_id)

    # ------------------------------------------------------------------
    # Target creation
    # ------------------------------------------------------------------

    async def _create_target(
        self,
        profile_ref: str,
        deadline_monotonic: float,
        provisional: _ProvisionalCreation,
    ) -> str:
        self._assert_owner_thread()
        profile: _ProfileHandle | None = None
        process: Any | None = None
        browser: Any | None = None
        context: Any | None = None
        page: Any | None = None

        async def cleanup() -> None:
            try:
                for value in (page, context, browser):
                    if value is not None:
                        await self._bounded_startup_close(value)
            finally:
                self._cleanup_native(provisional.native)

        async def confirmed_cleanup() -> None:
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise

        try:
            profile = self._open_profile(profile_ref)
            if not self._install_profile(provisional, profile):
                raise HostChromeControllerError("target_unavailable")
            executable_identity = self._executable_identity()
            self._prove_profile_inactive(profile)
            self._remove_stale_port(profile)
            self._recheck_profile(profile)
            argv = [
                os.fspath(self._executable),
                f"--user-data-dir={profile.path}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            process = self._popen_factory(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            if not self._install_process(provisional, process):
                raise HostChromeControllerError("target_unavailable")
            try:
                post_launch_identity = self._executable_identity()
            except HostChromeControllerError:
                raise HostChromeControllerError(
                    "target_unavailable"
                ) from None
            if post_launch_identity != executable_identity:
                raise HostChromeControllerError("target_unavailable")
            self._recheck_profile(profile)
            endpoint = await self._wait_for_endpoint(
                profile, process, deadline_monotonic
            )
            playwright = await self._await_startup(
                self._ensure_playwright(), deadline_monotonic
            )
            browser = await self._await_startup(
                playwright.chromium.connect_over_cdp(endpoint),
                deadline_monotonic,
            )
            context = await self._await_startup(
                self._select_context(browser), deadline_monotonic
            )
            page = await self._await_startup(
                self._select_page(context), deadline_monotonic
            )
            self._require_startup_time(deadline_monotonic)
            target_id = self._new_target_id()
            session = BrowserSession(
                id=target_id,
                bound_n_agent_session_id=target_id,
                backend_type=BrowserBackendType.HOST_CDP,
                status=BrowserSessionStatus.ACTIVE,
                profile_ref=profile_ref,
            )
            driver = self._driver_factory(
                url_verifier=self._url_verifier,
                default_timeout_seconds=(
                    self.config.navigation_timeout_seconds
                ),
                max_screenshot_bytes=self.config.max_screenshot_bytes,
            )
            driver.attach_page(page)
            self._require_startup_time(deadline_monotonic)
            with provisional.native.lock:
                if provisional.abandoned or provisional.native.cleaned:
                    raise HostChromeControllerError("target_unavailable")
            self._targets[target_id] = _ManagedTarget(
                target_id=target_id,
                profile_ref=profile_ref,
                session=session,
                profile=profile,
                process=process,
                browser=browser,
                context=context,
                page=page,
                driver=driver,
                native=provisional.native,
            )
            return target_id
        except asyncio.CancelledError:
            await confirmed_cleanup()
            raise
        except HostChromeControllerError:
            await confirmed_cleanup()
            raise
        except Exception:
            await confirmed_cleanup()
            raise HostChromeControllerError("target_unavailable") from None

    async def _bounded_startup_close(self, value: Any) -> None:
        close = getattr(value, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await self._bounded_owner_await(result)
        except BaseException:
            self._healthy = False

    async def _bounded_owner_await(self, awaitable: Any) -> bool:
        task = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait(
            {task}, timeout=self._hard_cleanup_timeout()
        )
        if task in done:
            try:
                task.result()
                return True
            except BaseException:
                self._healthy = False
                return False
        task.cancel()
        done, _ = await asyncio.wait(
            {task}, timeout=self._hard_cleanup_timeout()
        )
        if task not in done:
            self._healthy = False
            self._detached_tasks.add(task)
            task.add_done_callback(self._consume_detached_task)
            return False
        try:
            task.result()
        except BaseException:
            pass
        self._healthy = False
        return False

    def _consume_detached_task(self, task: asyncio.Task[Any]) -> None:
        try:
            task.exception()
        except BaseException:
            pass
        finally:
            self._detached_tasks.discard(task)

    def _install_profile(
        self,
        provisional: _ProvisionalCreation,
        profile: _ProfileHandle,
    ) -> bool:
        with provisional.native.lock:
            if provisional.abandoned or provisional.native.cleaned:
                accepted = False
            else:
                provisional.native.profile = profile
                accepted = True
        if not accepted:
            self._release_profile(profile)
        return accepted

    def _install_process(
        self, provisional: _ProvisionalCreation, process: Any
    ) -> bool:
        with provisional.native.lock:
            if provisional.abandoned or provisional.native.cleaned:
                accepted = False
            else:
                provisional.native.process = process
                accepted = True
        if not accepted:
            if not self._stop_process(process):
                self._healthy = False
        return accepted

    def _abandon_provisional(
        self, provisional: _ProvisionalCreation
    ) -> None:
        with provisional.native.lock:
            provisional.abandoned = True
        self._cleanup_native(provisional.native)
        with self._native_cleanup_lock:
            self._provisionals.pop(provisional.creation_id, None)

    def _cleanup_native(self, native: _NativeResources) -> None:
        with native.lock:
            if native.cleaned:
                return
            native.cleaned = True
            process = native.process
            profile = native.profile
            native.process = None
            native.profile = None
        try:
            exited = (
                self._stop_process(process)
                if process is not None
                else False
            )
            if profile is not None and exited:
                self._capture_owned_port_identity(profile)
                self._remove_owned_port(profile)
            elif process is not None and not exited:
                self._healthy = False
        finally:
            if profile is not None:
                self._release_profile(profile)

    def _open_profile(self, profile_ref: str) -> _ProfileHandle:
        self._assert_owner_thread()
        self._validate_profile_root()
        root_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            root_flags |= os.O_NOFOLLOW
        root_fd = os.open(self._profile_root, root_flags)
        try:
            root_stat = os.fstat(root_fd)
            self._validate_owned_directory_stat(root_stat)
            path_stat = os.lstat(self._profile_root)
            if _identity(root_stat) != _identity(path_stat):
                raise HostChromeControllerError(
                    "host_browser_profile_invalid"
                )
            try:
                os.mkdir(profile_ref, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            profile_flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                profile_flags |= os.O_NOFOLLOW
            profile_fd = os.open(
                profile_ref, profile_flags, dir_fd=root_fd
            )
        except Exception:
            os.close(root_fd)
            raise
        try:
            profile_stat = os.fstat(profile_fd)
            self._validate_owned_directory_stat(profile_stat)
            path_stat = os.lstat(self._profile_root / profile_ref)
            if _identity(profile_stat) != _identity(path_stat):
                raise HostChromeControllerError(
                    "host_browser_profile_invalid"
                )
            try:
                fcntl.flock(
                    profile_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
                )
            except (BlockingIOError, OSError):
                raise HostChromeControllerError(
                    "host_browser_profile_busy"
                ) from None
            for name in _SINGLETON_NAMES:
                try:
                    os.lstat(name, dir_fd=profile_fd)
                except FileNotFoundError:
                    continue
                raise HostChromeControllerError(
                    "host_browser_profile_busy"
                )
            return _ProfileHandle(
                path=self._profile_root / profile_ref,
                fd=profile_fd,
                root_fd=root_fd,
                root_device=root_stat.st_dev,
                root_inode=root_stat.st_ino,
                device=profile_stat.st_dev,
                inode=profile_stat.st_ino,
            )
        except Exception:
            try:
                fcntl.flock(profile_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(profile_fd)
            os.close(root_fd)
            raise

    def _remove_stale_port(self, profile: _ProfileHandle) -> None:
        self._recheck_profile(profile)
        try:
            port_stat = os.lstat(_DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(port_stat.st_mode) or not stat.S_ISREG(
            port_stat.st_mode
        ):
            raise HostChromeControllerError("host_browser_profile_invalid")
        os.unlink(_DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd)

    def _prove_profile_inactive(self, profile: _ProfileHandle) -> None:
        self._recheck_profile(profile)
        try:
            active = self._active_profile_detector(profile.path)
        except Exception:
            raise HostChromeControllerError(
                "host_browser_profile_busy"
            ) from None
        if type(active) is not bool or active:
            raise HostChromeControllerError(
                "host_browser_profile_busy"
            )

    @staticmethod
    def _default_active_profile_detector(profile_path: Path) -> bool:
        """Return whether a running process uses this exact user-data-dir.

        Failure to obtain or parse the process snapshot is reported to the
        caller, which maps it to profile-busy.  No command line is logged.
        """
        completed = subprocess.run(
            ["ps", "-axo", "command="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=2.0,
        )
        escaped_path = re.escape(os.fspath(profile_path))
        equals_form = re.compile(
            rf"(?<!\S)--user-data-dir={escaped_path}(?=\s|$)"
        )
        separate_form = re.compile(
            rf"(?<!\S)--user-data-dir\s+{escaped_path}(?=\s|$)"
        )
        for line in completed.stdout.splitlines():
            if "\x00" in line:
                raise RuntimeError("profile_process_detection_ambiguous")
            if equals_form.search(line) or separate_form.search(line):
                return True
        return False

    async def _wait_for_endpoint(
        self,
        profile: _ProfileHandle,
        process: Any,
        deadline: float,
    ) -> str:
        while self._monotonic() < deadline:
            if _process_poll(process) is not None:
                raise HostChromeControllerError("target_unavailable")
            self._recheck_profile(profile)
            try:
                port, websocket_path, identity = self._read_active_port(
                    profile
                )
            except FileNotFoundError:
                await asyncio.sleep(_WAIT_POLL_SECONDS)
                continue
            profile.port_identity = identity
            # websocket_path is validated but deliberately never retained.
            del websocket_path
            return f"http://127.0.0.1:{port}"
        raise HostChromeControllerError("target_unavailable")

    async def _await_startup(
        self, awaitable: Any, deadline_monotonic: float
    ) -> Any:
        remaining = deadline_monotonic - self._monotonic()
        if remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise HostChromeControllerError("target_unavailable")
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError:
            raise HostChromeControllerError("target_unavailable") from None

    def _require_startup_time(self, deadline_monotonic: float) -> None:
        if self._monotonic() >= deadline_monotonic:
            raise HostChromeControllerError("target_unavailable")

    def _read_active_port(
        self, profile: _ProfileHandle
    ) -> tuple[int, str, tuple[int, int]]:
        before = os.lstat(_DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise HostChromeControllerError("target_unavailable")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(_DEVTOOLS_ACTIVE_PORT, flags, dir_fd=profile.fd)
        try:
            opened = os.fstat(fd)
            if _identity(before) != _identity(opened):
                raise HostChromeControllerError("target_unavailable")
            chunks: list[bytes] = []
            total = 0
            while total <= 1024:
                chunk = os.read(fd, min(1025 - total, 1025))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > 1024:
                raise HostChromeControllerError("target_unavailable")
            after = os.lstat(_DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd)
            if _identity(after) != _identity(opened):
                raise HostChromeControllerError("target_unavailable")
            # The stale file was removed while this profile lock was held.
            # Once the newly created file has passed no-follow + identity
            # checks, retain its identity even if content parsing fails so
            # owned-process cleanup can unlink this exact file only.
            profile.port_identity = _identity(opened)
        finally:
            os.close(fd)
        try:
            text = b"".join(chunks).decode("ascii")
        except UnicodeDecodeError:
            raise HostChromeControllerError("target_unavailable") from None
        lines = text.splitlines()
        if len(lines) != 2:
            raise HostChromeControllerError("target_unavailable")
        try:
            port = int(lines[0], 10)
        except ValueError:
            raise HostChromeControllerError("target_unavailable") from None
        if not 1024 <= port <= 65535:
            raise HostChromeControllerError("target_unavailable")
        websocket_path = lines[1]
        if not _WEBSOCKET_PATH_RE.fullmatch(websocket_path):
            raise HostChromeControllerError("target_unavailable")
        return port, websocket_path, _identity(opened)

    async def _ensure_playwright(self) -> Any:
        self._assert_owner_thread()
        if self._playwright is not None:
            return self._playwright
        lock = self._playwright_lock
        if lock is None:
            raise HostChromeControllerError("target_unavailable")
        async with lock:
            if self._playwright is not None:
                return self._playwright
            if self._playwright_factory is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    raise HostChromeControllerError(
                        "target_unavailable"
                    ) from None
                starter: Any = async_playwright()
            else:
                starter = self._playwright_factory()
            if inspect.isawaitable(starter):
                starter = await starter
            if hasattr(starter, "start"):
                started = starter.start()
                starter = (
                    await started
                    if inspect.isawaitable(started)
                    else started
                )
            self._playwright = starter
            return starter

    @staticmethod
    async def _select_context(browser: Any) -> Any:
        contexts = getattr(browser, "contexts", ())
        if contexts:
            return contexts[0]
        if hasattr(browser, "new_context"):
            return await browser.new_context()
        raise HostChromeControllerError("target_unavailable")

    @staticmethod
    async def _select_page(context: Any) -> Any:
        pages = getattr(context, "pages", ())
        if pages:
            return pages[0]
        if hasattr(context, "new_page"):
            return await context.new_page()
        raise HostChromeControllerError("target_unavailable")

    # ------------------------------------------------------------------
    # Actions/state (completed by subsequent TDD sections)
    # ------------------------------------------------------------------

    async def _execute_action(
        self, target_id: str, action_type: str, action: Any
    ) -> dict[str, Any]:
        self._assert_owner_thread()
        target = self._require_target(target_id)
        self._require_live_process(target)
        target.driver.clear_last_screenshot()
        try:
            result = await target.driver.execute_action(
                target.session.id, action
            )
        except Exception:
            if self._managed_target_closed(target):
                raise TargetClosed from None
            raise HostChromeControllerError("target_unavailable") from None
        if self._managed_target_closed(target):
            raise TargetClosed
        response = _serialize_action_result(result)
        if result.status == "success":
            screenshot = target.driver.last_screenshot_bytes()
            if (
                isinstance(screenshot, bytes)
                and 0 < len(screenshot)
                <= self.config.max_screenshot_bytes
            ):
                response["screenshot_base64"] = base64.b64encode(
                    screenshot
                ).decode("ascii")
            elif action_type == "screenshot":
                response = {
                    "action_type": action_type,
                    "status": "error",
                    "error_code": "screenshot_unavailable",
                    "document_revision": result.document_revision,
                }
            elif not response.get("warning_code"):
                response["warning_code"] = "screenshot_unavailable"
        return response

    async def _get_state(self, target_id: str) -> dict[str, Any]:
        self._assert_owner_thread()
        target = self._require_target(target_id)
        self._require_live_process(target)
        try:
            state = await target.driver.get_state(target.session.id)
        except Exception:
            if self._managed_target_closed(target):
                raise TargetClosed from None
            raise HostChromeControllerError("target_unavailable") from None
        return {
            "safe_url": state.safe_url,
            "title": state.title,
            "status": state.status.value,
            "document_revision": state.document_revision,
            "latest_screenshot_ref": state.latest_screenshot_ref,
        }

    async def _sync_after_takeover(self, target_id: str) -> bytes | None:
        self._assert_owner_thread()
        target = self._require_target(target_id)
        self._require_live_process(target)
        try:
            await target.driver.sync_after_takeover()
        except Exception:
            if self._managed_target_closed(target):
                raise TargetClosed from None
            raise HostChromeControllerError("target_unavailable") from None
        if self._managed_target_closed(target):
            raise TargetClosed
        return target.driver.last_screenshot_bytes()

    # ------------------------------------------------------------------
    # Close and process lifecycle
    # ------------------------------------------------------------------

    async def _close_target(self, target_id: str) -> None:
        self._assert_owner_thread()
        target = self._targets.get(target_id)
        if target is None:
            return
        if target_id in self._closing_targets:
            return
        self._closing_targets.add(target_id)
        try:
            async with target.operation_lock:
                cleanup = asyncio.create_task(
                    self._close_managed_target(target)
                )
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
            if target.closed:
                self._targets.pop(target_id, None)
        finally:
            self._closing_targets.discard(target_id)

    async def _force_close_target(self, target_id: str) -> None:
        self._assert_owner_thread()
        target = self._targets.get(target_id)
        if target is None:
            return
        await self._close_managed_target(target)
        if target.closed:
            self._targets.pop(target_id, None)
        self._closing_targets.discard(target_id)

    async def _close_managed_target(self, target: _ManagedTarget) -> None:
        if target.closed:
            return
        try:
            for close in (
                lambda: target.driver.close_session(target.session.id),
                target.context.close,
                target.browser.close,
            ):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await self._bounded_owner_await(result)
                except BaseException:
                    self._healthy = False
        finally:
            self._cleanup_native(target.native)
            target.closed = True

    async def _shutdown_owner(self) -> None:
        self._assert_owner_thread()
        targets = tuple(self._targets.values())
        for target in targets:
            await self._close_managed_target(target)
            self._targets.pop(target.target_id, None)
        self._closing_targets.clear()
        if self._playwright is not None:
            try:
                await self._bounded_owner_await(
                    self._playwright.stop()
                )
            except Exception:
                self._healthy = False
            self._playwright = None

    def _force_native_cleanup(self, target_id: str) -> None:
        with self._native_cleanup_lock:
            target = self._targets.get(target_id)
        if target is None:
            return
        self._cleanup_native(target.native)
        target.closed = True

    def _hard_cleanup_timeout(self) -> float:
        return (
            self.config.cancellation_ack_timeout_seconds * 4
            + self.config.process_shutdown_timeout_seconds * 4
        )

    def _stop_process(self, process: Any) -> bool:
        if _process_poll(process) is None:
            try:
                process.terminate()
            except Exception:
                pass
        if self._wait_process(process):
            return True
        try:
            process.kill()
        except Exception:
            pass
        return self._wait_process(process)

    def _wait_process(self, process: Any) -> bool:
        try:
            process.wait(
                timeout=self.config.process_shutdown_timeout_seconds
            )
            return _process_poll(process) is not None
        except Exception:
            return _process_poll(process) is not None

    def _remove_owned_port(self, profile: _ProfileHandle) -> None:
        if profile.port_identity is None:
            return
        try:
            self._recheck_profile(profile)
            current = os.lstat(
                _DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd
            )
            if _identity(current) == profile.port_identity:
                os.unlink(_DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd)
        except (OSError, HostChromeControllerError):
            return

    def _capture_owned_port_identity(
        self, profile: _ProfileHandle
    ) -> None:
        if profile.port_identity is not None:
            return
        try:
            self._recheck_profile(profile)
            current = os.lstat(
                _DEVTOOLS_ACTIVE_PORT, dir_fd=profile.fd
            )
            if stat.S_ISREG(current.st_mode) and not stat.S_ISLNK(
                current.st_mode
            ):
                profile.port_identity = _identity(current)
        except (OSError, HostChromeControllerError):
            return

    @staticmethod
    def _release_profile(profile: _ProfileHandle) -> None:
        profile_fd = profile.fd
        root_fd = profile.root_fd
        profile.fd = -1
        profile.root_fd = -1
        try:
            if profile_fd >= 0:
                fcntl.flock(profile_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            if profile_fd >= 0:
                os.close(profile_fd)
        except OSError:
            pass
        try:
            if root_fd >= 0:
                os.close(root_fd)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Security validation helpers
    # ------------------------------------------------------------------

    def _resolve_executable(self) -> Path:
        if self._platform_system() != "Darwin":
            if not self._test_platform_injection:
                raise HostChromeControllerError(
                    "host_browser_platform_unsupported"
                )
        configured = self.config.chrome_executable
        if configured is not None:
            executable = Path(configured)
            self._validate_executable(executable)
            return executable
        if self._platform_system() != "Darwin":
            raise HostChromeControllerError(
                "host_browser_platform_unsupported"
            )
        for candidate in _MACOS_EXECUTABLES:
            try:
                self._validate_executable(candidate)
            except HostChromeControllerError:
                continue
            return candidate
        raise HostChromeControllerError("host_chrome_executable_invalid")

    def _validate_executable(self, executable: Path) -> None:
        try:
            _reject_symlink_components(executable)
            metadata = os.lstat(executable)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not os.access(executable, os.X_OK)
            ):
                raise OSError
        except (OSError, ValueError):
            raise HostChromeControllerError(
                "host_chrome_executable_invalid"
            ) from None

    def _executable_identity(self) -> tuple[int, int]:
        try:
            self._validate_executable(self._executable)
            before = os.lstat(self._executable)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self._executable, flags)
            try:
                opened = os.fstat(fd)
            finally:
                os.close(fd)
            if _identity(before) != _identity(opened):
                raise OSError
            return _identity(opened)
        except (OSError, HostChromeControllerError):
            raise HostChromeControllerError(
                "host_chrome_executable_invalid"
            ) from None

    def _validate_profile_root(self) -> None:
        try:
            _reject_symlink_components(self._profile_root)
            metadata = os.lstat(self._profile_root)
            self._validate_owned_directory_stat(metadata)
            root = _normalized_absolute(self._profile_root)
            for default in _DEFAULT_PROFILE_ROOTS:
                default_root = _normalized_absolute(default)
                if _paths_overlap(root, default_root):
                    raise OSError
        except (OSError, ValueError, HostChromeControllerError):
            raise HostChromeControllerError(
                "host_browser_profile_invalid"
            ) from None

    def _validate_owned_directory_stat(self, metadata: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._uid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise HostChromeControllerError(
                "host_browser_profile_invalid"
            )

    def _recheck_profile(self, profile: _ProfileHandle) -> None:
        try:
            _reject_symlink_components(self._profile_root)
            root_metadata = os.fstat(profile.root_fd)
            root_current = os.lstat(self._profile_root)
        except (OSError, ValueError):
            raise HostChromeControllerError(
                "host_browser_profile_invalid"
            ) from None
        self._validate_owned_directory_stat(root_metadata)
        if (
            _identity(root_metadata)
            != (profile.root_device, profile.root_inode)
            or _identity(root_current)
            != (profile.root_device, profile.root_inode)
        ):
            raise HostChromeControllerError(
                "host_browser_profile_invalid"
            )
        metadata = os.fstat(profile.fd)
        self._validate_owned_directory_stat(metadata)
        current = os.lstat(profile.path)
        if (
            _identity(metadata) != (profile.device, profile.inode)
            or _identity(current) != (profile.device, profile.inode)
        ):
            raise HostChromeControllerError(
                "host_browser_profile_invalid"
            )

    def _require_target(self, target_id: str) -> _ManagedTarget:
        target = self._targets.get(target_id)
        if target is None or target.closed:
            raise TargetClosed
        return target

    def _target_is_closed(self, target_id: str) -> bool:
        future: Future[bool]

        async def inspect_target() -> bool:
            target = self._targets.get(target_id)
            return target is None or self._managed_target_closed(target)

        try:
            future = self._submit_raw(inspect_target())
            return future.result(timeout=0.5)
        except Exception:
            return True

    @staticmethod
    def _managed_target_closed(target: _ManagedTarget) -> bool:
        if target.closed or _process_poll(target.process) is not None:
            return True
        try:
            if hasattr(target.page, "is_closed") and target.page.is_closed():
                return True
        except Exception:
            return True
        try:
            if hasattr(target.browser, "is_connected"):
                connected = target.browser.is_connected()
                if connected is False:
                    return True
        except Exception:
            return True
        return False

    def _require_live_process(self, target: _ManagedTarget) -> None:
        if self._managed_target_closed(target):
            raise TargetClosed

    def _require_running(self) -> None:
        if self._shutdown_started or not self._healthy:
            raise HostChromeControllerError("target_closed")

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise HostChromeControllerError("target_unavailable")

    def _new_target_id(self) -> str:
        while True:
            target_id = "target-" + secrets.token_urlsafe(18)
            if target_id not in self._targets:
                return target_id


def _convert_action(
    action_type: str, action: dict[str, Any], document_revision: int
) -> Any:
    if not isinstance(action_type, str) or action_type not in _ACTION_FIELDS:
        raise HostChromeControllerError("unknown_capability")
    if (
        not isinstance(action, dict)
        or set(action) != _ACTION_FIELDS.get(action_type, frozenset())
        or type(document_revision) is not int
        or document_revision < 0
    ):
        raise HostChromeControllerError("host_bridge_invalid_request")
    try:
        if action_type == "navigate":
            return NavigateAction(url=action["url"])
        if action_type == "observe":
            return ObserveAction(
                max_text_chars=action["max_text_chars"],
                max_elements=action["max_elements"],
            )
        if action_type == "click":
            nested = action["document_revision"]
            if type(nested) is not int or nested != document_revision:
                raise ValueError
            return ClickAction(
                element_ref=action["element_ref"],
                document_revision=nested,
            )
        if action_type == "type":
            nested = action["document_revision"]
            if (
                type(nested) is not int
                or nested != document_revision
                or type(action["clear_first"]) is not bool
            ):
                raise ValueError
            return TypeAction(
                element_ref=action["element_ref"],
                document_revision=nested,
                text=action["text"],
                clear_first=action["clear_first"],
            )
        if action_type == "scroll":
            nested = action["document_revision"]
            dx = action["dx"]
            dy = action["dy"]
            if (
                type(nested) is not int
                or nested != document_revision
                or type(dx) is not int
                or type(dy) is not int
                or abs(dx) > _MAX_SCROLL_DELTA
                or abs(dy) > _MAX_SCROLL_DELTA
            ):
                raise ValueError
            return ScrollAction(
                element_ref=action["element_ref"],
                document_revision=nested,
                dx=dx,
                dy=dy,
            )
        if action_type == "screenshot":
            if type(action["full_page"]) is not bool:
                raise ValueError
            return ScreenshotAction(full_page=action["full_page"])
    except (KeyError, TypeError, ValueError):
        pass
    raise HostChromeControllerError("host_bridge_invalid_request")


def _serialize_action_result(result: BrowserActionResult) -> dict[str, Any]:
    response: dict[str, Any] = {
        "action_type": result.action_type,
        "status": result.status,
        "document_revision": result.document_revision,
    }
    if result.status == "success":
        if result.url is not None:
            response["url"] = result.url
        if result.title is not None:
            response["title"] = result.title
        if result.text is not None:
            response["text"] = result.text
        if result.elements:
            response["elements"] = [asdict(element) for element in result.elements]
        if result.screenshot_ref is not None:
            response["screenshot_ref"] = result.screenshot_ref
        if result.warning_code is not None:
            response["warning_code"] = result.warning_code
    elif result.error_code is not None:
        response["error_code"] = result.error_code
    return response


def _timeout_result(operation_name: str, *, begun: bool) -> dict[str, Any]:
    uncertain = begun and operation_name in _SIDE_EFFECTING_ACTIONS
    return {
        "action_type": operation_name,
        "status": "error",
        "error_code": (
            "action_outcome_unknown" if uncertain else "host_bridge_timeout"
        ),
    }


async def _quiet_async_close(value: Any) -> None:
    close = getattr(value, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _process_poll(process: Any) -> int | None:
    try:
        return process.poll()
    except Exception:
        return None


def _normalized_absolute(path: Path) -> Path:
    return Path(os.path.normcase(os.path.abspath(os.fspath(path))))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError


__all__ = [
    "HostChromeController",
    "HostChromeControllerConfig",
    "HostChromeControllerError",
]
