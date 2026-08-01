from __future__ import annotations

import asyncio
import base64
import fcntl
import gc
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app.infrastructure.browser.host_chrome_controller as controller_module
from app.domain.browser import (
    BrowserActionResult,
    BrowserSessionStatus,
    BrowserState,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
)
from app.infrastructure.browser.host_bridge import TargetClosed
from app.infrastructure.browser.host_chrome_controller import (
    HostChromeController,
    HostChromeControllerConfig,
)
from app.infrastructure.browser.host_protocol import (
    HOST_CDP_MAX_SCREENSHOT_BYTES,
)

PROFILE_REF = "bp-host_cdp-0123456789ab"


def test_controller_uses_exact_shared_host_cdp_screenshot_limit(
    tmp_path: Path,
) -> None:
    config = HostChromeControllerConfig(profile_root=tmp_path)
    assert config.max_screenshot_bytes == HOST_CDP_MAX_SCREENSHOT_BYTES
    for value in (
        HOST_CDP_MAX_SCREENSHOT_BYTES - 1,
        HOST_CDP_MAX_SCREENSHOT_BYTES + 1,
    ):
        with pytest.raises(
            ValueError, match="host_browser_limits_invalid"
        ):
            HostChromeControllerConfig(
                profile_root=tmp_path,
                max_screenshot_bytes=value,
            )


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waited += 1
        if self.returncode is None:
            raise TimeoutError
        return self.returncode


class FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com/"
        self.main_frame = object()
        self.closed = False

    async def title(self) -> str:
        return "Example"

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.page = FakePage()
        self.contexts = [FakeContext(self.page)]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.endpoints: list[str] = []

    async def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.endpoints.append(endpoint)
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class PermanentlyResistantStarter:
    entered = threading.Event()

    async def start(self) -> FakePlaywright:
        type(self).entered.set()
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                continue


def _secure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake executable")
    path.chmod(0o700)


def _secure_root(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _controller(
    tmp_path: Path,
    *,
    before_return: Any | None = None,
) -> tuple[HostChromeController, list[dict[str, Any]], FakeProcess, FakePlaywright]:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    calls: list[dict[str, Any]] = []
    process = FakeProcess()
    playwright = FakePlaywright()

    def popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append({"argv": argv, **kwargs})
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        if before_return is not None:
            before_return()
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.5,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        platform_system=lambda: "Darwin",
    )
    return controller, calls, process, playwright


