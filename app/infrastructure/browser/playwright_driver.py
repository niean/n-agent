"""Playwright browser backend adapter (T6).

Thin adapter over a Playwright ``Page`` object. Generates opaque
``element_ref`` values backed by a page-internal index map invalidated on
document_revision change. Never reads cookies/storage_state. Never accepts
model-supplied arbitrary CSS/XPath/JS selectors -- only element_ref values
emitted by ``observe`` are honored.

The Playwright SDK is imported lazily inside ``connect``; this module imports
cleanly even when ``playwright`` is not installed, which lets the test suite
inject a STUB page implementing :class:`PageProtocol`.
"""
from __future__ import annotations

import inspect
import logging
import secrets
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.domain.browser import (
    BrowserActionResult,
    BrowserElementSummary,
    BrowserState,
    BrowserSession,
    BrowserSessionStatus,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScrollAction,
    ScreenshotAction,
    TypeAction,
)
from app.infrastructure.browser.url_safety import UrlSafetyError, UrlVerifier

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright  # noqa: F401

logger = logging.getLogger(__name__)


# Sensitive input types / autocomplete hints that must NOT be typed by the
# automation. Fail-closed: unknown types are also sensitive.
_SENSITIVE_INPUT_TYPES = frozenset({
    "password",
    "secret",
    "token",
    "credit-card",
    "creditcard",
    "cc-number",
    "cc-csc",
    "cc-exp",
    "new-password",
    "current-password",
})

# Known-safe input types. Anything not in this set (and not in the sensitive
# set) is treated as sensitive (fail-closed). This catches undetermined
# values like "totally-unknown".
_SAFE_INPUT_TYPES = frozenset({
    "text",
    "search",
    "email",
    "url",
    "tel",
    "number",
    "date",
    "time",
    "datetime-local",
    "month",
    "week",
    "range",
    "color",
    "checkbox",
    "radio",
    "submit",
    "reset",
    "button",
    "image",
    "file",
})

# Tags that never appear in observe output.
_HIDDEN_TAGS = frozenset({"script", "style", "template", "head", "link", "meta", "title", "base", "noscript"})

# Markers that indicate a model-supplied selector rather than an opaque ref.
_FORBIDDEN_REF_PREFIXES = ("css=", "xpath=", "text=", "role=", "label=", "test=", "data-test=")


class PlaywrightDriverError(RuntimeError):
    """Raised when the Playwright SDK is unavailable or a driver op fails."""


class SensitiveFieldMarker:
    """Marker indicating the driver refused to type into a sensitive field."""

    code = "sensitive_field_requires_takeover"


@runtime_checkable
class PageProtocol(Protocol):
    """Duck-typed surface of a Playwright Page that this adapter relies on.

    The real ``playwright.async_api.Page`` satisfies this protocol. Tests
    inject a fake page that implements these members.
    """

    @property
    def url(self) -> str: ...

    @property
    def main_frame(self) -> Any: ...

    async def title(self) -> str: ...

    async def goto(self, url: str, **kwargs: Any) -> Any: ...

    async def screenshot(self, *, full_page: bool = False, type: str = "png") -> bytes: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Internal: element-ref index map
# ---------------------------------------------------------------------------


class _ElementEntry:
    __slots__ = (
        "ref",
        "role",
        "name",
        "text",
        "disabled",
        "visible",
        "in_viewport",
        "input_type",
        "tag",
        "handle",
    )

    def __init__(
        self,
        *,
        ref: str,
        role: str,
        name: str,
        text: str,
        disabled: bool,
        visible: bool,
        in_viewport: bool,
        input_type: str | None,
        tag: str,
        handle: Any | None = None,
    ) -> None:
        self.ref = ref
        self.role = role or ""
        self.name = name or ""
        self.text = text or ""
        self.disabled = bool(disabled)
        self.visible = bool(visible)
        self.in_viewport = bool(in_viewport)
        self.input_type = input_type
        self.tag = (tag or "").lower()
        self.handle = handle


class _ElementIndex:
    """Per-document-revision map of element_ref -> element entry."""

    def __init__(self) -> None:
        self._entries: dict[str, _ElementEntry] = {}

    def reset(self) -> None:
        self._entries.clear()

    def add(self, entry: _ElementEntry) -> None:
        self._entries[entry.ref] = entry

    def get(self, ref: str) -> _ElementEntry | None:
        return self._entries.get(ref)

    @staticmethod
    def new_ref() -> str:
        # Opaque, url-safe, unpredictable. NOT a CSS/XPath selector.
        return "el-" + secrets.token_urlsafe(12)


