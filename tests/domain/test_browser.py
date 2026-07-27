from __future__ import annotations

import pytest

from app.domain.browser import (
    BrowserBackendType,
    BrowserSession,
    BrowserSessionId,
    BrowserSessionStatus,
    BrowserProfileRef,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScrollAction,
    ScreenshotAction,
    TypeAction,
)


def test_container_create_is_active():
    s = BrowserSession.create_for_container("bsess-1", "nagent-1", "profile-1")
    assert s.status is BrowserSessionStatus.ACTIVE
    assert s.backend_type is BrowserBackendType.CONTAINER
    assert s.is_active is True


def test_host_create_is_pending_authorization():
    h = BrowserSession.create_for_host("bsess-2", "nagent-1", "profile-2")
    assert h.status is BrowserSessionStatus.PENDING_AUTHORIZATION
    assert h.backend_type is BrowserBackendType.HOST_CDP


def test_allowed_transitions_active():
    s = BrowserSession.create_for_container("b", "n", "p")
    for target in (BrowserSessionStatus.PAUSED, BrowserSessionStatus.TAKEOVER,
                   BrowserSessionStatus.DEGRADED, BrowserSessionStatus.CLOSED):
        assert s.can_transition_to(target) is True
    # cannot go back to pending or stay active
    assert s.can_transition_to(BrowserSessionStatus.PENDING_AUTHORIZATION) is False


def test_denied_transitions_pending():
    h = BrowserSession.create_for_host("b", "n", "p")
    assert h.can_transition_to(BrowserSessionStatus.PAUSED) is False
    assert h.can_transition_to(BrowserSessionStatus.TAKEOVER) is False
    assert h.can_transition_to(BrowserSessionStatus.ACTIVE) is True
    assert h.can_transition_to(BrowserSessionStatus.CLOSED) is True


def test_degraded_only_to_closed():
    s = BrowserSession.create_for_container("b", "n", "p")
    d = s.transition_to(BrowserSessionStatus.DEGRADED, reason="x")
    assert d.status is BrowserSessionStatus.DEGRADED
    assert d.can_transition_to(BrowserSessionStatus.CLOSED) is True
    assert d.can_transition_to(BrowserSessionStatus.ACTIVE) is False


def test_closed_is_terminal():
    s = BrowserSession.create_for_container("b", "n", "p")
    c = s.transition_to(BrowserSessionStatus.CLOSED)
    assert c.status is BrowserSessionStatus.CLOSED
    assert c.can_transition_to(BrowserSessionStatus.ACTIVE) is False


def test_takeover_saves_pre_takeover_status():
    s = BrowserSession.create_for_container("b", "n", "p")
    t = s.transition_to(BrowserSessionStatus.TAKEOVER)
    assert t.status is BrowserSessionStatus.TAKEOVER
    assert t.pre_takeover_status is BrowserSessionStatus.ACTIVE


def test_release_restores_pre_takeover_status_and_clears():
    s = BrowserSession.create_for_container("b", "n", "p")
    t = s.transition_to(BrowserSessionStatus.TAKEOVER)
    released = t.transition_to(BrowserSessionStatus.ACTIVE)
    assert released.status is BrowserSessionStatus.ACTIVE
    assert released.pre_takeover_status is None


def test_release_can_restore_paused():
    s = BrowserSession.create_for_container("b", "n", "p")
    paused = s.transition_to(BrowserSessionStatus.PAUSED)
    t = paused.transition_to(BrowserSessionStatus.TAKEOVER)
    assert t.pre_takeover_status is BrowserSessionStatus.PAUSED
    released = t.transition_to(BrowserSessionStatus.PAUSED)
    assert released.status is BrowserSessionStatus.PAUSED
    assert released.pre_takeover_status is None


def test_invalid_transition_raises():
    s = BrowserSession.create_for_container("b", "n", "p")
    with pytest.raises(ValueError, match="invalid_state_transition"):
        s.transition_to(BrowserSessionStatus.PENDING_AUTHORIZATION)


def test_session_id_opaque_rejects_path_chars():
    with pytest.raises(ValueError):
        BrowserSessionId("a/b")
    with pytest.raises(ValueError):
        BrowserSessionId("")
    BrowserSessionId("bsess-ok")


def test_profile_ref_opaque_rejects_path_chars():
    with pytest.raises(ValueError):
        BrowserProfileRef("a/b")
    with pytest.raises(ValueError):
        BrowserProfileRef("")
    BrowserProfileRef("profile-ok")


def test_click_action_requires_element_ref_and_revision():
    with pytest.raises(ValueError):
        ClickAction(element_ref="", document_revision=0)
    with pytest.raises(ValueError):
        ClickAction(element_ref="el", document_revision=-1)
    ClickAction(element_ref="el-1", document_revision=0)


def test_type_action_validation():
    with pytest.raises(ValueError):
        TypeAction(element_ref="el", document_revision=0, text="x\x00y")
    TypeAction(element_ref="el-1", document_revision=0, text="hello", clear_first=True)


def test_observe_action_bounds():
    with pytest.raises(ValueError):
        ObserveAction(max_text_chars=0)
    with pytest.raises(ValueError):
        ObserveAction(max_text_chars=100000)
    with pytest.raises(ValueError):
        ObserveAction(max_elements=0)
    ObserveAction()


def test_scroll_action_allows_none_element_ref():
    ScrollAction(element_ref=None, document_revision=0, dx=10, dy=10)
    with pytest.raises(ValueError):
        ScrollAction(element_ref=None, document_revision=-1, dx=0, dy=0)


def test_navigate_action_requires_url():
    with pytest.raises(ValueError):
        NavigateAction(url="")
    NavigateAction(url="https://example.com")


def test_screenshot_action_default():
    a = ScreenshotAction()
    assert a.full_page is False
    ScreenshotAction(full_page=True)
