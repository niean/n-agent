"""Tests for the Playwright driver adapter (T6).

The driver is a thin adapter over a STUB Page object injected via the
PageProtocol. No real browser is connected and the playwright SDK is NOT
required to be installed in this environment.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.browser import (
    BrowserActionResult,
    BrowserElementSummary,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScrollAction,
    ScreenshotAction,
    TypeAction,
)
from app.domain.browser_policy import BrowserPolicy
from app.infrastructure.browser.playwright_driver import (
    PlaywrightBrowserBackend,
    PlaywrightDriverError,
    SensitiveFieldMarker,
)
from app.infrastructure.browser.url_safety import UrlSafetyError, UrlVerifier


# ---------------------------------------------------------------------------
# Fake page objects implementing the PageProtocol surface used by the driver.
# ---------------------------------------------------------------------------


class FakeElement:
    def __init__(
        self,
        *,
        role: str = "button",
        name: str = "Submit",
        text: str = "Submit",
        disabled: bool = False,
        visible: bool = True,
        in_viewport: bool = True,
        input_type: str | None = None,
        tag: str = "button",
    ) -> None:
        self._role = role
        self._name = name
        self._text = text
        self._disabled = disabled
        self._visible = visible
        self._in_viewport = in_viewport
        self._input_type = input_type
        self._tag = tag
        self.click_calls = 0
        self.click_kwargs: list[dict[str, Any]] = []
        self.type_calls: list[str] = []
        self.scroll_into_view_calls = 0

    async def is_visible(self) -> bool:
        return self._visible

    async def is_enabled(self) -> bool:
        return not self._disabled

    async def get_attribute(self, name: str) -> str | None:
        if name == "role":
            return self._role
        if name == "aria-label" or name == "name":
            return self._name
        if name == "type":
            return self._input_type
        return None

    async def inner_text(self) -> str:
        return self._text

    async def text_content(self) -> str:
        return self._text

    async def click(self, **kwargs: Any) -> None:
        self.click_calls += 1
        self.click_kwargs.append(kwargs)

    async def type(self, text: str, delay: int = 0) -> None:  # noqa: A002
        self.type_calls.append(text)

    async def scroll_into_view_if_needed(self) -> None:
        self.scroll_into_view_calls += 1


class FakeFrame:
    def __init__(self, elements: list[FakeElement]) -> None:
        self._elements = elements
        self.evaluate_calls: list[str] = []

    async def query_selector_all(self, selector: str) -> list[Any]:
        # Driver should only use the documented selector scheme.
        if selector.startswith("xpath=") or selector.startswith("css="):
            raise ValueError(f"selector scheme forbidden: {selector}")
        # Return elements that match the marker
        return self._elements

    async def query_selector(self, selector: str) -> Any | None:
        for index, element in enumerate(self._elements):
            if f'fake-observe-ref-{index}"' in selector:
                return element
        return None

    async def evaluate(self, expression: str) -> Any:
        self.evaluate_calls.append(expression)
        if "cookies" in expression or "storageState" in expression or "localStorage" in expression:
            raise AssertionError("driver must not call evaluate for storage/cookies")
        # Return element info list - the driver's documented evaluate expression
        # gathers role/name/text/disabled/visible/input_type/tag for each element.
        # We return our element projections.
        return [
            {
                "role": el._role,
                "name": el._name,
                "text": el._text,
                "disabled": el._disabled,
                "visible": el._visible,
                "interactive": (
                    el._tag in {"a", "button", "input", "textarea", "select", "option", "summary"}
                    or el._role in {
                        "button", "link", "textbox", "combobox", "checkbox",
                        "radio", "switch", "menuitem", "option", "tab",
                    }
                ),
                "in_viewport": el._in_viewport,
                "input_type": el._input_type,
                "tag": el._tag,
                "handle_ref": f"fake-observe-ref-{index}",
            }
            for index, el in enumerate(self._elements)
        ]

    async def wait_for_load_state(self, state: str = "load") -> None:
        return None


class FakePage:
    def __init__(self, url: str = "https://example.com/", title: str = "Example") -> None:
        self._url = url
        self._title = title
        self._frames: list[FakeFrame] = []
        self._main_frame: FakeFrame | None = None
        self._screenshot_bytes: bytes | None = None
        self._closed = False
        self.goto_urls: list[str] = []
        self._navigation_count = 0
        self._document_replacement_count = 0

    def set_elements(self, elements: list[FakeElement]) -> None:
        frame = FakeFrame(elements)
        self._main_frame = frame
        self._frames = [frame]

    def set_screenshot(self, data: bytes) -> None:
        self._screenshot_bytes = data

    @property
    def url(self) -> str:
        return self._url

    @property
    def main_frame(self) -> FakeFrame | None:
        return self._main_frame

    @property
    def frames(self) -> list[FakeFrame]:
        return self._frames

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.goto_urls.append(url)
        self._url = url
        self._navigation_count += 1
        # Each navigation replaces the main document.
        self._document_replacement_count += 1
        return None

    async def screenshot(self, *, full_page: bool = False, type: str = "png") -> bytes:  # noqa: A002
        if self._screenshot_bytes is None:
            raise RuntimeError("no screenshot configured")
        return self._screenshot_bytes

    async def close(self) -> None:
        self._closed = True

    async def wait_for_url(self, url: str | None = None, *, timeout: int = 30000) -> None:
        return None

    @property
    def navigation_count(self) -> int:
        return self._navigation_count

    @property
    def document_replacement_count(self) -> int:
        return self._document_replacement_count


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.new_page_calls = 0
        self.close_calls = 0

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self._page

    async def close(self) -> None:
        self.close_calls += 1


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser
        self.chromium_calls = 0

    @property
    def chromium(self) -> FakeBrowser:
        self.chromium_calls += 1
        return self._browser


# ---------------------------------------------------------------------------
# Module import without playwright installed
# ---------------------------------------------------------------------------


def test_module_imports_without_playwright_installed():
    # Importing the module must not require the playwright SDK.
    from app.infrastructure.browser import playwright_driver
    assert hasattr(playwright_driver, "PlaywrightBrowserBackend")


# ---------------------------------------------------------------------------
# Stub page injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_returns_bounded_text_and_summaries_no_input_value():
    # Setup: page with two elements, one is a password input.
    pwd = FakeElement(
        role="textbox", name="Password", text="", input_type="password", tag="input"
    )
    btn = FakeElement(role="button", name="Sign in", text="Sign in", tag="button")
    page = FakePage()
    page.set_elements([pwd, btn])
    page.set_screenshot(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # not used by observe

    verifier = UrlVerifier()
    driver = PlaywrightBrowserBackend(url_verifier=verifier)
    driver.attach_page(page)

    result = await driver.execute_action("session-1", ObserveAction(max_text_chars=200, max_elements=10))
    assert isinstance(result, BrowserActionResult)
    assert result.action_type == "observe"
    assert result.status == "success"
    # Element refs are opaque strings
    assert len(result.elements) == 2
    for el in result.elements:
        assert isinstance(el, BrowserElementSummary)
        assert isinstance(el.element_ref, str)
        assert el.element_ref  # non-empty
    # Password field must not expose its (empty) text in summary beyond what
    # BrowserElementSummary allows; the summary has text_excerpt.
    pwd_summary = next(e for e in result.elements if e.role == "textbox" and e.accessible_name == "Password")
    # text_excerpt must NOT contain the input value (which we left empty here,
    # but the field is still masked) -- enforced as empty string.
    assert pwd_summary.text_excerpt == ""
    # Hidden/script/style elements filtered: we did not include any, but the
    # driver must reject them in the observe pipeline (covered below).
    # Text is bounded.
    assert result.text is not None
    assert len(result.text) <= 200


@pytest.mark.asyncio
async def test_observe_filters_hidden_script_style_elements():
    # We can't easily inject hidden/script/style via FakeElement without
    # extending the protocol. Instead verify that the driver calls our
    # evaluate with a projection expression and uses the returned
    # visibility/tag to filter. Our FakeFrame.evaluate returns projections
    # for whatever elements query_selector_all yields; we use the `visible`
    # flag and `tag` to verify filtering.
    hidden_el = FakeElement(role="button", name="Hidden", text="Hidden", visible=False, tag="button")
    script_el = FakeElement(role="", name="", text="", visible=True, tag="script")
    style_el = FakeElement(role="", name="", text="", visible=True, tag="style")
    visible_btn = FakeElement(role="button", name="OK", text="OK", visible=True, tag="button")

    page = FakePage()
    page.set_elements([hidden_el, script_el, style_el, visible_btn])

    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    result = await driver.execute_action("session-1", ObserveAction())
    # Only the visible button is returned.
    assert len(result.elements) == 1
    assert result.elements[0].accessible_name == "OK"


@pytest.mark.asyncio
async def test_observe_prioritizes_viewport_interactions_and_skips_layout_nodes():
    layout_nodes = [
        FakeElement(
            role="",
            name="",
            text=f"Layout {index}",
            tag="div",
            in_viewport=False,
        )
        for index in range(100)
    ]
    header_links = [
        FakeElement(
            role="link",
            name=f"Header {index}",
            text=f"Header {index}",
            tag="a",
            in_viewport=False,
        )
        for index in range(100)
    ]
    target = FakeElement(
        role="button",
        name="",
        text="Show more activity",
        tag="button",
        in_viewport=True,
    )
    page = FakePage()
    page.set_elements([*layout_nodes, *header_links, target])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    result = await driver.execute_action(
        "session-1", ObserveAction(max_text_chars=200, max_elements=10)
    )

    assert len(result.elements) == 10
    assert result.elements[0].text_excerpt == "Show more activity"
    assert all(element.role for element in result.elements)
    assert "Show more activity" in (result.text or "")


@pytest.mark.asyncio
async def test_observe_never_calls_cookies_or_storage_state():
    page = FakePage()
    page.set_elements([FakeElement()])
    # If the driver tries to call page.context.cookies() / storage_state()
    # or page.evaluate for storage, our FakeFrame.evaluate raises.
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    await driver.execute_action("session-1", ObserveAction())
    # No evaluate call should reference storage/cookies.
    frame = page.main_frame
    assert frame is not None
    for expr in frame.evaluate_calls:
        assert "cookies" not in expr
        assert "storageState" not in expr
        assert "localStorage" not in expr
        assert "sessionStorage" not in expr


# ---------------------------------------------------------------------------
# Element ref invalidation on document_revision change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_element_refs_invalidated_on_document_revision_change():
    btn = FakeElement(role="button", name="Sign in", text="Sign in")
    page = FakePage()
    page.set_elements([btn])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    # Observe to get a ref at revision 0.
    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev0 = obs.document_revision
    assert rev0 == 0

    # Simulate main-document replacement -> driver increments revision.
    driver._on_main_document_replaced()
    rev1 = driver.current_document_revision()
    assert rev1 == rev0 + 1

    # ClickAction with the OLD revision must yield stale_element_ref.
    result = await driver.execute_action(
        "session-1", ClickAction(element_ref=ref, document_revision=rev0)
    )
    assert result.status == "error"
    assert result.error_code == "stale_element_ref"


@pytest.mark.asyncio
async def test_navigate_increments_document_revision_and_calls_url_verifier():
    page = FakePage()
    page.set_elements([])

    calls: list[str] = []

    class RecordingVerifier(UrlVerifier):
        async def verify_url(self, url: str) -> str:
            calls.append(url)
            return url

    driver = PlaywrightBrowserBackend(url_verifier=RecordingVerifier())
    driver.attach_page(page)

    rev_before = driver.current_document_revision()
    result = await driver.execute_action(
        "session-1", NavigateAction(url="https://example.com/login")
    )
    assert result.status == "success"
    assert result.action_type == "navigate"
    assert driver.current_document_revision() == rev_before + 1
    # URL verifier was called at least once for the navigation.
    assert "https://example.com/login" in calls


@pytest.mark.asyncio
async def test_navigate_rejects_unsafe_url_and_does_not_increment_revision():
    page = FakePage()
    page.set_elements([])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    rev_before = driver.current_document_revision()

    result = await driver.execute_action(
        "session-1", NavigateAction(url="http://169.254.169.254/latest/meta-data/")
    )
    assert result.status == "error"
    assert result.error_code == "unsafe_url"
    # No navigation performed.
    assert page.goto_urls == []
    assert driver.current_document_revision() == rev_before


# ---------------------------------------------------------------------------
# Reject model-supplied arbitrary selectors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_rejects_css_selector_as_element_ref():
    page = FakePage()
    page.set_elements([FakeElement()])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    # The model tries to pass a CSS selector as the element_ref.
    result = await driver.execute_action(
        "session-1", ClickAction(element_ref="css=button#submit", document_revision=0)
    )
    assert result.status == "error"
    assert result.error_code in {"invalid_element_ref", "stale_element_ref"}


@pytest.mark.asyncio
async def test_click_rejects_xpath_selector_as_element_ref():
    page = FakePage()
    page.set_elements([FakeElement()])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    result = await driver.execute_action(
        "session-1",
        ClickAction(element_ref="//button[@id='submit']", document_revision=0),
    )
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Click/type re-verify visibility / role / name before acting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_re_verifies_role_and_name_before_acting():
    btn = FakeElement(role="button", name="Sign in", text="Sign in")
    page = FakePage()
    page.set_elements([btn])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    # Click with correct ref + revision -> clicks.
    result = await driver.execute_action(
        "session-1", ClickAction(element_ref=ref, document_revision=rev)
    )
    assert result.status == "success"
    assert btn.click_calls == 1


@pytest.mark.asyncio
async def test_click_bounds_playwright_wait_and_skips_scroll_for_viewport_element():
    button = FakeElement(
        role="button",
        name="button",
        text="Show more activity",
        in_viewport=True,
    )
    page = FakePage()
    page.set_elements([button])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    observed = await driver.execute_action("session-1", ObserveAction())
    result = await driver.execute_action(
        "session-1",
        ClickAction(
            element_ref=observed.elements[0].element_ref,
            document_revision=observed.document_revision,
        ),
    )

    assert result.status == "success"
    assert button.scroll_into_view_calls == 0
    assert button.click_kwargs == [{"timeout": 5000, "no_wait_after": True}]


@pytest.mark.asyncio
async def test_observe_keeps_exact_handle_when_dynamic_dom_cannot_align_full_scan():
    button = FakeElement(
        role="button",
        name="button",
        text="Show more activity",
        in_viewport=True,
    )

    class DynamicFrame(FakeFrame):
        async def query_selector_all(self, selector: str) -> list[Any]:
            raise AssertionError("dynamic full-DOM scans cannot be aligned")

    page = FakePage()
    frame = DynamicFrame([button])
    page._main_frame = frame
    page._frames = [frame]
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    observed = await driver.execute_action("session-1", ObserveAction())
    result = await driver.execute_action(
        "session-1",
        ClickAction(
            element_ref=observed.elements[0].element_ref,
            document_revision=observed.document_revision,
        ),
    )

    assert result.status == "success"
    assert button.click_calls == 1


@pytest.mark.asyncio
async def test_click_refreshes_dashboard_screenshot():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    button = FakeElement(role="button", name="", text="Show more activity")
    page = FakePage()
    page.set_elements([button])
    page.set_screenshot(png)
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    observed = await driver.execute_action("session-1", ObserveAction())
    driver._last_screenshot = None
    result = await driver.execute_action(
        "session-1",
        ClickAction(
            element_ref=observed.elements[0].element_ref,
            document_revision=observed.document_revision,
        ),
    )

    assert result.status == "success"
    assert driver.last_screenshot_bytes() == png


@pytest.mark.asyncio
async def test_click_uses_the_exact_observed_element_when_semantics_are_duplicated():
    first = FakeElement(role="button", name="", text="First")
    second = FakeElement(role="button", name="", text="Second")
    page = FakePage()
    page.set_elements([first, second])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    result = await driver.execute_action(
        "session-1",
        ClickAction(
            element_ref=obs.elements[1].element_ref,
            document_revision=obs.document_revision,
        ),
    )

    assert result.status == "success"
    assert first.click_calls == 0
    assert second.click_calls == 1


@pytest.mark.asyncio
async def test_click_accepts_native_button_with_inferred_role():
    button = FakeElement(role="", name="", text="Continue", tag="button")
    page = FakePage()
    page.set_elements([button])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    assert obs.elements[0].role == "button"
    result = await driver.execute_action(
        "session-1",
        ClickAction(
            element_ref=obs.elements[0].element_ref,
            document_revision=obs.document_revision,
        ),
    )

    assert result.status == "success"
    assert button.click_calls == 1


@pytest.mark.asyncio
async def test_click_refuses_when_element_no_longer_visible():
    btn = FakeElement(role="button", name="Sign in", text="Sign in")
    page = FakePage()
    page.set_elements([btn])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    # Element becomes hidden.
    btn._visible = False
    result = await driver.execute_action(
        "session-1", ClickAction(element_ref=ref, document_revision=rev)
    )
    assert result.status == "error"
    assert result.error_code == "element_not_interactable"
    assert btn.click_calls == 0


@pytest.mark.asyncio
async def test_click_refuses_when_role_or_name_changed():
    btn = FakeElement(role="button", name="Sign in", text="Sign in")
    page = FakePage()
    page.set_elements([btn])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    # Element identity changes (role/name different) -> stale.
    btn._role = "link"
    btn._name = "Cancel"
    result = await driver.execute_action(
        "session-1", ClickAction(element_ref=ref, document_revision=rev)
    )
    assert result.status == "error"
    assert result.error_code in {"stale_element_ref", "element_not_interactable"}
    assert btn.click_calls == 0


# ---------------------------------------------------------------------------
# Sensitive input controls: password/secret/token/credit-card/undetermined
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_into_password_field_returns_sensitive_marker():
    pwd = FakeElement(
        role="textbox", name="Password", text="", input_type="password", tag="input"
    )
    page = FakePage()
    page.set_elements([pwd])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1",
        TypeAction(element_ref=ref, document_revision=rev, text="hunter2"),
    )
    assert result.status == "error"
    assert result.error_code == "sensitive_field_requires_takeover"
    # The driver MUST NOT type the text.
    assert pwd.type_calls == []


@pytest.mark.asyncio
async def test_type_into_credit_card_field_returns_sensitive_marker():
    cc = FakeElement(
        role="textbox", name="Card Number", text="", input_type="credit-card", tag="input"
    )
    page = FakePage()
    page.set_elements([cc])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1",
        TypeAction(element_ref=ref, document_revision=rev, text="4111111111111111"),
    )
    assert result.status == "error"
    assert result.error_code == "sensitive_field_requires_takeover"
    assert cc.type_calls == []


@pytest.mark.asyncio
async def test_type_into_secret_token_field_returns_sensitive_marker():
    tok = FakeElement(
        role="textbox", name="API token", text="", input_type="secret", tag="input"
    )
    page = FakePage()
    page.set_elements([tok])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1",
        TypeAction(element_ref=ref, document_revision=rev, text="sk-xxxx"),
    )
    assert result.status == "error"
    assert result.error_code == "sensitive_field_requires_takeover"
    assert tok.type_calls == []


@pytest.mark.asyncio
async def test_type_into_undetermined_input_returns_sensitive_marker():
    # Unknown input_type -> sensitive marker (fail-closed).
    undetermined = FakeElement(
        role="textbox", name="Mystery", text="", input_type="totally-unknown", tag="input"
    )
    page = FakePage()
    page.set_elements([undetermined])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1",
        TypeAction(element_ref=ref, document_revision=rev, text="whatever"),
    )
    assert result.status == "error"
    assert result.error_code == "sensitive_field_requires_takeover"
    assert undetermined.type_calls == []


@pytest.mark.asyncio
async def test_type_into_normal_text_field_succeeds():
    inp = FakeElement(
        role="textbox", name="Search", text="", input_type="text", tag="input"
    )
    page = FakePage()
    page.set_elements([inp])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1",
        TypeAction(element_ref=ref, document_revision=rev, text="hello world"),
    )
    assert result.status == "success"
    assert inp.type_calls == ["hello world"]


@pytest.mark.asyncio
async def test_type_uses_the_exact_observed_element_when_semantics_are_duplicated():
    first = FakeElement(
        role="textbox", name="", text="", input_type="text", tag="input"
    )
    second = FakeElement(
        role="textbox", name="", text="", input_type="text", tag="input"
    )
    page = FakePage()
    page.set_elements([first, second])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    result = await driver.execute_action(
        "session-1",
        TypeAction(
            element_ref=obs.elements[1].element_ref,
            document_revision=obs.document_revision,
            text="second only",
        ),
    )

    assert result.status == "success"
    assert first.type_calls == []
    assert second.type_calls == ["second only"]


# ---------------------------------------------------------------------------
# Screenshot returns bytes via the action result (caller persists).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_returns_bytes_in_result():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    page = FakePage()
    page.set_elements([])
    page.set_screenshot(png)
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    result = await driver.execute_action("session-1", ScreenshotAction())
    # The driver returns raw bytes via a private channel; BrowserService
    # persists via screenshot_store. The BrowserActionResult itself does NOT
    # carry the bytes (it carries screenshot_captured=true).
    assert result.status == "success"
    # Raw bytes accessible via a side channel the BrowserService reads.
    raw = driver.last_screenshot_bytes()
    assert raw == png


# ---------------------------------------------------------------------------
# get_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_current_url_title_revision():
    page = FakePage(url="https://example.com/page", title="Example Page")
    page.set_elements([])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    state = await driver.get_state("session-1")
    assert state.safe_url == "https://example.com/page"
    assert state.title == "Example Page"


# ---------------------------------------------------------------------------
# begin_takeover / end_takeover are no-ops at the adapter level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_takeover_returns_none_at_adapter_level():
    page = FakePage()
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    token = await driver.begin_takeover("session-1")
    # The driver is a thin adapter; takeover lifecycle is owned by BrowserService.
    assert token is None


# ---------------------------------------------------------------------------
# connect() lazy-imports playwright (skipped when SDK absent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_raises_when_playwright_not_installed(monkeypatch):
    # Force the import to fail by inserting a sentinel into sys.modules.
    import sys
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    with pytest.raises(PlaywrightDriverError):
        await driver.connect()


# ---------------------------------------------------------------------------
# Scroll uses element ref or whole-page scroll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_with_element_ref_succeeds():
    el = FakeElement(role="textbox", name="Long text", text="..." * 200, input_type="text", tag="textarea")
    page = FakePage()
    page.set_elements([el])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)

    obs = await driver.execute_action("session-1", ObserveAction())
    ref = obs.elements[0].element_ref
    rev = obs.document_revision

    result = await driver.execute_action(
        "session-1", ScrollAction(element_ref=ref, document_revision=rev, dy=100)
    )
    assert result.status == "success"


@pytest.mark.asyncio
async def test_scroll_without_element_ref_succeeds():
    page = FakePage()
    page.set_elements([])
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    result = await driver.execute_action(
        "session-1", ScrollAction(element_ref=None, document_revision=0, dy=200)
    )
    assert result.status == "success"


# ---------------------------------------------------------------------------
# close_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_session_calls_page_close():
    page = FakePage()
    driver = PlaywrightBrowserBackend(url_verifier=UrlVerifier())
    driver.attach_page(page)
    await driver.close_session("session-1")
    assert page._closed is True