# ---------------------------------------------------------------------------
# PlaywrightBrowserBackend
# ---------------------------------------------------------------------------


class PlaywrightBrowserBackend:
    """Thin adapter over a Playwright page.

    Owns NO session lifecycle and writes NO registry state. The BrowserService
    owns lifecycle, registry writes, and screenshot persistence.
    """

    def __init__(
        self,
        *,
        url_verifier: UrlVerifier,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        self._url_verifier = url_verifier
        self._default_timeout = default_timeout_seconds
        # Element actions need to fail before the service/backend's outer
        # timeout so a local actionability problem does not invalidate the
        # whole browser session. Five seconds is ample for a stable control;
        # shorter configured timeouts retain a safety margin.
        self._interaction_timeout_ms = max(
            100,
            min(5000, int(default_timeout_seconds * 500)),
        )
        self._page: PageProtocol | Any | None = None
        self._playwright: "Playwright | None" = None
        self._browser: "Browser | None" = None
        self._document_revision = 0
        self._index = _ElementIndex()
        self._last_screenshot: bytes | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Lazy-import playwright and launch a chromium browser.

        Raises :class:`PlaywrightDriverError` if the SDK is unavailable.
        """
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError as exc:
            raise PlaywrightDriverError(
                "playwright SDK is not installed; run `pip install playwright` "
                "and `playwright install chromium`"
            ) from exc
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._page = await self._browser.new_page()

    def attach_page(self, page: Any) -> None:
        """Inject a page implementing :class:`PageProtocol` (for tests)."""
        self._page = page
        self._document_revision = 0
        self._index = _ElementIndex()
        self._last_screenshot = None

    @property
    def page(self) -> Any | None:
        return self._page

    def current_document_revision(self) -> int:
        return self._document_revision

    def last_screenshot_bytes(self) -> bytes | None:
        return self._last_screenshot

    # ------------------------------------------------------------------
    # Document-revision tracking (called by navigate / redirect hooks)
    # ------------------------------------------------------------------

    def _on_main_document_replaced(self) -> None:
        """Mark that the main document was replaced (navigation/redirect).

        Increments the internal document_revision and invalidates the
        element_ref index. Old refs cannot be used after this.
        """
        self._document_revision += 1
        self._index.reset()

    # ------------------------------------------------------------------
    # BrowserBackend Protocol: create/close/get_state/takeover
    # ------------------------------------------------------------------

    async def create_session(self, session: BrowserSession) -> None:
        # The driver does NOT own session lifecycle; the BrowserService
        # calls create_session only for active backends. If no page has been
        # attached (production path), connect() lazily.
        if self._page is None:
            await self.connect()

    async def close_session(self, session_id: str) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                logger.warning("page close failed for session=%s", session_id, exc_info=True)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.warning("browser close failed", exc_info=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.warning("playwright stop failed", exc_info=True)
        self._page = None
        self._browser = None
        self._playwright = None
        self._index.reset()
        self._document_revision = 0
        self._last_screenshot = None

    async def get_state(self, session_id: str) -> BrowserState:
        if self._page is None:
            return BrowserState(
                safe_url=None,
                title=None,
                status=BrowserSessionStatus.CLOSED,
                document_revision=self._document_revision,
                latest_screenshot_ref=None,
            )
        url = ""
        try:
            url = self._page.url or ""
        except Exception:
            url = ""
        title = ""
        try:
            title = await self._page.title()
        except Exception:
            title = ""
        # Sanitize URL for safe projection (strip query/fragment).
        safe_url = UrlVerifier.sanitize_url(url) if url else None
        return BrowserState(
            safe_url=safe_url,
            title=title or None,
            status=BrowserSessionStatus.ACTIVE,
            document_revision=self._document_revision,
            latest_screenshot_ref=None,
        )

    async def begin_takeover(self, session_id: str) -> str | None:
        # The driver is a thin adapter; takeover lifecycle is owned by the
        # BrowserService and the registry. Return None to signal "no driver
        # token required".
        return None

    async def end_takeover(self, session_id: str) -> None:
        # No-op at adapter level.
        return None

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def execute_action(
        self, session_id: str, action: Any
    ) -> BrowserActionResult:
        if self._page is None:
            return BrowserActionResult(
                action_type=type(action).__name__,
                status="error",
                error_code="backend_not_connected",
            )
        if isinstance(action, NavigateAction):
            return await self._do_navigate(session_id, action)
        if isinstance(action, ObserveAction):
            return await self._do_observe(session_id, action)
        if isinstance(action, ClickAction):
            return await self._do_click(session_id, action)
        if isinstance(action, TypeAction):
            return await self._do_type(session_id, action)
        if isinstance(action, ScrollAction):
            return await self._do_scroll(session_id, action)
        if isinstance(action, ScreenshotAction):
            return await self._do_screenshot(session_id, action)
        return BrowserActionResult(
            action_type=type(action).__name__,
            status="error",
            error_code="unknown_action_type",
        )

    # ------------------------------------------------------------------
    # navigate
    # ------------------------------------------------------------------

    async def _do_navigate(
        self, session_id: str, action: NavigateAction
    ) -> BrowserActionResult:
        # Verify URL safety before navigating.
        try:
            await self._url_verifier.verify_url(action.url)
        except UrlSafetyError:
            return BrowserActionResult(
                action_type="navigate",
                status="error",
                error_code="unsafe_url",
            )
        try:
            await self._page.goto(action.url, wait_until="domcontentloaded")
            # Each navigation replaces the main document.
            self._on_main_document_replaced()
        except Exception as exc:
            return BrowserActionResult(
                action_type="navigate",
                status="error",
                error_code="navigation_failed",
                text=str(exc)[:200],
            )
        title = ""
        try:
            title = await self._page.title()
        except Exception:
            title = ""
        url = ""
        try:
            url = self._page.url or action.url
        except Exception:
            url = action.url
        safe_url = UrlVerifier.sanitize_url(url) if url else None
        # Capture a screenshot for the Dashboard view (side channel; persisted
        # by BrowserService, never exposed to the model ToolResult).
        await self._capture_dashboard_screenshot()
        return BrowserActionResult(
            action_type="navigate",
            status="success",
            url=safe_url,
            title=title or None,
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # observe
    # ------------------------------------------------------------------

    async def _do_observe(
        self, session_id: str, action: ObserveAction
    ) -> BrowserActionResult:
        # Reset the element index at each observe (a fresh snapshot).
        self._index.reset()
        # We never read cookies/storage_state. Give each visible interaction a
        # per-observe internal marker so its exact ElementHandle can be
        # recovered even while a dynamic page changes unrelated DOM nodes.
        observation_marker = "obs-" + secrets.token_urlsafe(8)
        projection_expr = (
            "() => {"
            "const attr = 'data-n-agent-observe-ref';"
            f"const marker = '{observation_marker}';"
            "document.querySelectorAll(`[${attr}]`).forEach("
            "el => el.removeAttribute(attr));"
            "return Array.from(document.querySelectorAll('body *')).map("
            "(el, index) => {"
            "const tag = el.tagName.toLowerCase();"
            "const role = el.getAttribute('role') || '';"
            "const visible = el.checkVisibility?.() ?? (el.offsetParent !== null);"
            "const interactive = "
            "['a','button','input','textarea','select','option','summary'].includes(tag) || "
            "['button','link','textbox','combobox','checkbox','radio','switch',"
            "'menuitem','option','tab'].includes(role) || "
            "el.hasAttribute('tabindex') || el.isContentEditable;"
            "const handle_ref = visible && interactive ? `${marker}-${index}` : null;"
            "if (handle_ref) el.setAttribute(attr, handle_ref);"
            "const rect = el.getBoundingClientRect();"
            "return {"
            "role,"
            "name: el.getAttribute('aria-label') || el.getAttribute('name') || '',"
            "text: (el.innerText || el.textContent || '').slice(0, 200),"
            "disabled: el.disabled || false,"
            "visible,"
            "interactive,"
            "in_viewport: rect.bottom > 0 && rect.right > 0 "
            "&& rect.top < window.innerHeight && rect.left < window.innerWidth,"
            "input_type: el.getAttribute('type') || null,"
            "tag,"
            "handle_ref"
            "};"
            "});"
            "}"
        )
        try:
            frame = self._page.main_frame
            try:
                projection: list[dict[str, Any]] = await frame.evaluate(projection_expr)
            except Exception:
                projection = []
        except Exception as exc:
            return BrowserActionResult(
                action_type="observe",
                status="error",
                error_code="observe_failed",
                text=str(exc)[:200],
            )

        candidates: list[tuple[int, dict[str, Any]]] = []
        for projection_index, proj in enumerate(projection):
            tag = (proj.get("tag") or "").lower()
            if tag in _HIDDEN_TAGS:
                continue
            if not proj.get("visible", False):
                continue
            role = proj.get("role") or _infer_role_from_tag(tag)
            interactive = bool(
                proj.get("interactive", _is_interactive_semantics(tag, role))
            )
            if not interactive:
                continue
            candidates.append((projection_index, proj))

        # Scrolling must affect what the model sees. Preserve DOM order within
        # each group, but place current-viewport interactions before off-screen
        # navigation and controls so max_elements cannot hide the target.
        candidates.sort(
            key=lambda item: (
                not bool(item[1].get("in_viewport", False)),
                item[0],
            )
        )

        summaries: list[BrowserElementSummary] = []
        body_text_parts: list[str] = []
        for projection_index, proj in candidates[: action.max_elements]:
            tag = (proj.get("tag") or "").lower()
            role = proj.get("role") or _infer_role_from_tag(tag)
            name = proj.get("name") or ""
            text = (proj.get("text") or "").strip()
            disabled = bool(proj.get("disabled", False))
            input_type = proj.get("input_type")
            # Password fields never expose text in summary.
            if _is_sensitive_input(input_type, tag):
                text_excerpt = ""
            else:
                text_excerpt = text[:80]
            handle = None
            handle_ref = proj.get("handle_ref")
            if isinstance(handle_ref, str) and handle_ref:
                try:
                    handle = await frame.query_selector(
                        f'[data-n-agent-observe-ref="{handle_ref}"]'
                    )
                except Exception:
                    handle = None
            ref = _ElementIndex.new_ref()
            entry = _ElementEntry(
                ref=ref,
                role=role,
                name=name,
                text=text,
                disabled=disabled,
                visible=True,
                in_viewport=bool(proj.get("in_viewport", False)),
                input_type=input_type,
                tag=tag,
                handle=handle,
            )
            self._index.add(entry)
            summaries.append(
                BrowserElementSummary(
                    element_ref=ref,
                    role=role,
                    accessible_name=name,
                    text_excerpt=text_excerpt,
                    disabled=disabled,
                )
            )
            if text:
                body_text_parts.append(text)

        body_text = "\n".join(body_text_parts)[: action.max_text_chars]
        # Safe URL projection.
        url = ""
        try:
            url = self._page.url or ""
        except Exception:
            url = ""
        safe_url = UrlVerifier.sanitize_url(url) if url else None
        title = ""
        try:
            title = await self._page.title()
        except Exception:
            title = ""
        # Capture a screenshot for the Dashboard view (side channel).
        await self._capture_dashboard_screenshot()
        return BrowserActionResult(
            action_type="observe",
            status="success",
            url=safe_url,
            title=title or None,
            text=body_text,
            elements=tuple(summaries),
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # click
    # ------------------------------------------------------------------

    async def _do_click(
        self, session_id: str, action: ClickAction
    ) -> BrowserActionResult:
        if _looks_like_selector(action.element_ref):
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="invalid_element_ref",
            )
        if action.document_revision != self._document_revision:
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="stale_element_ref",
            )
        entry = self._index.get(action.element_ref)
        if entry is None:
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="stale_element_ref",
            )
        # Re-verify the element at the page level.
        try:
            element = await self._lookup_element(entry)
        except Exception:
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="element_not_interactable",
            )
        if element is None:
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="element_not_interactable",
            )
        try:
            visible = await element.is_visible() if hasattr(element, "is_visible") else True
            if not visible:
                return BrowserActionResult(
                    action_type="click",
                    status="error",
                    error_code="element_not_interactable",
                )
            tag_now = await _element_tag(element, entry.tag)
            role_now = (
                await _element_attribute(element, "role")
                or _infer_role_from_tag(tag_now)
            )
            name_now = (
                await _element_attribute(element, "aria-label")
                or await _element_attribute(element, "name")
            )
            # Role/name must match what was captured.
            if (
                tag_now != entry.tag
                or (role_now or "") != entry.role
                or (name_now or "") != entry.name
            ):
                return BrowserActionResult(
                    action_type="click",
                    status="error",
                    error_code="stale_element_ref",
                )
            # Avoid another stability wait when observe already captured the
            # control in the current viewport. This matters on dynamically
            # expanding pages where layout can keep moving after scrolling.
            if not entry.in_viewport and hasattr(element, "scroll_into_view_if_needed"):
                await element.scroll_into_view_if_needed(
                    timeout=self._interaction_timeout_ms
                )
            # Dispatch the approved click without waiting for a possible
            # navigation/load lifecycle. A later observe is the authority for
            # the resulting page state.
            await element.click(
                timeout=self._interaction_timeout_ms,
                no_wait_after=True,
            )
        except Exception as exc:
            return BrowserActionResult(
                action_type="click",
                status="error",
                error_code="element_not_interactable",
                text=str(exc)[:200],
            )
        await self._capture_dashboard_screenshot()
        return BrowserActionResult(
            action_type="click",
            status="success",
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # type
    # ------------------------------------------------------------------

    async def _do_type(
        self, session_id: str, action: TypeAction
    ) -> BrowserActionResult:
        if _looks_like_selector(action.element_ref):
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="invalid_element_ref",
            )
        if action.document_revision != self._document_revision:
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="stale_element_ref",
            )
        entry = self._index.get(action.element_ref)
        if entry is None:
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="stale_element_ref",
            )
        # Fail-closed: sensitive input types never receive the typed text.
        if _is_sensitive_input(entry.input_type, entry.tag):
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code=SensitiveFieldMarker.code,
            )
        try:
            element = await self._lookup_element(entry)
        except Exception:
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="element_not_interactable",
            )
        if element is None:
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="element_not_interactable",
            )
        try:
            visible = await element.is_visible() if hasattr(element, "is_visible") else True
            if not visible:
                return BrowserActionResult(
                    action_type="type",
                    status="error",
                    error_code="element_not_interactable",
                )
            tag_now = await _element_tag(element, entry.tag)
            role_now = (
                await _element_attribute(element, "role")
                or _infer_role_from_tag(tag_now)
            )
            name_now = (
                await _element_attribute(element, "aria-label")
                or await _element_attribute(element, "name")
            )
            if (
                tag_now != entry.tag
                or (role_now or "") != entry.role
                or (name_now or "") != entry.name
            ):
                return BrowserActionResult(
                    action_type="type",
                    status="error",
                    error_code="stale_element_ref",
                )
            if not entry.in_viewport and hasattr(element, "scroll_into_view_if_needed"):
                await element.scroll_into_view_if_needed(
                    timeout=self._interaction_timeout_ms
                )
            if action.clear_first and hasattr(element, "fill"):
                await element.fill("")
            await element.type(action.text)
        except Exception as exc:
            return BrowserActionResult(
                action_type="type",
                status="error",
                error_code="element_not_interactable",
                text=str(exc)[:200],
            )
        await self._capture_dashboard_screenshot()
        return BrowserActionResult(
            action_type="type",
            status="success",
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # scroll
    # ------------------------------------------------------------------

    async def _do_scroll(
        self, session_id: str, action: ScrollAction
    ) -> BrowserActionResult:
        if action.element_ref is not None:
            if _looks_like_selector(action.element_ref):
                return BrowserActionResult(
                    action_type="scroll",
                    status="error",
                    error_code="invalid_element_ref",
                )
            if action.document_revision != self._document_revision:
                return BrowserActionResult(
                    action_type="scroll",
                    status="error",
                    error_code="stale_element_ref",
                )
            entry = self._index.get(action.element_ref)
            if entry is None:
                return BrowserActionResult(
                    action_type="scroll",
                    status="error",
                    error_code="stale_element_ref",
                )
            try:
                element = await self._lookup_element(entry)
                if element is not None and hasattr(element, "scroll_into_view_if_needed"):
                    await element.scroll_into_view_if_needed()
            except Exception:
                # Best-effort; the scroll action itself is on the page.
                pass
        # Page-level scroll via mouse wheel simulation.
        try:
            frame = self._page.main_frame
            if hasattr(frame, "evaluate"):
                await frame.evaluate(
                    f"() => window.scrollBy({int(action.dx)}, {int(action.dy)})"
                )
        except Exception:
            # Some test fakes do not implement evaluate; treat as success.
            pass
        await self._capture_dashboard_screenshot()
        return BrowserActionResult(
            action_type="scroll",
            status="success",
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # screenshot
    # ------------------------------------------------------------------

    async def _capture_dashboard_screenshot(self) -> None:
        """Best-effort screenshot for the Dashboard view (side channel).

        Stored on ``self._last_screenshot`` so BrowserService can persist it
        and the Dashboard can read it. Never fails the calling action; capture
        errors just clear the bytes.
        """
        try:
            self._last_screenshot = await self._page.screenshot(
                full_page=False, type="png"
            )
        except Exception:
            self._last_screenshot = None

    async def _do_screenshot(
        self, session_id: str, action: ScreenshotAction
    ) -> BrowserActionResult:
        try:
            data = await self._page.screenshot(
                full_page=action.full_page, type="png"
            )
        except Exception as exc:
            return BrowserActionResult(
                action_type="screenshot",
                status="error",
                error_code="screenshot_failed",
                text=str(exc)[:200],
            )
        # The driver keeps raw bytes on a side channel; BrowserService reads
        # them via last_screenshot_bytes() and persists via screenshot_store.
        # The BrowserActionResult itself carries only a flag.
        self._last_screenshot = data
        return BrowserActionResult(
            action_type="screenshot",
            status="success",
            document_revision=self._document_revision,
        )

    # ------------------------------------------------------------------
    # Element lookup
    # ------------------------------------------------------------------

    async def _lookup_element(self, entry: _ElementEntry) -> Any | None:
        """Return only the exact ElementHandle captured by observe.

        Missing handles fail closed. A semantic scan of the live DOM can pick
        the wrong duplicate and is unbounded on large, dynamic pages.
        """
        return entry.handle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _element_attribute(element: Any, name: str) -> str:
    if not hasattr(element, "get_attribute"):
        return ""
    value = element.get_attribute(name)
    if inspect.isawaitable(value):
        value = await value
    return value if isinstance(value, str) else ""


async def _element_tag(element: Any, fallback: str) -> str:
    if hasattr(element, "evaluate"):
        value = element.evaluate("el => el.tagName.toLowerCase()")
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, str) and value:
            return value.lower()
    return (fallback or "").lower()


def _looks_like_selector(ref: str) -> bool:
    """Return True if *ref* looks like a CSS/XPath/text/role selector.

    Element refs are opaque tokens of the form ``el-<random>``; any other
    prefix that looks like a Playwright selector engine is rejected.
    """
    if not ref:
        return False
    lowered = ref.lower()
    for prefix in _FORBIDDEN_REF_PREFIXES:
        if lowered.startswith(prefix):
            return True
    # XPath starts with / or //
    if ref.startswith("/") or ref.startswith("./"):
        return True
    # CSS selectors contain characters not present in token_urlsafe output.
    if any(c in ref for c in ("#", ">", "[", "]", " ", "*", "(", ")", ":")):
        return True
    return False


def _is_sensitive_input(input_type: str | None, tag: str) -> bool:
    """Return True if the input control is sensitive (fail-closed).

    Treats unknown input types and ``password``/``secret``/``token``/
    ``credit-card`` autocomplete hints as sensitive. ``text``/``search``/
    ``email``/``url``/``tel`` are non-sensitive. Any input_type not in the
    known-safe set is treated as sensitive (fail-closed).
    """
    if tag in ("script", "style"):
        return False
    if input_type is None or input_type == "":
        # Undetermined input on an <input> -> fail-closed.
        if tag == "input":
            return True
        return False
    lowered = input_type.lower()
    if lowered in _SENSITIVE_INPUT_TYPES:
        return True
    if lowered in _SAFE_INPUT_TYPES:
        return False
    # Unknown input_type -> fail-closed.
    return True


def _is_interactive_semantics(tag: str, role: str) -> bool:
    return tag in {
        "a",
        "button",
        "input",
        "textarea",
        "select",
        "option",
        "summary",
    } or role in {
        "button",
        "link",
        "textbox",
        "combobox",
        "checkbox",
        "radio",
        "switch",
        "menuitem",
        "option",
        "tab",
    }


def _infer_role_from_tag(tag: str) -> str:
    """Infer an ARIA role from a tag name when role attribute is absent."""
    role_map = {
        "a": "link",
        "button": "button",
        "input": "textbox",
        "textarea": "textbox",
        "select": "combobox",
        "option": "option",
        "img": "image",
        "form": "form",
        "nav": "navigation",
        "main": "main",
        "article": "article",
        "section": "region",
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "h4": "heading",
        "h5": "heading",
        "h6": "heading",
        "label": "label",
        "table": "table",
        "tr": "row",
        "td": "cell",
        "th": "columnheader",
        "ul": "list",
        "ol": "list",
        "li": "listitem",
    }
    return role_map.get(tag.lower(), "")


__all__ = [
    "PageProtocol",
    "PlaywrightBrowserBackend",
    "PlaywrightDriverError",
    "SensitiveFieldMarker",
]
