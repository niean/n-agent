"""Tests for the Container browser backend (T11).

Uses a FAKE CDP browser/context/page and the real PlaywrightBrowserBackend
driver (with a stub page). No real browser is connected and the Playwright
SDK is NOT required to be installed in this environment -- the CDP
connection is monkeypatched to return fakes.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from app.domain.browser import (
    BrowserActionResult,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
)
from app.infrastructure.browser.container_backend import (
    ContainerBackendError,
    ContainerBrowserBackend,
)
from app.infrastructure.browser.url_safety import UrlVerifier


# ---------------------------------------------------------------------------
# Fake CDP connection objects (browser / context / page)
# ---------------------------------------------------------------------------


class FakeCDPPage:
    """Fake Playwright Page for the CDP path.

    Implements enough of the PageProtocol surface for the
    PlaywrightBrowserBackend to operate on it (url, main_frame, title, goto,
    screenshot, close).
    """

    def __init__(
        self, url: str = "https://example.com/", title: str = "Example"
    ) -> None:
        self._url = url
        self._title = title
        self._closed = False
        self.goto_urls: list[str] = []
        self._main_frame = FakeFrame()

    @property
    def url(self) -> str:
        return self._url

    @property
    def main_frame(self) -> Any:
        return self._main_frame

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_urls.append(url)
        self._url = url
        return None

    async def screenshot(self, *, full_page: bool = False, type: str = "png") -> bytes:  # noqa: A002
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    async def close(self) -> None:
        self._closed = True


class FakeFrame:
    def __init__(self) -> None:
        self.evaluate_calls: list[str] = []

    async def query_selector_all(self, selector: str) -> list[Any]:
        return []

    async def evaluate(self, expression: str) -> Any:
        self.evaluate_calls.append(expression)
        # Return empty projection for observe.
        return []


class FakeCDPContext:
    """Fake Playwright BrowserContext."""

    def __init__(self) -> None:
        self.pages: list[FakeCDPPage] = []
        self.close_calls = 0

    async def new_page(self) -> FakeCDPPage:
        page = FakeCDPPage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.close_calls += 1


class FakeCDPBrowser:
    """Fake Playwright Browser (CDP connection)."""

    def __init__(self) -> None:
        self.contexts: list[FakeCDPContext] = []
        self.close_calls = 0

    async def new_context(self) -> FakeCDPContext:
        ctx = FakeCDPContext()
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.close_calls += 1


class FakePlaywright:
    """Fake Playwright SDK instance."""

    def __init__(self, browser: FakeCDPBrowser) -> None:
        self._browser = browser
        self.stop_calls = 0

    @property
    def chromium(self) -> Any:
        return self

    async def connect_over_cdp(self, endpoint: str) -> FakeCDPBrowser:
        return self._browser

    async def stop(self) -> None:
        self.stop_calls += 1


# ---------------------------------------------------------------------------
# Fixture: backend with monkeypatched CDP connection
# ---------------------------------------------------------------------------


class PassThroughUrlVerifier(UrlVerifier):
    """UrlVerifier that skips DNS resolution and always passes."""

    async def verify_url(self, url: str) -> str:
        return url

    async def verify_redirect(self, new_url: str) -> str:
        return new_url


def _make_backend(
    *,
    endpoint: str = "http://browser:9222",
    action_timeout_seconds: float = 30.0,
    takeover_ttl_seconds: int = 60,
    novnc_base_url: str = "http://browser:6080",
) -> tuple[ContainerBrowserBackend, FakeCDPBrowser, FakePlaywright]:
    """Create a ContainerBrowserBackend with fakes wired in.

    Returns (backend, fake_browser, fake_playwright) so tests can assert
    on connection/context/page lifecycle.
    """
    fake_browser = FakeCDPBrowser()
    fake_pw = FakePlaywright(fake_browser)

    backend = ContainerBrowserBackend(
        endpoint=endpoint,
        url_verifier=PassThroughUrlVerifier(),
        action_timeout_seconds=action_timeout_seconds,
        takeover_ttl_seconds=takeover_ttl_seconds,
        novnc_base_url=novnc_base_url,
    )
    # Monkeypatch the connection methods to avoid importing playwright.
    backend._start_playwright = lambda: _await_none(fake_pw)  # type: ignore[method-assign]
    backend._connect_over_cdp = lambda ep: _await_none(fake_browser)  # type: ignore[method-assign]
    return backend, fake_browser, fake_pw


async def _await_none(value: Any) -> Any:
    """Helper: return value as if an async method returned it."""
    return value


def _make_session(
    sid: str = "browser-session-1",
    profile_ref: str = "profile-abc",
    nagent_sid: str = "nagent-1",
) -> BrowserSession:
    return BrowserSession.create_for_container(sid, nagent_sid, profile_ref)


# ---------------------------------------------------------------------------
# Module import without playwright installed
# ---------------------------------------------------------------------------


def test_module_imports_without_playwright_installed():
    from app.infrastructure.browser import container_backend
    assert hasattr(container_backend, "ContainerBrowserBackend")


def test_constructor_requires_endpoint():
    with pytest.raises(ContainerBackendError):
        ContainerBrowserBackend(
            endpoint="",
            url_verifier=UrlVerifier(),
        )


# ---------------------------------------------------------------------------
# create_session / close_session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_connects_cdp_and_creates_context():
    backend, fake_browser, fake_pw = _make_backend()
    session = _make_session()

    await backend.create_session(session)

    assert backend.is_connected()
    # One context created for the session.
    assert len(fake_browser.contexts) == 1
    # One page created in the context.
    assert len(fake_browser.contexts[0].pages) == 1
    assert backend.has_session(session.id)
    assert backend.active_session_count() == 1


@pytest.mark.asyncio
async def test_create_session_is_idempotent_for_same_session():
    backend, fake_browser, _ = _make_backend()
    session = _make_session()

    await backend.create_session(session)
    await backend.create_session(session)

    # Only one context created despite double create.
    assert len(fake_browser.contexts) == 1
    assert backend.active_session_count() == 1


@pytest.mark.asyncio
async def test_create_session_multiple_sessions_get_separate_contexts():
    backend, fake_browser, _ = _make_backend()
    s1 = _make_session(sid="s1", profile_ref="p1")
    s2 = _make_session(sid="s2", profile_ref="p2")

    await backend.create_session(s1)
    await backend.create_session(s2)

    assert len(fake_browser.contexts) == 2
    assert backend.active_session_count() == 2


@pytest.mark.asyncio
async def test_close_session_closes_context_and_removes_session():
    backend, fake_browser, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)
    ctx = fake_browser.contexts[0]

    await backend.close_session(session.id)

    assert not backend.has_session(session.id)
    assert ctx.close_calls == 1


@pytest.mark.asyncio
async def test_close_session_disconnects_when_last_session_closed():
    backend, fake_browser, fake_pw = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    await backend.close_session(session.id)

    assert not backend.is_connected()
    assert fake_browser.close_calls == 1
    assert fake_pw.stop_calls == 1


@pytest.mark.asyncio
async def test_close_session_does_not_disconnect_when_other_sessions_active():
    backend, fake_browser, fake_pw = _make_backend()
    s1 = _make_session(sid="s1", profile_ref="p1")
    s2 = _make_session(sid="s2", profile_ref="p2")
    await backend.create_session(s1)
    await backend.create_session(s2)

    await backend.close_session(s1.id)

    assert backend.is_connected()
    assert fake_browser.close_calls == 0
    assert backend.active_session_count() == 1


@pytest.mark.asyncio
async def test_close_session_is_safe_for_unknown_session():
    backend, _, _ = _make_backend()
    # Should not raise.
    await backend.close_session("nonexistent-session")


@pytest.mark.asyncio
async def test_close_session_revokes_active_takeover_capability():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    cap_url = await backend.begin_takeover(session.id)
    assert cap_url is not None
    token = cap_url.split("token=")[-1]
    assert backend.capability_is_active(token)

    await backend.close_session(session.id)
    assert not backend.capability_is_active(token)


# ---------------------------------------------------------------------------
# execute_action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_action_navigate_delegates_to_driver():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    result = await backend.execute_action(
        session.id, NavigateAction(url="https://example.com/login")
    )
    assert isinstance(result, BrowserActionResult)
    assert result.action_type == "navigate"
    assert result.status == "success"
    # The driver's page should have been navigated.
    page = backend._sessions[session.id].page
    assert "https://example.com/login" in page.goto_urls


@pytest.mark.asyncio
async def test_execute_action_observe_delegates_to_driver():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    result = await backend.execute_action(
        session.id, ObserveAction(max_text_chars=200, max_elements=10)
    )
    assert result.action_type == "observe"
    assert result.status == "success"


@pytest.mark.asyncio
async def test_execute_action_screenshot_delegates_to_driver():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    result = await backend.execute_action(
        session.id, ScreenshotAction(full_page=False)
    )
    assert result.action_type == "screenshot"
    assert result.status == "success"


@pytest.mark.asyncio
async def test_execute_action_unknown_session_returns_error():
    backend, _, _ = _make_backend()
    result = await backend.execute_action(
        "nonexistent", NavigateAction(url="https://example.com/")
    )
    assert result.status == "error"
    assert result.error_code == "session_not_found"


@pytest.mark.asyncio
async def test_execute_action_timeout_returns_timeout_result():
    backend, _, _ = _make_backend(action_timeout_seconds=0.05)
    session = _make_session()
    await backend.create_session(session)

    # Patch the driver's execute_action to sleep longer than the timeout.
    original = backend._sessions[session.id].driver.execute_action

    async def slow_action(session_id: str, action: Any) -> BrowserActionResult:
        await asyncio.sleep(1.0)
        return BrowserActionResult(
            action_type="navigate", status="success"
        )

    backend._sessions[session.id].driver.execute_action = slow_action  # type: ignore[method-assign]

    result = await backend.execute_action(
        session.id, NavigateAction(url="https://example.com/")
    )
    assert result.status == "timeout"
    assert result.error_code == "browser_action_timeout"


@pytest.mark.asyncio
async def test_execute_action_serializes_via_per_session_lock():
    """Concurrent actions on the same session must serialize."""
    backend, _, _ = _make_backend(action_timeout_seconds=5.0)
    session = _make_session()
    await backend.create_session(session)

    call_order: list[str] = []

    async def slow_navigate(session_id: str, action: Any) -> BrowserActionResult:
        call_order.append(f"start:{action.url}")
        await asyncio.sleep(0.1)
        call_order.append(f"end:{action.url}")
        return BrowserActionResult(action_type="navigate", status="success")

    backend._sessions[session.id].driver.execute_action = slow_navigate  # type: ignore[method-assign]

    # Launch two concurrent actions.
    await asyncio.gather(
        backend.execute_action(
            session.id, NavigateAction(url="https://a.com/")
        ),
        backend.execute_action(
            session.id, NavigateAction(url="https://b.com/")
        ),
    )
    # The actions must NOT interleave: start/end pairs must be contiguous.
    # Find the two start entries and verify each is immediately followed by
    # its end.
    starts = [e for e in call_order if e.startswith("start:")]
    ends = [e for e in call_order if e.startswith("end:")]
    assert len(starts) == 2
    assert len(ends) == 2
    # For each start, the next entry must be its matching end (no interleaving).
    for i, s in enumerate(starts):
        url = s.split("start:")[1]
        next_entry = call_order[call_order.index(s) + 1]
        assert next_entry == f"end:{url}"


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_active_state_for_known_session():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    state = await backend.get_state(session.id)
    assert isinstance(state, BrowserState)
    assert state.status is BrowserSessionStatus.ACTIVE
    assert state.latest_screenshot_ref is None  # BrowserService fills this


@pytest.mark.asyncio
async def test_get_state_returns_closed_for_unknown_session():
    backend, _, _ = _make_backend()
    state = await backend.get_state("nonexistent")
    assert state.status is BrowserSessionStatus.CLOSED
    assert state.safe_url is None
    assert state.latest_screenshot_ref is None


# ---------------------------------------------------------------------------
# Takeover capability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_takeover_returns_novnc_url_with_token():
    backend, _, _ = _make_backend(novnc_base_url="http://browser:6080")
    session = _make_session()
    await backend.create_session(session)

    url = await backend.begin_takeover(session.id)
    assert url is not None
    assert url.startswith("http://browser:6080/vnc.html?token=")
    token = url.split("token=")[-1]
    assert token  # non-empty
    assert backend.capability_is_active(token)


@pytest.mark.asyncio
async def test_begin_takeover_returns_none_for_unknown_session():
    backend, _, _ = _make_backend()
    url = await backend.begin_takeover("nonexistent")
    assert url is None


@pytest.mark.asyncio
async def test_begin_takeover_returns_capability_token_without_novnc_base():
    backend, _, _ = _make_backend(novnc_base_url="")
    session = _make_session()
    await backend.create_session(session)

    url = await backend.begin_takeover(session.id)
    assert url is not None
    assert url.startswith("cap://")


@pytest.mark.asyncio
async def test_end_takeover_revokes_capability():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    url = await backend.begin_takeover(session.id)
    token = url.split("token=")[-1]  # type: ignore[union-attr]
    assert backend.capability_is_active(token)

    await backend.end_takeover(session.id)
    assert not backend.capability_is_active(token)


@pytest.mark.asyncio
async def test_end_takeover_safe_for_session_without_capability():
    backend, _, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)
    # No capability issued, should not raise.
    await backend.end_takeover(session.id)


@pytest.mark.asyncio
async def test_takeover_capability_bound_to_session():
    """end_takeover for session A must NOT revoke session B's capability."""
    backend, _, _ = _make_backend()
    s1 = _make_session(sid="s1", profile_ref="p1")
    s2 = _make_session(sid="s2", profile_ref="p2")
    await backend.create_session(s1)
    await backend.create_session(s2)

    url1 = await backend.begin_takeover(s1.id)
    url2 = await backend.begin_takeover(s2.id)
    token1 = url1.split("token=")[-1]  # type: ignore[union-attr]
    token2 = url2.split("token=")[-1]  # type: ignore[union-attr]
    assert token1 != token2

    # Revoke only s1's capability.
    await backend.end_takeover(s1.id)
    assert not backend.capability_is_active(token1)
    assert backend.capability_is_active(token2)


