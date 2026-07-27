from __future__ import annotations

from dataclasses import dataclass

from app.domain.browser import (
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
    BrowserScreenshotConsumer,
)
from app.domain.browser_policy import BrowserPolicy, BrowserPolicyRequest
from app.domain.policy import PolicyOutcome


def _host_session():
    return BrowserSession.create_for_host("b-1", "n-1", "p-1")


def _container_session():
    return BrowserSession.create_for_container("b-2", "n-1", "p-2")


def _req(
    session,
    action_type="navigate",
    backend=None,
    consumer=None,
    takeover=False,
    grant=None,
):
    return BrowserPolicyRequest(
        run_context=None,
        session=session,
        action_type=action_type,
        requested_backend=backend or session.backend_type,
        trusted_host_grant=grant,
        screenshot_consumer=consumer,
        takeover_operation="request" if takeover else None,
    )


@dataclass
class _FakeGrant:
    browser_session_id: str
    n_agent_session_id: str
    actor_id: str
    policy_version: str
    expired: bool = False
    revoked: bool = False


def test_host_default_deny_without_grant():
    p = BrowserPolicy()
    d = p.evaluate(_req(_host_session(), "navigate"))
    assert d.outcome is PolicyOutcome.DENY
    assert d.reason == "host_grant_required"


def test_pending_authorization_returns_host_grant_required():
    p = BrowserPolicy()
    d = p.evaluate(_req(_host_session(), "navigate"))
    assert d.outcome is PolicyOutcome.DENY
    assert d.reason == "host_grant_required"


def test_container_default_allow_active():
    p = BrowserPolicy()
    d = p.evaluate(_req(_container_session(), "navigate"))
    assert d.outcome is PolicyOutcome.ALLOW
    assert d.allowed_backend is BrowserBackendType.CONTAINER


def test_takeover_request_requires_approval():
    p = BrowserPolicy()
    d = p.evaluate(_req(_container_session(), "navigate", takeover=True))
    assert d.outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_takeover_release_only_in_takeover():
    p = BrowserPolicy()
    release_req = BrowserPolicyRequest(
        run_context=None,
        session=_container_session(),
        action_type="navigate",
        requested_backend=BrowserBackendType.CONTAINER,
        trusted_host_grant=None,
        screenshot_consumer=None,
        takeover_operation="release",
    )
    # not in takeover -> deny
    assert p.evaluate(release_req).outcome is PolicyOutcome.DENY
    # in takeover -> allow release
    t = _container_session().transition_to(BrowserSessionStatus.TAKEOVER)
    release_req_takeover = BrowserPolicyRequest(
        run_context=None,
        session=t,
        action_type="navigate",
        requested_backend=BrowserBackendType.CONTAINER,
        trusted_host_grant=None,
        screenshot_consumer=None,
        takeover_operation="release",
    )
    assert p.evaluate(release_req_takeover).outcome is PolicyOutcome.ALLOW


def test_screenshot_only_dashboard_internal():
    p = BrowserPolicy()
    s = _container_session()
    d = p.evaluate(_req(s, "screenshot", consumer=BrowserScreenshotConsumer.LLM_PROVIDER))
    assert d.outcome is PolicyOutcome.DENY
    d2 = p.evaluate(_req(s, "screenshot", consumer=BrowserScreenshotConsumer.DASHBOARD_INTERNAL))
    assert d2.outcome is PolicyOutcome.ALLOW
    assert d2.screenshot_allowed is True


def test_screenshot_all_non_dashboard_consumers_denied():
    p = BrowserPolicy()
    s = _container_session()
    for consumer in (
        BrowserScreenshotConsumer.EXTERNAL_TOOL,
        BrowserScreenshotConsumer.EXTERNAL_MEMORY,
        BrowserScreenshotConsumer.OBSERVATION_LOG,
        BrowserScreenshotConsumer.USAGE_RETENTION,
        BrowserScreenshotConsumer.CLIENT_RESPONSE,
    ):
        d = p.evaluate(_req(s, "screenshot", consumer=consumer))
        assert d.outcome is PolicyOutcome.DENY, consumer


def test_non_active_states_deny_actions():
    p = BrowserPolicy()
    for st in (BrowserSessionStatus.PAUSED, BrowserSessionStatus.TAKEOVER,
               BrowserSessionStatus.DEGRADED, BrowserSessionStatus.CLOSED):
        s = BrowserSession(
            id="x", bound_n_agent_session_id="n",
            backend_type=BrowserBackendType.CONTAINER, status=st, profile_ref="p",
        )
        assert p.evaluate(_req(s, "navigate")).outcome is PolicyOutcome.DENY, st


def test_host_grant_valid_allows():
    p = BrowserPolicy()
    h = _host_session()
    active_h = h.transition_to(BrowserSessionStatus.ACTIVE)
    grant = _FakeGrant(
        browser_session_id=active_h.id,
        n_agent_session_id=active_h.bound_n_agent_session_id,
        actor_id="actor-1",
        policy_version="v1",
    )
    d = p.evaluate(_req(active_h, "navigate", grant=grant))
    assert d.outcome is PolicyOutcome.ALLOW
    assert d.allowed_backend is BrowserBackendType.HOST_CDP


def test_host_grant_rejects_missing_bindings():
    p = BrowserPolicy()
    h = _host_session()
    active_h = h.transition_to(BrowserSessionStatus.ACTIVE)
    # wrong session id
    g1 = _FakeGrant("other", active_h.bound_n_agent_session_id, "a", "v1")
    assert p.evaluate(_req(active_h, "navigate", grant=g1)).outcome is PolicyOutcome.DENY
    # wrong n_agent session
    g2 = _FakeGrant(active_h.id, "other-n", "a", "v1")
    assert p.evaluate(_req(active_h, "navigate", grant=g2)).outcome is PolicyOutcome.DENY
    # missing actor
    g3 = _FakeGrant(active_h.id, active_h.bound_n_agent_session_id, "", "v1")
    assert p.evaluate(_req(active_h, "navigate", grant=g3)).outcome is PolicyOutcome.DENY
    # missing policy version
    g4 = _FakeGrant(active_h.id, active_h.bound_n_agent_session_id, "a", "")
    assert p.evaluate(_req(active_h, "navigate", grant=g4)).outcome is PolicyOutcome.DENY
    # expired
    g5 = _FakeGrant(active_h.id, active_h.bound_n_agent_session_id, "a", "v1", expired=True)
    assert p.evaluate(_req(active_h, "navigate", grant=g5)).outcome is PolicyOutcome.DENY
    # revoked
    g6 = _FakeGrant(active_h.id, active_h.bound_n_agent_session_id, "a", "v1", revoked=True)
    assert p.evaluate(_req(active_h, "navigate", grant=g6)).outcome is PolicyOutcome.DENY
