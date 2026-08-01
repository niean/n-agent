"""Container browser backend (T11).

Connects to a containerized Chromium via Playwright ``connect_over_cdp``,
creates a dedicated browser context per session bound to the session's
profile_ref, and delegates action execution to the Playwright driver (T6).
The Playwright SDK is imported lazily inside :meth:`_ensure_connected`;
this module imports cleanly even when ``playwright`` is not installed,
which lets the test suite inject fakes via monkeypatching.

Takeover capability: issues short-lived, single-session noVNC capability
URLs bound to session/actor/TTL. :meth:`end_takeover` revokes the
capability immediately.

Security:
  - CDP/noVNC ports are container-network only (no host port mapping).
  - Action timeout enforced via :func:`asyncio.wait_for`.
  - Single context/page per session; per-session lock serializes page ops.
  - Profile_ref is an opaque key mapped to a persistent volume dir inside
    the container (NOT a host path).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.domain.browser import (
    BrowserActionResult,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
)
from app.infrastructure.browser.playwright_driver import PlaywrightBrowserBackend
from app.infrastructure.browser.container_profile_runtime import (
    ContainerProfileRuntimeClient,
)
from app.infrastructure.browser.url_safety import UrlVerifier

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright  # noqa: F401

logger = logging.getLogger(__name__)
_MIN_SCREENSHOT_BYTES = 1_024
_MAX_SCREENSHOT_BYTES = 16 * 1_024 * 1_024


class ContainerBackendError(RuntimeError):
    """Raised when the container browser backend encounters an error."""


@dataclass
class _SessionContext:
    """Per-session CDP context + page + driver bundle."""

    driver: PlaywrightBrowserBackend
    context: Any  # playwright BrowserContext
    page: Any  # playwright Page
    profile_ref: str
    runtime_id: str | None = None
    persistent_profile: bool = False
    runtime_browser: Any | None = None
    runtime_playwright: Any | None = None
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Capability:
    """Takeover capability bound to a session with TTL."""

    session_id: str
    token: str
    expires_at: datetime
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def active(self) -> bool:
        return not self.revoked and not self.expired


class ContainerBrowserBackend:
    """Container browser backend: CDP to containerized Chromium.

    Owns:
      - Lazy CDP connection to the container Chromium (shared across
        sessions).
      - Per-session browser context + page + PlaywrightBrowserBackend
        (one context/page per session, isolated by profile_ref).
      - Action timeout enforcement via :func:`asyncio.wait_for`.
      - Takeover capability issuance/revocation (short-lived, single-session,
        TTL-bound noVNC capability URLs).

    Does NOT own:
      - Session lifecycle / registry state (BrowserService owns this).
      - Screenshot persistence (BrowserService reads
        ``driver.last_screenshot_bytes()`` and persists via screenshot store).
      - Host port exposure (compose-level concern).
      - CPU/memory/pids/shm limits (compose-level concern).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        url_verifier: UrlVerifier,
        action_timeout_seconds: float = 30.0,
        navigation_timeout_seconds: float = 30.0,
        takeover_ttl_seconds: int = 60,
        novnc_base_url: str = "",
        profile_runtime_endpoint: str = "",
        max_screenshot_bytes: int = 1_048_576,
    ) -> None:
        if not endpoint or not endpoint.strip():
            raise ContainerBackendError(
                "container browser endpoint is required (browser_container_endpoint)"
            )
        self._endpoint = endpoint.strip()
        self._url_verifier = url_verifier
        self._action_timeout = float(action_timeout_seconds)
        self._navigation_timeout = float(navigation_timeout_seconds)
        self._takeover_ttl = int(takeover_ttl_seconds)
        self._novnc_base_url = (novnc_base_url or "").rstrip("/")
        self._runtime_client = (
            ContainerProfileRuntimeClient(profile_runtime_endpoint)
            if profile_runtime_endpoint.strip()
            else None
        )
        if (
            type(max_screenshot_bytes) is not int
            or not _MIN_SCREENSHOT_BYTES
            <= max_screenshot_bytes
            <= _MAX_SCREENSHOT_BYTES
        ):
            raise ContainerBackendError(
                "container screenshot limit invalid"
            )
        self._max_screenshot_bytes = max_screenshot_bytes
        # Shared CDP connection (lazy).
        self._playwright: "Playwright | None" = None
        self._browser: "Browser | None" = None
        # Per-session state.
        self._sessions: dict[str, _SessionContext] = {}
        self._sessions_guard = asyncio.Lock()
        # Takeover capabilities: token -> _Capability.
        self._capabilities: dict[str, _Capability] = {}
        self._capabilities_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # CDP connection management (lazy, shared)
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        """Lazily connect to the container Chromium via CDP. Idempotent."""
        if self._browser is not None:
            return
        self._playwright = await self._start_playwright()
        self._browser = await self._connect_over_cdp(self._endpoint)

    async def _start_playwright(self) -> "Playwright":
        """Lazy-import and start the Playwright SDK.

        Raises :class:`ContainerBackendError` if the SDK is unavailable.
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError as exc:
            raise ContainerBackendError(
                "playwright SDK is not installed; run `pip install playwright` "
                "and `playwright install chromium`"
            ) from exc
        return await async_playwright().start()

    async def _connect_over_cdp(self, endpoint: str) -> "Browser":
        """Connect to the container Chromium via CDP.

        Tests override this method to inject a fake browser without
        importing the Playwright SDK.
        """
        return await self._playwright.chromium.connect_over_cdp(endpoint)  # type: ignore[union-attr]

    async def _disconnect(self) -> None:
        """Close the shared CDP browser and stop Playwright."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.warning("CDP browser close failed", exc_info=True)
        self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.warning("playwright stop failed", exc_info=True)
        self._playwright = None

    # ------------------------------------------------------------------
    # BrowserBackend Protocol: create / close
    # ------------------------------------------------------------------

    async def create_session(self, session: BrowserSession) -> None:
        """Create a dedicated browser context + page for the session.

        Idempotent: if the session already exists, returns without error.
        The profile_ref is an opaque key; the persistent profile directory
        is managed inside the container (NOT a host path).
        """
        async with self._sessions_guard:
            if session.id in self._sessions:
                return
            runtime_id: str | None = None
            persistent_profile = self._runtime_client is not None
            if self._runtime_client is None:
                await self._ensure_connected()
                context = await self._browser.new_context()  # type: ignore[union-attr]
                page = await context.new_page()
            else:
                endpoint, runtime_id = await self._runtime_client.ensure_profile(
                    session.profile_ref
                )
                browser, playwright = await self._connect_profile_browser(endpoint)
                context, page = await self._persistent_page(browser)
            driver = PlaywrightBrowserBackend(
                url_verifier=self._url_verifier,
                default_timeout_seconds=self._navigation_timeout,
                max_screenshot_bytes=self._max_screenshot_bytes,
            )
            driver.attach_page(page, document_revision=session.document_revision)
            self._sessions[session.id] = _SessionContext(
                driver=driver,
                context=context,
                page=page,
                profile_ref=session.profile_ref,
                runtime_id=runtime_id,
                persistent_profile=persistent_profile,
                runtime_browser=browser if persistent_profile else None,
                runtime_playwright=playwright if persistent_profile else None,
            )

    async def close_session(self, session_id: str) -> None:
        """Close the per-session context/page and release resources.

        If no sessions remain, disconnects the shared CDP connection.
        Also revokes any active takeover capability for the session.
        """
        async with self._sessions_guard:
            ctx = self._sessions.pop(session_id, None)
            should_disconnect = not self._sessions
        if ctx is None:
            # Still revoke capabilities for the session id.
            await self._revoke_session_capabilities(session_id)
            return
        # Persistent profiles own their browser process and page lifecycle.
        # Closing either from this CDP client would discard the restorable tab
        # that must survive an n-agent/browser service reconnect.
        if ctx.persistent_profile:
            await self._revoke_session_capabilities(session_id)
            return
        # Close the driver (closes the page).
        try:
            await ctx.driver.close_session(session_id)
        except Exception:
            logger.warning(
                "driver close failed for session=%s", session_id, exc_info=True
            )
        # Close the browser context (releases the isolated profile).
        try:
            await ctx.context.close()
        except Exception:
            logger.warning(
                "context close failed for session=%s", session_id, exc_info=True
            )
        # Revoke any active takeover capability for this session.
        await self._revoke_session_capabilities(session_id)
        # If no sessions remain, disconnect the shared CDP connection.
        if should_disconnect:
            async with self._sessions_guard:
                if not self._sessions and self._browser is not None:
                    await self._disconnect()

    # ------------------------------------------------------------------
    # BrowserBackend Protocol: execute_action / get_state
    # ------------------------------------------------------------------

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        """Route the action to the per-session driver with timeout.

        Enforces action timeout via :func:`asyncio.wait_for`. Per-session
        lock prevents parallel page operations.
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            return BrowserActionResult(
                action_type=type(action).__name__,
                status="error",
                error_code="session_not_found",
            )
        async with ctx.page_lock:
            await self._rebind_persistent_profile(ctx)
            try:
                result = await asyncio.wait_for(
                    ctx.driver.execute_action(session_id, action),
                    timeout=self._action_timeout,
                )
                return result
            except asyncio.TimeoutError:
                return BrowserActionResult(
                    action_type=type(action).__name__,
                    status="timeout",
                    error_code="browser_action_timeout",
                )

    async def get_state(self, session_id: str) -> BrowserState:
        """Return the current browser state for the session.

        latest_screenshot_ref is None here; the BrowserService fills it
        from the screenshot store.
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            return BrowserState(
                safe_url=None,
                title=None,
                status=BrowserSessionStatus.CLOSED,
                document_revision=0,
                latest_screenshot_ref=None,
            )
        async with ctx.page_lock:
            await self._rebind_persistent_profile(ctx)
            return await ctx.driver.get_state(session_id)

    async def _connect_profile_browser(self, endpoint: str) -> tuple[Any, Any]:
        """Connect a dedicated CDP client to one profile-owned Chromium."""
        playwright = await self._start_playwright()
        # Keep the Playwright object alive through the Browser object.  The
        # session context owns it indirectly until process restart/teardown.
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        return browser, playwright

    async def _persistent_page(self, browser: Any) -> tuple[Any, Any]:
        contexts = list(browser.contexts)
        if not contexts:
            raise ContainerBackendError("persistent_profile_context_missing")
        context = contexts[0]
        pages = list(context.pages)
        page = pages[0] if pages else await context.new_page()
        return context, page

    async def _rebind_persistent_profile(self, ctx: _SessionContext) -> None:
        """Reconnect after browser-container restart without creating a tab."""
        if not ctx.persistent_profile or self._runtime_client is None:
            return
        endpoint, runtime_id = await self._runtime_client.ensure_profile(ctx.profile_ref)
        if runtime_id == ctx.runtime_id:
            return
        browser, playwright = await self._connect_profile_browser(endpoint)
        context, page = await self._persistent_page(browser)
        ctx.context = context
        ctx.page = page
        ctx.runtime_id = runtime_id
        ctx.runtime_browser = browser
        ctx.runtime_playwright = playwright
        # Keep the registry-aligned revision across a Chromium runtime
        # restart.  The new CDP connection has no intrinsic revision history.
        ctx.driver.attach_page(
            page, document_revision=ctx.driver.current_document_revision()
        )

    async def sync_document_revision(
        self, session_id: str, document_revision: int
    ) -> None:
        """Align a live/reconnected profile driver with registry state.

        BrowserService calls this before validating an element_ref.  This is
        needed when the browser runtime restarts independently of n-agent.
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            return
        async with ctx.page_lock:
            if ctx.driver.current_document_revision() != document_revision:
                ctx.driver.restore_document_revision(document_revision)

    def last_screenshot_bytes(self, session_id: str) -> bytes | None:
        """Return the side-channel screenshot bytes captured by the session driver.

        Called by BrowserService right after execute_action to persist the
        screenshot for the Dashboard view. Synchronous (the driver caches the
        last capture in memory). Returns None if the session or bytes are absent.
        """
        ctx = self._sessions.get(session_id)
        if ctx is None:
            return None
        return ctx.driver.last_screenshot_bytes()

    # ------------------------------------------------------------------
    # BrowserBackend Protocol: takeover capability
    # ------------------------------------------------------------------

    async def begin_takeover(self, session_id: str) -> str | None:
        """Issue a short-lived, single-session interactive takeover capability URL.

        Returns a noVNC URL with a session-bound token that expires after
        ``takeover_ttl_seconds``. Returns None if the session is unknown.
        The URL is container-network internal (no host port mapping).
        """
        if session_id not in self._sessions:
            return None
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._takeover_ttl
        )
        async with self._capabilities_guard:
            self._capabilities[token] = _Capability(
                session_id=session_id,
                token=token,
                expires_at=expires_at,
                revoked=False,
            )
        if self._novnc_base_url:
            return f"{self._novnc_base_url}/vnc.html?token={token}"
        # If no noVNC base URL is configured, return the capability token.
        return f"cap://{token}"

    async def end_takeover(self, session_id: str) -> None:
        """Revoke capabilities and snapshot the page changed through VNC."""
        await self._revoke_session_capabilities(session_id)
        ctx = self._sessions.get(session_id)
        if ctx is not None:
            async with ctx.page_lock:
                await ctx.driver.sync_after_takeover()

    async def _revoke_session_capabilities(self, session_id: str) -> None:
        async with self._capabilities_guard:
            for cap in self._capabilities.values():
                if cap.session_id == session_id and not cap.revoked:
                    cap.revoked = True

    # ------------------------------------------------------------------
    # Introspection (for tests and health checks)
    # ------------------------------------------------------------------

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def active_session_count(self) -> int:
        return len(self._sessions)

    def is_connected(self) -> bool:
        return self._browser is not None

    def capability_is_active(self, token: str) -> bool:
        cap = self._capabilities.get(token)
        return cap is not None and cap.active


__all__ = [
    "ContainerBackendError",
    "ContainerBrowserBackend",
]