@pytest.mark.asyncio
async def test_takeover_capability_has_ttl():
    """Capability expires after takeover_ttl_seconds."""
    backend, _, _ = _make_backend(takeover_ttl_seconds=1)
    session = _make_session()
    await backend.create_session(session)

    url = await backend.begin_takeover(session.id)
    token = url.split("token=")[-1]  # type: ignore[union-attr]
    assert backend.capability_is_active(token)

    # Wait for TTL to expire.
    await asyncio.sleep(1.2)
    assert not backend.capability_is_active(token)


# ---------------------------------------------------------------------------
# Single context/page per session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_context_per_session():
    """Each session gets exactly one context and one page."""
    backend, fake_browser, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)

    # Calling create again does NOT create a second context.
    await backend.create_session(session)
    assert len(fake_browser.contexts) == 1
    assert len(fake_browser.contexts[0].pages) == 1


@pytest.mark.asyncio
async def test_close_then_recreate_session_creates_new_context():
    """After closing the last session and reconnecting, a new session can be created."""
    backend, fake_browser, _ = _make_backend()
    session = _make_session()
    await backend.create_session(session)
    assert len(fake_browser.contexts) == 1

    await backend.close_session(session.id)
    assert not backend.is_connected()

    # Re-create: backend reconnects and creates a new context.
    await backend.create_session(session)
    assert backend.has_session(session.id)
    assert backend.is_connected()
    # The fake browser accumulated a second context on reconnect.
    assert len(fake_browser.contexts) == 2