def test_profile_ref_and_fixed_headed_chrome_argv(tmp_path: Path) -> None:
    controller, calls, _, _ = _controller(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_invalid"):
            controller.create_target("../default")
        target_id = controller.create_target(PROFILE_REF)
        assert target_id.startswith("target-")
        assert len(calls) == 1
        call = calls[0]
        assert call["argv"][1:] == [
            f"--user-data-dir={tmp_path / 'profiles' / PROFILE_REF}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        assert call["shell"] is False
        assert call["stdin"] != subprocess.PIPE
        assert call["stdout"] != subprocess.PIPE
        assert call["stderr"] != subprocess.PIPE
    finally:
        controller.shutdown()


def test_new_profile_is_exactly_owner_only(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    try:
        controller.create_target(PROFILE_REF)
        mode = stat.S_IMODE((tmp_path / "profiles" / PROFILE_REF).stat().st_mode)
        assert mode == 0o700
    finally:
        controller.shutdown()


def test_profile_root_component_symlink_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    _secure_root(actual)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    executable = tmp_path / "Chrome"
    _secure_file(executable)
    with pytest.raises(RuntimeError, match="host_browser_profile_invalid"):
        HostChromeController(
            HostChromeControllerConfig(
                profile_root=linked,
                chrome_executable=executable,
            ),
            popen_factory=lambda *a, **k: FakeProcess(),
            playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
            platform_system=lambda: "Darwin",
        )


def test_executable_component_symlink_is_rejected(tmp_path: Path) -> None:
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    executable = actual_dir / "Chrome"
    _secure_file(executable)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(actual_dir, target_is_directory=True)
    profiles = tmp_path / "profiles"
    _secure_root(profiles)
    with pytest.raises(RuntimeError, match="host_chrome_executable_invalid"):
        HostChromeController(
            HostChromeControllerConfig(
                profile_root=profiles,
                chrome_executable=linked_dir / "Chrome",
            ),
            popen_factory=lambda *a, **k: FakeProcess(),
            playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
            platform_system=lambda: "Darwin",
        )


def test_executable_identity_rechecked_after_launch(tmp_path: Path) -> None:
    executable = tmp_path / "Chrome"

    def replace_executable() -> None:
        replacement = tmp_path / "replacement"
        _secure_file(replacement)
        os.replace(replacement, executable)

    controller, _, process, _ = _controller(
        tmp_path, before_return=replace_executable
    )
    try:
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
        assert process.terminated == 1
        assert process.waited >= 1
    finally:
        controller.shutdown()


def test_unknown_singleton_or_profile_lock_is_busy(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    profile = tmp_path / "profiles" / PROFILE_REF
    profile.mkdir(mode=0o700)
    (profile / "SingletonLock").write_text("unknown", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_busy"):
            controller.create_target(PROFILE_REF)
    finally:
        controller.shutdown()


def test_non_macos_without_test_platform_injection_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="host_browser_platform_unsupported"):
        HostChromeController(
            HostChromeControllerConfig(
                profile_root=profiles,
                chrome_executable=executable,
            )
        )


def test_macos_discovery_uses_only_fixed_candidate_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "standard-app" / "Chrome"
    arbitrary = tmp_path / "arbitrary-app" / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(candidate)
    _secure_file(arbitrary)
    _secure_root(profiles)
    monkeypatch.setattr(
        controller_module, "_MACOS_EXECUTABLES", (candidate,)
    )
    controller = HostChromeController(
        HostChromeControllerConfig(profile_root=profiles),
        popen_factory=lambda *a, **k: FakeProcess(),
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
    )
    try:
        assert controller._executable == candidate
        assert controller._executable != arbitrary
    finally:
        controller.shutdown()


def test_profile_root_cannot_overlap_default_chrome_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    monkeypatch.setattr(
        controller_module, "_DEFAULT_PROFILE_ROOTS", (profiles / "Default",)
    )
    with pytest.raises(RuntimeError, match="host_browser_profile_invalid"):
        HostChromeController(
            HostChromeControllerConfig(
                profile_root=profiles,
                chrome_executable=executable,
            ),
            popen_factory=lambda *a, **k: FakeProcess(),
            playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
            platform_system=lambda: "Darwin",
        )


class RecordingDriver:
    instances: list["RecordingDriver"] = []

    def __init__(self, **_: Any) -> None:
        self.page: Any = None
        self.actions: list[Any] = []
        self.session_ids: list[str] = []
        self.thread_ids: list[int] = []
        self.screenshot: bytes | None = b"shot"
        self._last_screenshot: bytes | None = None
        self.closed = False
        RecordingDriver.instances.append(self)

    def attach_page(self, page: Any) -> None:
        self.page = page

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        self.actions.append(action)
        self.session_ids.append(session_id)
        self.thread_ids.append(threading.get_ident())
        self._last_screenshot = self.screenshot
        name = type(action).__name__.replace("Action", "").lower()
        return BrowserActionResult(
            action_type=name,
            status="success",
            document_revision=getattr(action, "document_revision", 0),
        )

    async def get_state(self, session_id: str) -> BrowserState:
        self.session_ids.append(session_id)
        self.thread_ids.append(threading.get_ident())
        return BrowserState(
            safe_url="https://example.com/",
            title="Example",
            status=BrowserSessionStatus.ACTIVE,
            document_revision=3,
            latest_screenshot_ref=None,
        )

    async def close_session(self, session_id: str) -> None:
        self.closed = True
        await self.page.close()

    def last_screenshot_bytes(self) -> bytes | None:
        return self._last_screenshot

    def clear_last_screenshot(self) -> None:
        self._last_screenshot = None

    async def sync_after_takeover(self) -> None:
        self.thread_ids.append(threading.get_ident())
        self._last_screenshot = self.screenshot


class MultiChromium:
    def __init__(self) -> None:
        self.browsers: list[FakeBrowser] = []

    async def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        browser = FakeBrowser()
        self.browsers.append(browser)
        return browser


class HungChromium:
    async def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        await asyncio.sleep(10)
        return FakeBrowser()


class MultiPlaywright(FakePlaywright):
    def __init__(self) -> None:
        self.chromium = MultiChromium()
        self.stopped = False


def test_playwright_initialization_is_single_flight_for_concurrent_creates(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    playwright = MultiPlaywright()
    starts = 0

    class SlowStarter:
        async def start(self) -> MultiPlaywright:
            nonlocal starts
            starts += 1
            await asyncio.sleep(0.05)
            return playwright

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile_arg = next(
            item for item in argv if item.startswith("--user-data-dir=")
        )
        profile = Path(profile_arg.split("=", 1)[1])
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.5,
            max_in_flight_tasks=2,
        ),
        popen_factory=popen,
        playwright_factory=SlowStarter,
        driver_factory=RecordingDriver,
        platform_system=lambda: "Darwin",
        active_profile_detector=lambda path: False,
    )
    results: list[Any] = []
    workers = [
        threading.Thread(
            target=lambda ref=ref: results.append(
                controller.create_target(ref)
            )
        )
        for ref in (PROFILE_REF, "bp-host_cdp-fedcba987654")
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=1)
        assert len(results) == 2
        assert starts == 1
        assert controller._playwright is playwright
    finally:
        controller.shutdown()
    assert playwright.stopped is True


def _recording_controller(
    tmp_path: Path,
    *,
    driver_factory: Any = RecordingDriver,
    max_queued_tasks: int = 8,
    max_in_flight_tasks: int = 4,
    cancellation_ack_timeout_seconds: float = 2.0,
    process_shutdown_timeout_seconds: float = 2.0,
) -> tuple[HostChromeController, MultiPlaywright]:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    playwright = MultiPlaywright()

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile_arg = next(
            item for item in argv if item.startswith("--user-data-dir=")
        )
        profile = Path(profile_arg.split("=", 1)[1])
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.5,
            max_queued_tasks=max_queued_tasks,
            max_in_flight_tasks=max_in_flight_tasks,
            cancellation_ack_timeout_seconds=(
                cancellation_ack_timeout_seconds
            ),
            process_shutdown_timeout_seconds=(
                process_shutdown_timeout_seconds
            ),
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        driver_factory=driver_factory,
        platform_system=lambda: "Darwin",
    )
    return controller, playwright


@pytest.mark.parametrize(
    ("action_type", "payload", "revision", "expected"),
    [
        ("navigate", {"url": "https://example.com"}, 0, NavigateAction),
        (
            "observe",
            {"max_text_chars": 100, "max_elements": 5},
            0,
            ObserveAction,
        ),
        (
            "click",
            {"element_ref": "el-one", "document_revision": 2},
            2,
            ClickAction,
        ),
        (
            "type",
            {
                "element_ref": "el-one",
                "document_revision": 2,
                "text": "hello",
                "clear_first": True,
            },
            2,
            TypeAction,
        ),
        (
            "scroll",
            {
                "element_ref": None,
                "document_revision": 2,
                "dx": 10,
                "dy": -20,
            },
            2,
            ScrollAction,
        ),
        ("screenshot", {"full_page": False}, 0, ScreenshotAction),
    ],
)
def test_exact_action_conversion_and_complete_session(
    tmp_path: Path,
    action_type: str,
    payload: dict[str, Any],
    revision: int,
    expected: type[Any],
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        response = controller.execute_action(
            target,
            action_type,
            payload,
            revision,
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        driver = RecordingDriver.instances[0]
        assert isinstance(driver.actions[0], expected)
        assert driver.session_ids == [target]
        assert response["status"] == "success"
        assert base64.b64decode(
            response["screenshot_base64"], validate=True
        ) == b"shot"
        assert driver.thread_ids == [controller._owner_thread_id]
    finally:
        controller.shutdown()


def test_takeover_release_syncs_driver_and_returns_fresh_screenshot(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)

        screenshot = controller.sync_after_takeover(
            target,
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )

        driver = RecordingDriver.instances[0]
        assert screenshot == b"shot"
        assert driver.thread_ids == [controller._owner_thread_id]
    finally:
        controller.shutdown()


@pytest.mark.parametrize(
    ("action_type", "payload", "revision"),
    [
        ("navigate", {}, 0),
        ("navigate", {"url": "https://e", "extra": 1}, 0),
        (
            "observe",
            {"max_text_chars": True, "max_elements": 2},
            0,
        ),
        (
            "click",
            {"element_ref": "el", "document_revision": True},
            1,
        ),
        (
            "type",
            {
                "element_ref": "el",
                "document_revision": 1,
                "text": "x",
                "clear_first": 1,
            },
            1,
        ),
        (
            "scroll",
            {
                "element_ref": None,
                "document_revision": 1,
                "dx": 1_000_001,
                "dy": 0,
            },
            1,
        ),
        ("screenshot", {"full_page": 0}, 0),
    ],
)
def test_invalid_action_is_rejected_without_page_work(
    tmp_path: Path,
    action_type: str,
    payload: dict[str, Any],
    revision: int,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        with pytest.raises(
            RuntimeError, match="host_bridge_invalid_request"
        ):
            controller.execute_action(
                target,
                action_type,
                payload,
                revision,
                deadline_monotonic=time.monotonic() + 1,
                cancel_event=threading.Event(),
            )
        assert RecordingDriver.instances[0].actions == []
    finally:
        controller.shutdown()


def test_unknown_action_type_maps_unknown_capability(tmp_path: Path) -> None:
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        with pytest.raises(RuntimeError, match="unknown_capability"):
            controller.execute_action(
                target,
                "raw_cdp",
                {},
                0,
                deadline_monotonic=time.monotonic() + 1,
                cancel_event=threading.Event(),
            )
    finally:
        controller.shutdown()


def test_cross_target_id_is_denied_and_targets_are_isolated(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, playwright = _recording_controller(tmp_path)
    try:
        first = controller.create_target(PROFILE_REF)
        second = controller.create_target("bp-host_cdp-fedcba987654")
        assert first != second
        assert len(playwright.chromium.browsers) == 2
        assert (
            playwright.chromium.browsers[0].contexts[0]
            is not playwright.chromium.browsers[1].contexts[0]
        )
        with pytest.raises(TargetClosed):
            controller.get_state(
                "target-forged",
                deadline_monotonic=time.monotonic() + 1,
                cancel_event=threading.Event(),
            )
    finally:
        controller.shutdown()


class BlockingDriver(RecordingDriver):
    entered = threading.Event()
    exited = threading.Event()
    active = 0
    peak = 0

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        type(self).entered.set()
        try:
            await asyncio.sleep(10)
            return await super().execute_action(session_id, action)
        finally:
            type(self).active -= 1
            type(self).exited.set()


class CancellationResistantDriver(RecordingDriver):
    active = 0
    cancelled = threading.Event()
    resolved = threading.Event()

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        type(self).active += 1
        try:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                type(self).cancelled.set()
                await asyncio.sleep(0.1)
            return await super().execute_action(session_id, action)
        finally:
            type(self).active -= 1
            type(self).resolved.set()

    async def get_state(self, session_id: str) -> BrowserState:
        type(self).active += 1
        try:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                type(self).cancelled.set()
                await asyncio.sleep(0.1)
            return await super().get_state(session_id)
        finally:
            type(self).active -= 1
            type(self).resolved.set()


class PermanentlyResistantDriver(RecordingDriver):
    entered = threading.Event()
    release = threading.Event()

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        type(self).entered.set()
        while not type(self).release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                continue
        return await super().execute_action(session_id, action)

class GateDriver(RecordingDriver):
    entered_count = 0
    entered = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        type(self).entered_count += 1
        type(self).active += 1
        type(self).peak = max(type(self).peak, type(self).active)
        type(self).entered.set()
        try:
            while not type(self).release.is_set():
                await asyncio.sleep(0.005)
            return await super().execute_action(session_id, action)
        finally:
            type(self).active -= 1


def _click(
    controller: HostChromeController,
    target: str,
    *,
    timeout: float = 1,
) -> dict[str, Any]:
    return controller.execute_action(
        target,
        "click",
        {"element_ref": "el", "document_revision": 0},
        0,
        deadline_monotonic=time.monotonic() + timeout,
        cancel_event=threading.Event(),
    )


def test_same_target_serializes_and_queued_timeout_has_no_side_effect(
    tmp_path: Path,
) -> None:
    GateDriver.instances.clear()
    GateDriver.entered_count = 0
    GateDriver.entered.clear()
    GateDriver.release.clear()
    GateDriver.active = 0
    GateDriver.peak = 0
    controller, _ = _recording_controller(
        tmp_path, driver_factory=GateDriver
    )
    first_result: list[dict[str, Any]] = []
    try:
        target = controller.create_target(PROFILE_REF)
        first = threading.Thread(
            target=lambda: first_result.append(
                _click(controller, target, timeout=1)
            )
        )
        first.start()
        assert GateDriver.entered.wait(timeout=0.5)
        second = _click(controller, target, timeout=0.03)
        assert second["error_code"] == "host_bridge_timeout"
        assert GateDriver.entered_count == 1
        assert GateDriver.peak == 1
        GateDriver.release.set()
        first.join(timeout=1)
        assert first_result[0]["status"] == "success"
    finally:
        GateDriver.release.set()
        controller.shutdown()


def test_different_targets_can_execute_concurrently(tmp_path: Path) -> None:
    GateDriver.instances.clear()
    GateDriver.entered_count = 0
    GateDriver.entered.clear()
    GateDriver.release.clear()
    GateDriver.active = 0
    GateDriver.peak = 0
    controller, _ = _recording_controller(
        tmp_path, driver_factory=GateDriver, max_in_flight_tasks=2
    )
    results: list[dict[str, Any]] = []
    try:
        first = controller.create_target(PROFILE_REF)
        second = controller.create_target("bp-host_cdp-fedcba987654")
        threads = [
            threading.Thread(
                target=lambda target=target: results.append(
                    _click(controller, target, timeout=1)
                )
            )
            for target in (first, second)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 0.5
        while GateDriver.entered_count < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert GateDriver.entered_count == 2
        assert GateDriver.peak == 2
        GateDriver.release.set()
        for thread in threads:
            thread.join(timeout=1)
        assert [item["status"] for item in results] == [
            "success",
            "success",
        ]
    finally:
        GateDriver.release.set()
        controller.shutdown()


def test_total_queued_and_in_flight_capacity_is_bounded(
    tmp_path: Path,
) -> None:
    GateDriver.instances.clear()
    GateDriver.entered_count = 0
    GateDriver.entered.clear()
    GateDriver.release.clear()
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=GateDriver,
        max_queued_tasks=1,
        max_in_flight_tasks=1,
    )
    outcomes: list[Any] = []
    try:
        target = controller.create_target(PROFILE_REF)
        workers = [
            threading.Thread(
                target=lambda: outcomes.append(
                    _click(controller, target, timeout=1)
                )
            )
            for _ in range(2)
        ]
        workers[0].start()
        assert GateDriver.entered.wait(timeout=0.5)
        workers[1].start()
        deadline = time.monotonic() + 0.5
        while (
            getattr(controller._admission, "_value", 1) != 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        with pytest.raises(RuntimeError, match="host_bridge_busy"):
            _click(controller, target)
        GateDriver.release.set()
        for worker in workers:
            worker.join(timeout=1)
        assert len(outcomes) == 2
    finally:
        GateDriver.release.set()
        controller.shutdown()


def test_concurrent_create_submissions_share_bounded_admission(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    playwright = FakePlaywright()
    playwright.chromium = HungChromium()
    popen_count = 0
    first_popen = threading.Event()

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        nonlocal popen_count
        profile_arg = next(
            item for item in argv if item.startswith("--user-data-dir=")
        )
        profile = Path(profile_arg.split("=", 1)[1])
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        popen_count += 1
        first_popen.set()
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.15,
            max_queued_tasks=1,
            max_in_flight_tasks=1,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        platform_system=lambda: "Darwin",
    )
    outcomes: list[Any] = []

    def create(ref: str) -> None:
        try:
            outcomes.append(controller.create_target(ref))
        except Exception as exc:
            outcomes.append(exc)

    workers = [
        threading.Thread(target=create, args=(ref,))
        for ref in (
            PROFILE_REF,
            "bp-host_cdp-fedcba987654",
        )
    ]
    try:
        for worker in workers:
            worker.start()
        assert first_popen.wait(timeout=0.5)
        time.sleep(0.03)
        assert popen_count == 1
        with pytest.raises(RuntimeError, match="host_bridge_busy"):
            controller.create_target("bp-host_cdp-aabbccddeeff")
        for worker in workers:
            worker.join(timeout=1)
        assert len(outcomes) == 2
        assert all(
            isinstance(item, RuntimeError) for item in outcomes
        )
    finally:
        controller.shutdown()


def test_action_in_flight_prevents_create_from_reaching_popen(
    tmp_path: Path,
) -> None:
    GateDriver.instances.clear()
    GateDriver.entered_count = 0
    GateDriver.entered.clear()
    GateDriver.release.clear()
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=GateDriver,
        max_queued_tasks=2,
        max_in_flight_tasks=1,
    )
    original_popen = controller._popen_factory
    popen_count = 0

    def counted_popen(*args: Any, **kwargs: Any) -> Any:
        nonlocal popen_count
        popen_count += 1
        return original_popen(*args, **kwargs)

    controller._popen_factory = counted_popen
    action_result: list[Any] = []
    create_result: list[Any] = []
    try:
        target = controller.create_target(PROFILE_REF)
        popen_before = popen_count
        action = threading.Thread(
            target=lambda: action_result.append(
                _click(controller, target, timeout=1)
            )
        )
        action.start()
        assert GateDriver.entered.wait(timeout=0.5)
        create = threading.Thread(
            target=lambda: create_result.append(
                controller.create_target("bp-host_cdp-fedcba987654")
            )
        )
        create.start()
        time.sleep(0.03)
        assert popen_count == popen_before
        GateDriver.release.set()
        action.join(timeout=1)
        create.join(timeout=1)
        assert action_result[0]["status"] == "success"
        assert create_result[0].startswith("target-")
    finally:
        GateDriver.release.set()
        controller.shutdown()


def test_deadline_cancels_and_acknowledges_no_background_action(
    tmp_path: Path,
) -> None:
    BlockingDriver.instances.clear()
    BlockingDriver.entered.clear()
    BlockingDriver.exited.clear()
    controller, _ = _recording_controller(
        tmp_path, driver_factory=BlockingDriver
    )
    try:
        target = controller.create_target(PROFILE_REF)
        response = controller.execute_action(
            target,
            "click",
            {"element_ref": "el", "document_revision": 0},
            0,
            deadline_monotonic=time.monotonic() + 0.03,
            cancel_event=threading.Event(),
        )
        assert response["error_code"] == "action_outcome_unknown"
        assert BlockingDriver.entered.is_set()
        assert BlockingDriver.exited.is_set()
        assert BlockingDriver.active == 0
    finally:
        controller.shutdown()


def test_cancellation_resistant_action_forces_target_closed_before_return(
    tmp_path: Path,
) -> None:
    CancellationResistantDriver.cancelled.clear()
    CancellationResistantDriver.resolved.clear()
    CancellationResistantDriver.active = 0
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=CancellationResistantDriver,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    try:
        target = controller.create_target(PROFILE_REF)
        process = controller._targets[target].process
        started = time.monotonic()
        response = _click(controller, target, timeout=0.02)
        assert response["error_code"] == "action_outcome_unknown"
        assert time.monotonic() - started >= 0.09
        assert CancellationResistantDriver.cancelled.is_set()
        assert CancellationResistantDriver.resolved.is_set()
        assert CancellationResistantDriver.active == 0
        assert process.poll() is not None
        with pytest.raises(TargetClosed):
            controller.get_state(
                target,
                deadline_monotonic=time.monotonic() + 1,
                cancel_event=threading.Event(),
            )
    finally:
        controller.shutdown()


def test_cancellation_resistant_state_is_resolved_before_timeout_returns(
    tmp_path: Path,
) -> None:
    CancellationResistantDriver.cancelled.clear()
    CancellationResistantDriver.resolved.clear()
    CancellationResistantDriver.active = 0
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=CancellationResistantDriver,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    try:
        target = controller.create_target(PROFILE_REF)
        response = controller.get_state(
            target,
            deadline_monotonic=time.monotonic() + 0.02,
            cancel_event=threading.Event(),
        )
        assert response["error_code"] == "host_bridge_timeout"
        assert CancellationResistantDriver.resolved.is_set()
        assert CancellationResistantDriver.active == 0
        assert target not in controller._targets
    finally:
        controller.shutdown()


def test_permanently_resistant_action_returns_with_hard_bound_and_unusable_target(
    tmp_path: Path,
) -> None:
    PermanentlyResistantDriver.entered.clear()
    PermanentlyResistantDriver.release.clear()
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=PermanentlyResistantDriver,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    outcome: list[Any] = []
    target = controller.create_target(PROFILE_REF)
    process = controller._targets[target].process

    def act() -> None:
        outcome.append(_click(controller, target, timeout=0.02))

    worker = threading.Thread(target=act)
    worker.start()
    assert PermanentlyResistantDriver.entered.wait(timeout=0.5)
    worker.join(timeout=0.3)
    assert not worker.is_alive()
    assert outcome[0]["error_code"] == "action_outcome_unknown"
    assert process.poll() is not None
    assert controller.healthy is False
    PermanentlyResistantDriver.release.set()
    controller.shutdown()


def test_preexecution_timeout_is_stable_and_side_effect_free(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        response = controller.execute_action(
            target,
            "click",
            {"element_ref": "el", "document_revision": 0},
            0,
            deadline_monotonic=time.monotonic() - 1,
            cancel_event=threading.Event(),
        )
        assert response["error_code"] == "host_bridge_timeout"
        assert RecordingDriver.instances[0].actions == []
    finally:
        controller.shutdown()


def test_explicit_screenshot_without_fresh_bytes_is_unavailable(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        RecordingDriver.instances[0].screenshot = None
        response = controller.execute_action(
            target,
            "screenshot",
            {"full_page": False},
            0,
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        assert response == {
            "action_type": "screenshot",
            "status": "error",
            "error_code": "screenshot_unavailable",
            "document_revision": 0,
        }
    finally:
        controller.shutdown()


def test_other_success_without_screenshot_gets_warning(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    try:
        target = controller.create_target(PROFILE_REF)
        RecordingDriver.instances[0].screenshot = None
        response = controller.execute_action(
            target,
            "observe",
            {"max_text_chars": 100, "max_elements": 5},
            0,
            deadline_monotonic=time.monotonic() + 1,
            cancel_event=threading.Event(),
        )
        assert response["status"] == "success"
        assert response["warning_code"] == "screenshot_unavailable"
        assert "screenshot_base64" not in response
    finally:
        controller.shutdown()


def test_close_orders_page_context_browser_then_process_and_retains_profile(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, playwright = _recording_controller(tmp_path)
    profile = tmp_path / "profiles" / PROFILE_REF
    try:
        target_id = controller.create_target(PROFILE_REF)
        target = controller._targets[target_id]
        process = target.process
        sentinel = profile / "persistent-login-state"
        sentinel.write_text("keep", encoding="utf-8")
        controller.close_target(target_id)
        assert RecordingDriver.instances[0].closed is True
        browser = playwright.chromium.browsers[0]
        assert browser.page.closed is True
        assert browser.contexts[0].closed is True
        assert browser.closed is True
        assert process.terminated == 1
        assert process.waited >= 1
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert profile.is_dir()
        assert not (profile / "DevToolsActivePort").exists()
        controller.close_target(target_id)
    finally:
        controller.shutdown()


class StubbornProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def terminate(self) -> None:
        self.terminated += 1
        self.events.append("terminate")

    def kill(self) -> None:
        self.killed += 1
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waited += 1
        self.events.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("Chrome", timeout)
        return self.returncode


def test_process_close_escalates_terminate_wait_kill_wait(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = StubbornProcess()
    playwright = FakePlaywright()

    def popen(argv: list[str], **_: Any) -> StubbornProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.5,
            process_shutdown_timeout_seconds=0.01,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        driver_factory=RecordingDriver,
        platform_system=lambda: "Darwin",
    )
    try:
        target = controller.create_target(PROFILE_REF)
        controller.close_target(target)
        assert process.events == ["terminate", "wait", "kill", "wait"]
    finally:
        controller.shutdown()


def test_port_remove_error_still_releases_profile_lock_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(tmp_path)
    target = controller.create_target(PROFILE_REF)
    real_unlink = controller_module.os.unlink

    def failing_unlink(
        path: Any, *args: Any, **kwargs: Any
    ) -> None:
        if path == "DevToolsActivePort":
            raise PermissionError("redacted")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(controller_module.os, "unlink", failing_unlink)
    try:
        controller.close_target(target)
        assert target not in controller._targets
        profile_fd = os.open(
            tmp_path / "profiles" / PROFILE_REF, os.O_RDONLY
        )
        try:
            fcntl.flock(
                profile_fd, fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        finally:
            os.close(profile_fd)
    finally:
        controller.shutdown()


class ExplodingDriver(RecordingDriver):
    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        raise RuntimeError("/secret/profile 127.0.0.1:49152")


class HungNormalCloseDriver(RecordingDriver):
    close_started = threading.Event()
    release = threading.Event()

    async def close_session(self, session_id: str) -> None:
        type(self).close_started.set()
        while not type(self).release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                continue


def test_hung_normal_close_is_hard_bounded_and_releases_native_resources(
    tmp_path: Path,
) -> None:
    HungNormalCloseDriver.close_started.clear()
    HungNormalCloseDriver.release.clear()
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=HungNormalCloseDriver,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    target = controller.create_target(PROFILE_REF)
    process = controller._targets[target].process
    outcome: list[Any] = []

    def close() -> None:
        try:
            controller.close_target(target)
            outcome.append(None)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=close)
    worker.start()
    assert HungNormalCloseDriver.close_started.wait(timeout=0.5)
    worker.join(timeout=0.3)
    assert not worker.is_alive()
    assert process.poll() is not None
    assert controller.healthy is False
    HungNormalCloseDriver.release.set()
    controller.shutdown()


def test_force_and_owner_close_share_exactly_once_native_cleanup(
    tmp_path: Path,
) -> None:
    HungNormalCloseDriver.close_started.clear()
    HungNormalCloseDriver.release.clear()
    controller, _ = _recording_controller(
        tmp_path,
        driver_factory=HungNormalCloseDriver,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    target_id = controller.create_target(PROFILE_REF)
    target = controller._targets[target_id]
    process = target.process
    released_fd = target.profile.fd
    outcome: list[BaseException | None] = []

    def close() -> None:
        try:
            controller.close_target(target_id)
            outcome.append(None)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=close)
    worker.start()
    opened: list[int] = []
    try:
        assert HungNormalCloseDriver.close_started.wait(timeout=0.5)
        controller._force_native_cleanup(target_id)
        for _ in range(16):
            opened.append(os.open(os.devnull, os.O_RDONLY))
            if released_fd in opened:
                break
        assert released_fd in opened
        HungNormalCloseDriver.release.set()
        worker.join(timeout=0.5)
        assert not worker.is_alive()
        assert len(outcome) == 1
        time.sleep(0.05)
        os.fstat(released_fd)
        assert process.terminated == 1
        assert process.waited == 1
    finally:
        HungNormalCloseDriver.release.set()
        worker.join(timeout=1)
        controller.shutdown()
        for fd in opened:
            os.close(fd)


def test_target_disappearance_maps_target_closed_and_other_error_is_redacted(
    tmp_path: Path,
) -> None:
    RecordingDriver.instances.clear()
    controller, _ = _recording_controller(
        tmp_path, driver_factory=ExplodingDriver
    )
    try:
        target_id = controller.create_target(PROFILE_REF)
        with pytest.raises(RuntimeError) as captured:
            _click(controller, target_id)
        assert str(captured.value) == "target_unavailable"
        assert "secret" not in str(captured.value)
        controller._targets[target_id].process.returncode = -9
        with pytest.raises(TargetClosed):
            controller.get_state(
                target_id,
                deadline_monotonic=time.monotonic() + 1,
                cancel_event=threading.Event(),
            )
    finally:
        controller.shutdown()


def test_shutdown_cancels_queued_action_and_stops_owner_thread(
    tmp_path: Path,
) -> None:
    BlockingDriver.instances.clear()
    BlockingDriver.entered.clear()
    BlockingDriver.exited.clear()
    controller, _ = _recording_controller(
        tmp_path, driver_factory=BlockingDriver
    )
    target = controller.create_target(PROFILE_REF)
    outcome: list[Any] = []

    def run_action() -> None:
        try:
            outcome.append(_click(controller, target, timeout=10))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run_action)
    worker.start()
    assert BlockingDriver.entered.wait(timeout=0.5)
    assert controller.shutdown() is True
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert BlockingDriver.exited.is_set()
    assert not controller.owner_thread_alive
    assert len(outcome) == 1
    assert controller.shutdown() is True


def test_shutdown_returns_false_for_internal_failure_without_exception(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    controller._healthy = False

    assert controller.shutdown() is False
    assert controller.shutdown() is False


def test_hung_playwright_stop_keeps_shutdown_bounded_and_unhealthy(
    tmp_path: Path,
) -> None:
    controller, playwright = _recording_controller(
        tmp_path,
        cancellation_ack_timeout_seconds=0.02,
        process_shutdown_timeout_seconds=0.01,
    )
    target = controller.create_target(PROFILE_REF)
    controller.close_target(target)
    stop_started = threading.Event()
    release = threading.Event()

    async def hung_stop() -> None:
        stop_started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                continue

    playwright.stop = hung_stop  # type: ignore[method-assign]
    worker = threading.Thread(target=controller.shutdown)
    worker.start()
    try:
        assert stop_started.wait(timeout=0.5)
        worker.join(timeout=0.3)
        assert not worker.is_alive()
        assert controller._healthy is False
    finally:
        release.set()
        worker.join(timeout=1)
        controller._thread.join(timeout=0.5)


def test_shutdown_awaits_in_progress_create_cleanup(tmp_path: Path) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = StubbornProcess()
    playwright = FakePlaywright()
    playwright.chromium = HungChromium()
    launched = threading.Event()

    def popen(argv: list[str], **_: Any) -> StubbornProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        launched.set()
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=2,
            process_shutdown_timeout_seconds=0.01,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        platform_system=lambda: "Darwin",
        active_profile_detector=lambda path: False,
    )
    outcome: list[Any] = []

    def create() -> None:
        try:
            outcome.append(controller.create_target(PROFILE_REF))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=create)
    worker.start()
    assert launched.wait(timeout=0.5)
    controller.shutdown()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], RuntimeError)
    assert process.events == ["terminate", "wait", "kill", "wait"]
    assert not controller.owner_thread_alive
    controller.shutdown()


def test_permanently_resistant_startup_cannot_retain_native_resources(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = FakeProcess()
    PermanentlyResistantStarter.entered.clear()

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.08,
            process_shutdown_timeout_seconds=0.01,
            cancellation_ack_timeout_seconds=0.02,
        ),
        popen_factory=popen,
        playwright_factory=PermanentlyResistantStarter,
        platform_system=lambda: "Darwin",
    )
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="target_unavailable"):
        controller.create_target(PROFILE_REF)
    assert time.monotonic() - started < 0.5
    assert PermanentlyResistantStarter.entered.is_set()
    assert process.terminated == 1
    assert controller._targets == {}
    assert controller._provisionals == {}
    profile_fd = os.open(profiles / PROFILE_REF, os.O_RDONLY)
    try:
        fcntl.flock(profile_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(profile_fd)
    started = time.monotonic()
    controller.shutdown()
    assert time.monotonic() - started < 0.5
    assert not controller.owner_thread_alive


def test_invalid_owned_port_file_removed_after_failed_launch(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = FakeProcess()

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "not-a-port\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.05,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
    )
    try:
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
        assert process.terminated == 1
        assert not (
            profiles / PROFILE_REF / "DevToolsActivePort"
        ).exists()
    finally:
        controller.shutdown()


def test_startup_deadline_covers_hung_cdp_connect_and_confirms_cleanup(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = StubbornProcess()
    playwright = FakePlaywright()
    playwright.chromium = HungChromium()

    def popen(argv: list[str], **_: Any) -> StubbornProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.03,
            process_shutdown_timeout_seconds=0.01,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        platform_system=lambda: "Darwin",
    )
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
        assert time.monotonic() - started < 0.5
        assert process.events == ["terminate", "wait", "kill", "wait"]
        assert not (
            profiles / PROFILE_REF / "DevToolsActivePort"
        ).exists()
        probe_fd = os.open(profiles / PROFILE_REF, os.O_RDONLY)
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe_fd)
    finally:
        controller.shutdown()


class HungContextBrowser(FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.contexts = []

    async def new_context(self) -> FakeContext:
        await asyncio.sleep(10)
        return FakeContext(FakePage())


class HungPageContext(FakeContext):
    def __init__(self) -> None:
        super().__init__(FakePage())
        self.pages = []

    async def new_page(self) -> FakePage:
        await asyncio.sleep(10)
        return FakePage()


class HungClosePage(FakePage):
    close_started = threading.Event()

    async def close(self) -> None:
        type(self).close_started.set()
        await asyncio.sleep(10)


class HungCloseBrowser(FakeBrowser):
    def __init__(self) -> None:
        self.page = HungClosePage()
        self.contexts = [FakeContext(self.page)]
        self.closed = False


@pytest.mark.parametrize("stage", ["context", "page"])
def test_startup_deadline_covers_context_and_page_setup(
    tmp_path: Path, stage: str
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = FakeProcess()
    playwright = FakePlaywright()
    browser = (
        HungContextBrowser()
        if stage == "context"
        else FakeBrowser()
    )
    if stage == "page":
        browser.contexts = [HungPageContext()]
    playwright.chromium.browser = browser

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.03,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        platform_system=lambda: "Darwin",
    )
    try:
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
        assert process.poll() is not None
    finally:
        controller.shutdown()


def test_startup_cleanup_hung_page_close_still_stops_process_and_releases_fds(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    process = StubbornProcess()
    playwright = FakePlaywright()
    playwright.chromium.browser = HungCloseBrowser()
    HungClosePage.close_started.clear()

    def popen(argv: list[str], **_: Any) -> StubbornProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    def slow_driver(**kwargs: Any) -> RecordingDriver:
        time.sleep(0.04)
        return RecordingDriver(**kwargs)

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.02,
            process_shutdown_timeout_seconds=0.01,
            cancellation_ack_timeout_seconds=0.02,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
        driver_factory=slow_driver,
        platform_system=lambda: "Darwin",
        active_profile_detector=lambda path: False,
    )
    try:
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
        assert HungClosePage.close_started.is_set()
        assert process.events == ["terminate", "wait", "kill", "wait"]
        profile_fd = os.open(profiles / PROFILE_REF, os.O_RDONLY)
        try:
            fcntl.flock(profile_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(profile_fd)
    finally:
        controller.shutdown()


def test_root_rename_to_symlink_same_directory_rejected_after_popen(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    moved = tmp_path / "profiles-moved"
    _secure_file(executable)
    _secure_root(profiles)
    process = FakeProcess()

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profiles.rename(moved)
        profiles.symlink_to(moved, target_is_directory=True)
        (moved / PROFILE_REF / "DevToolsActivePort").write_text(
            "49152\n/devtools/browser/test-id", encoding="ascii"
        )
        return process

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
    )
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_invalid"):
            controller.create_target(PROFILE_REF)
        assert process.poll() is not None
        assert not controller._targets
    finally:
        controller.shutdown()


def test_confirmed_absent_detector_allows_stale_port_replacement(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    profile = profiles / PROFILE_REF
    _secure_file(executable)
    _secure_root(profiles)
    profile.mkdir(mode=0o700)
    stale = profile / "DevToolsActivePort"
    stale.write_text(
        "49152\n/devtools/browser/stale", encoding="ascii"
    )

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        assert not stale.exists()
        stale.write_text(
            "49153\n/devtools/browser/new", encoding="ascii"
        )
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
        active_profile_detector=lambda path: False,
    )
    try:
        target = controller.create_target(PROFILE_REF)
        assert target.startswith("target-")
    finally:
        controller.shutdown()


@pytest.mark.parametrize("detector_result", [True, RuntimeError("unknown")])
def test_stale_port_requires_proof_no_unknown_active_profile_process(
    tmp_path: Path, detector_result: Any
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    profile = profiles / PROFILE_REF
    _secure_file(executable)
    _secure_root(profiles)
    profile.mkdir(mode=0o700)
    stale = profile / "DevToolsActivePort"
    stale.write_text(
        "49152\n/devtools/browser/stale", encoding="ascii"
    )
    popen_calls = 0

    def detector(path: Path) -> bool:
        if isinstance(detector_result, Exception):
            raise detector_result
        return detector_result

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal popen_calls
        popen_calls += 1
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
        active_profile_detector=detector,
    )
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_busy"):
            controller.create_target(PROFILE_REF)
        assert stale.exists()
        assert popen_calls == 0
    finally:
        controller.shutdown()


@pytest.mark.parametrize("detector_result", [True, RuntimeError("unknown")])
def test_every_launch_requires_proof_no_active_process_without_port_file(
    tmp_path: Path, detector_result: Any
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)
    popen_calls = 0

    def detector(path: Path) -> bool:
        if isinstance(detector_result, Exception):
            raise detector_result
        return detector_result

    def popen(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal popen_calls
        popen_calls += 1
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
        active_profile_detector=detector,
    )
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_busy"):
            controller.create_target(PROFILE_REF)
        assert popen_calls == 0
    finally:
        controller.shutdown()


@pytest.mark.parametrize(
    "command",
    [
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--user-data-dir=/tmp/Application Support/n-agent profiles/"
            f"{PROFILE_REF} --no-first-run"
        ),
        (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--user-data-dir /tmp/Application Support/n-agent profiles/"
            f"{PROFILE_REF} --no-first-run"
        ),
    ],
)
def test_default_detector_matches_unquoted_ps_argv_with_spaces(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    class Completed:
        stdout = command

    monkeypatch.setattr(
        controller_module.subprocess,
        "run",
        lambda *args, **kwargs: Completed(),
    )
    profile = Path(
        f"/tmp/Application Support/n-agent profiles/{PROFILE_REF}"
    )
    assert (
        HostChromeController._default_active_profile_detector(profile)
        is True
    )

    Completed.stdout = command.replace(
        PROFILE_REF, f"{PROFILE_REF}-other"
    )
    assert (
        HostChromeController._default_active_profile_detector(profile)
        is False
    )


@pytest.mark.parametrize(
    "contents",
    [
        "0\n/devtools/browser/test-id",
        "65536\n/devtools/browser/test-id",
        "49152\n/devtools/page/not-browser",
        "49152\n/devtools/browser/test-id\nunexpected",
    ],
)
def test_active_port_requires_exact_dynamic_port_and_websocket_format(
    tmp_path: Path, contents: str
) -> None:
    executable = tmp_path / "Chrome"
    profiles = tmp_path / "profiles"
    _secure_file(executable)
    _secure_root(profiles)

    def popen(argv: list[str], **_: Any) -> FakeProcess:
        profile = profiles / PROFILE_REF
        (profile / "DevToolsActivePort").write_text(
            contents, encoding="ascii"
        )
        return FakeProcess()

    controller = HostChromeController(
        HostChromeControllerConfig(
            profile_root=profiles,
            chrome_executable=executable,
            startup_timeout_seconds=0.05,
        ),
        popen_factory=popen,
        playwright_factory=lambda: FakePlaywrightStarter(FakePlaywright()),
        platform_system=lambda: "Darwin",
    )
    try:
        with pytest.raises(RuntimeError, match="target_unavailable"):
            controller.create_target(PROFILE_REF)
    finally:
        controller.shutdown()


def test_orphan_singleton_is_never_killed_or_recursively_removed(
    tmp_path: Path,
) -> None:
    controller, _, process, _ = _controller(tmp_path)
    profile = tmp_path / "profiles" / PROFILE_REF
    profile.mkdir(mode=0o700)
    singleton = profile / "SingletonLock"
    singleton.write_text("orphan", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="host_browser_profile_busy"):
            controller.create_target(PROFILE_REF)
        assert process.terminated == 0
        assert process.killed == 0
        assert singleton.exists()
        assert (tmp_path / "profiles").is_dir()
    finally:
        controller.shutdown()


def test_detached_delayed_exception_is_retrieved_and_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    completed = threading.Event()

    async def schedule() -> None:
        async def delayed_failure() -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError(
                "/secret/profile 127.0.0.1:49152"
            )

        task = asyncio.create_task(delayed_failure())
        controller._detached_tasks.add(task)
        task.add_done_callback(controller._consume_detached_task)
        task.add_done_callback(lambda _: completed.set())

    try:
        asyncio.run_coroutine_threadsafe(
            schedule(), controller._loop
        ).result(timeout=0.5)
        assert completed.wait(timeout=0.5)
        gc.collect()
        captured = capsys.readouterr()
        combined = captured.out + captured.err + caplog.text
        assert "Task exception was never retrieved" not in combined
        assert "secret" not in combined
        assert "49152" not in combined
    finally:
        controller.shutdown()
