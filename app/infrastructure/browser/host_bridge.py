"""Host-side restricted browser bridge with authoritative authorization rereads."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager
import hmac
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, Protocol

from app.domain.browser import (
    BrowserBackendType,
    ClickAction,
    BrowserScreenshotConsumer,
    BrowserSession,
    BrowserSessionStatus,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
    ScrollAction,
    TypeAction,
)
from app.domain.browser_policy import (
    BROWSER_POLICY_VERSION,
    BrowserPolicy,
    BrowserPolicyRequest,
)
from app.domain.policy import PolicyOutcome
from app.infrastructure.browser.host_cdp_backend import load_secure_token
from app.infrastructure.browser.host_grant_store import (
    BrowserAuthorizationStoreError,
    HostAuthorizationSnapshot,
)
from app.infrastructure.browser.host_protocol import PROTOCOL_VERSION


_KNOWN_ACTION_TYPES = frozenset(
    {"navigate", "click", "type", "scroll", "observe", "screenshot"}
)
_SIDE_EFFECTING_ACTION_TYPES = frozenset(
    {"navigate", "click", "type", "scroll"}
)
_SESSION_LOCK_STRIPES = 64
_MAX_EXPIRY_GRACE_SECONDS = 60.0
_MAX_REQUEST_TIMEOUT_SECONDS = 3_600.0
_MAX_REQUEST_DEADLINE_AHEAD_SECONDS = 3_600.0
_MAX_SCROLL_DELTA = 1_000_000
_WAIT_POLL_SECONDS = 0.01
_CONTROLLER_JOB_CAPACITY = 8
_SHUTDOWN_DISPATCH_ACK_SECONDS = 0.1
_DEFAULT_MAX_SESSIONS = 16
_MAX_SESSIONS = 64
_ACTION_FIELDS: dict[str, frozenset[str]] = {
    "navigate": frozenset({"url"}),
    "click": frozenset({"element_ref", "document_revision"}),
    "type": frozenset(
        {"element_ref", "document_revision", "text", "clear_first"}
    ),
    "scroll": frozenset(
        {"element_ref", "document_revision", "dx", "dy"}
    ),
    "observe": frozenset({"max_text_chars", "max_elements"}),
    "screenshot": frozenset({"full_page"}),
}
_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "/v1/browser/session/create": frozenset(
        {
            "protocol_version",
            "session_id",
            "n_agent_session_id",
            "profile_ref",
            "status",
        }
    ),
    "/v1/browser/session/close": frozenset(
        {"protocol_version", "session_id"}
    ),
    "/v1/browser/session/action": frozenset(
        {
            "protocol_version",
            "session_id",
            "action_type",
            "action",
            "document_revision",
        }
    ),
    "/v1/browser/session/state": frozenset(
        {"protocol_version", "session_id"}
    ),
    "/v1/browser/session/takeover/begin": frozenset(
        {"protocol_version", "session_id"}
    ),
    "/v1/browser/session/takeover/end": frozenset(
        {"protocol_version", "session_id"}
    ),
}


class AuthorizationStore(Protocol):
    """Loads a complete authoritative grant/session JOIN snapshot."""

    def load_authorization(
        self, session_id: str
    ) -> HostAuthorizationSnapshot | None: ...


class CdpTargetController(Protocol):
    def create_target(self, profile_ref: str) -> str: ...

    def close_target(self, target_id: str) -> None: ...

    def execute_action(
        self,
        target_id: str,
        action_type: str,
        action: dict[str, Any],
        document_revision: int,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]: ...

    def get_state(
        self,
        target_id: str,
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]: ...

    def shutdown(self) -> bool: ...


@dataclass
class _RegisteredSession:
    session: BrowserSession
    target_id: str
    generation: int = 0
    expiry_deadline_monotonic: float = 0.0
    expiry_authoritative_expires_at: datetime | None = None
    expiry_timer: threading.Timer | None = None
    expiring: bool = False
    in_flight_count: int = 0
    in_flight_cancel: threading.Event | None = None
    in_flight_done: threading.Event = field(default_factory=threading.Event)
    cleanup_claim: object | None = None

    def __post_init__(self) -> None:
        self.in_flight_done.set()


@dataclass
class _TargetCreation:
    done: threading.Event = field(default_factory=threading.Event)
    lock: Any = field(default_factory=threading.Lock)
    target_id: str | None = None
    error: Exception | None = None
    abandoned: bool = False
    slot_owned: bool = True
    on_released: Callable[[], None] | None = None


@dataclass
class _PreparedTargetClose:
    gate: threading.Event = field(default_factory=threading.Event)
    execute: threading.Event = field(default_factory=threading.Event)
    dispatched: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    dispatch_lock: Any = field(default_factory=threading.Lock)
    cancelled: bool = False
    succeeded: bool = False


@dataclass(frozen=True)
class HostBridgeConfig:
    token_path: str | os.PathLike[str]
    bind_host: str = "127.0.0.1"
    port: int = 8766
    max_request_bytes: int = 262_144
    max_concurrency: int = 8
    # Host CDP is a local interactive resource; cap open targets/timers.
    max_sessions: int = _DEFAULT_MAX_SESSIONS
    expiry_grace_seconds: float = 1.0
    default_request_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise ValueError("host_bridge_loopback_required")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("host_bridge_port_invalid")
        if (
            isinstance(self.max_request_bytes, bool)
            or not isinstance(self.max_request_bytes, int)
            or self.max_request_bytes <= 0
            or isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
            or isinstance(self.max_sessions, bool)
            or not isinstance(self.max_sessions, int)
            or not 1 <= self.max_sessions <= _MAX_SESSIONS
        ):
            raise ValueError("host_bridge_limits_invalid")
        if (
            isinstance(self.expiry_grace_seconds, bool)
            or not isinstance(self.expiry_grace_seconds, (int, float))
            or not math.isfinite(self.expiry_grace_seconds)
            or self.expiry_grace_seconds < 0
            or self.expiry_grace_seconds > _MAX_EXPIRY_GRACE_SECONDS
            or isinstance(self.default_request_timeout_seconds, bool)
            or not isinstance(
                self.default_request_timeout_seconds, (int, float)
            )
            or not math.isfinite(self.default_request_timeout_seconds)
            or self.default_request_timeout_seconds <= 0
            or self.default_request_timeout_seconds
            > _MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ValueError("host_bridge_limits_invalid")


class HostBridge:
    """Owns CDP target capabilities and re-authorizes every operation."""

    def __init__(
        self,
        config: HostBridgeConfig,
        *,
        authorization_store: AuthorizationStore,
        cdp_controller: CdpTargetController,
    ) -> None:
        self.config = config
        self._authorization_store = authorization_store
        self._cdp = cdp_controller
        self._token = load_secure_token(Path(config.token_path))
        self._sessions: dict[str, _RegisteredSession] = {}
        self._session_reservations: set[str] = set()
        self._pending_expiry_cleanup: dict[
            str, tuple[_RegisteredSession, int]
        ] = {}
        self._expiry_cleanup_wake = threading.Event()
        self._expiry_cleanup_stop = threading.Event()
        self._expiry_cleanup_thread: threading.Thread | None = None
        # Fixed striped locks bound memory under attacker-controlled session
        # IDs. Hash collisions only serialize unrelated sessions.
        self._session_locks = tuple(
            threading.RLock() for _ in range(_SESSION_LOCK_STRIPES)
        )
        self._registry_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._active_requests = 0
        self._controller_job_slots = threading.BoundedSemaphore(
            _CONTROLLER_JOB_CAPACITY
        )
        self._controller_jobs_lock = threading.Lock()
        self._controller_jobs_active = 0
        self._controller_jobs_idle = threading.Event()
        self._controller_jobs_idle.set()
        self._policy = BrowserPolicy()
        self._healthy = True
        self._health_lock = threading.Lock()
        self._shutdown_started = False
        self._controller_shutdown_called = False
        self._controller_shutdown_result: bool | None = None
        self._shutdown_cleanup_failed = False
        self._shutdown_condition = threading.Condition(self._registry_lock)
        self._shutdown_running = False
        self._shutdown_last_result: bool | None = None
        self._shutdown_waiters = 0
        self._shutdown_waiter_present = threading.Event()

    @property
    def healthy(self) -> bool:
        with self._health_lock:
            return self._healthy

    def authenticate(self, supplied: str | None) -> bool:
        if supplied is None:
            return False
        try:
            encoded = supplied.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(encoded, self._token)

    def shutdown(self) -> bool:
        wait_deadline = (
            time.monotonic()
            + self.config.expiry_grace_seconds
            + _SHUTDOWN_DISPATCH_ACK_SECONDS
        )
        with self._shutdown_condition:
            if (
                not self._shutdown_running
                and not self._sessions
                and self._controller_shutdown_result is not None
            ):
                return bool(self._shutdown_last_result)
            if self._shutdown_running:
                self._shutdown_waiters += 1
                self._shutdown_waiter_present.set()
                try:
                    while self._shutdown_running:
                        remaining = wait_deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._shutdown_condition.wait(timeout=remaining)
                    return bool(self._shutdown_last_result)
                finally:
                    self._shutdown_waiters -= 1
                    if self._shutdown_waiters == 0:
                        self._shutdown_waiter_present.clear()
            self._shutdown_running = True
        try:
            owner_result = self._run_shutdown_owner()
        except Exception:
            owner_result = False
        with self._shutdown_condition:
            if not owner_result:
                self._shutdown_cleanup_failed = True
            shared_result = (
                owner_result and not self._shutdown_cleanup_failed
            )
            self._shutdown_last_result = shared_result
            self._shutdown_running = False
            self._shutdown_condition.notify_all()
            return shared_result

    def _run_shutdown_owner(self) -> bool:
        cleanup_confirmed = True
        with self._registry_lock:
            first_shutdown = not self._shutdown_started
            if first_shutdown:
                self._shutdown_started = True
                with self._health_lock:
                    self._healthy = False
        if not self._stop_expiry_cleanup_scheduler():
            cleanup_confirmed = False
        with self._registry_lock:
            registrations = tuple(self._sessions.items())
            for _, registered in registrations:
                if registered.in_flight_cancel is not None:
                    registered.in_flight_cancel.set()
        grace_deadline = (
            time.monotonic() + self.config.expiry_grace_seconds
        )
        for _, registered in registrations:
            remaining = max(0.0, grace_deadline - time.monotonic())
            if remaining <= 0:
                break
            if not registered.in_flight_done.wait(timeout=remaining):
                cleanup_confirmed = False
        dispatch_deadline = (
            time.monotonic() + _SHUTDOWN_DISPATCH_ACK_SECONDS
        )
        close_jobs: list[_PreparedTargetClose] = []
        for session_id, registered in registrations:
            cleanup_claim = self._claim_target_cleanup(
                session_id, registered
            )
            if cleanup_claim is None:
                cleanup_confirmed = False
                continue
            if not self._try_reserve_controller_job():
                cleanup_confirmed = False
                self._release_target_cleanup_claim(
                    session_id, registered, cleanup_claim
                )
                continue
            try:
                prepared = self._prepare_reserved_target_close(
                    registered.target_id
                )
            except _BridgeDenied:
                cleanup_confirmed = False
                self._release_target_cleanup_claim(
                    session_id, registered, cleanup_claim
                )
                continue
            with self._registry_lock:
                if (
                    self._sessions.get(session_id) is not registered
                    or registered.cleanup_claim is not cleanup_claim
                ):
                    cleanup_confirmed = False
                    self._cancel_prepared_target_close(prepared)
                    continue
            self._commit_prepared_target_close(prepared)
            dispatch_remaining = max(
                0.0, dispatch_deadline - time.monotonic()
            )
            if not prepared.dispatched.wait(timeout=dispatch_remaining):
                if self._cancel_prepared_target_close(prepared):
                    cleanup_confirmed = False
                    self._release_target_cleanup_claim(
                        session_id, registered, cleanup_claim
                    )
                    continue
            self._unregister(
                session_id, expected_cleanup_claim=cleanup_claim
            )
            close_jobs.append(prepared)
        for close_job in close_jobs:
            remaining = max(
                0.0,
                max(grace_deadline, dispatch_deadline)
                - time.monotonic(),
            )
            if remaining <= 0:
                break
            if not close_job.done.wait(timeout=remaining):
                cleanup_confirmed = False
            elif not close_job.succeeded:
                cleanup_confirmed = False
        with self._registry_lock:
            should_shutdown_controller = (
                not self._sessions
                and not self._controller_shutdown_called
            )
            if should_shutdown_controller:
                self._controller_shutdown_called = True
            remaining_sessions = bool(self._sessions)
        if should_shutdown_controller:
            try:
                controller_result = self._cdp.shutdown()
            except Exception:
                controller_result = False
            with self._registry_lock:
                self._controller_shutdown_result = (
                    controller_result is True
                )
        with self._registry_lock:
            controller_shutdown_result = self._controller_shutdown_result
        with self._controller_jobs_lock:
            controller_jobs_active = self._controller_jobs_active
        if (
            remaining_sessions
            or controller_shutdown_result is not True
            or controller_jobs_active != 0
        ):
            cleanup_confirmed = False
        return cleanup_confirmed

    def handle_request(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        deadline_monotonic: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, _error_response("host_bridge_invalid_request")
        if deadline_monotonic is None:
            deadline_monotonic = (
                time.monotonic() + self.config.default_request_timeout_seconds
            )
        now_monotonic = time.monotonic()
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic
            > now_monotonic + _MAX_REQUEST_DEADLINE_AHEAD_SECONDS
        ):
            return 400, _error_response("host_bridge_invalid_request")
        if cancel_event is None:
            cancel_event = threading.Event()
        if not isinstance(cancel_event, threading.Event):
            return 400, _error_response("host_bridge_invalid_request")
        if cancel_event.is_set() or time.monotonic() >= deadline_monotonic:
            return 200, _error_response("host_bridge_timeout")
        with self._admission_lock:
            if self._active_requests >= self.config.max_concurrency:
                return 409, _error_response("host_bridge_busy")
            self._active_requests += 1
        cancellation_timer = _start_cancellation_timer(
            float(deadline_monotonic), cancel_event
        )
        try:
            return self._dispatch(
                path,
                payload,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        except _BridgeDenied as exc:
            return (
                _status_for_denial(exc.error_code),
                _error_response(exc.error_code),
            )
        except Exception:
            return 500, _error_response("host_bridge_internal_error")
        finally:
            if cancellation_timer is not None:
                cancellation_timer.cancel()
            with self._admission_lock:
                self._active_requests -= 1

    def _dispatch(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> tuple[int, dict[str, Any]]:
        expected_fields = _PAYLOAD_FIELDS.get(path)
        if expected_fields is None:
            if payload.get("protocol_version") != PROTOCOL_VERSION:
                return 400, _error_response("host_bridge_invalid_request")
            return 404, _error_response("not_found")
        if (
            set(payload) != expected_fields
            or payload.get("protocol_version") != PROTOCOL_VERSION
        ):
            return 400, _error_response("host_bridge_invalid_request")
        if path == "/v1/browser/session/create":
            return 200, self._handle_create(
                payload,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        if path == "/v1/browser/session/close":
            return 200, self._handle_close(
                payload,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        if path == "/v1/browser/session/action":
            return 200, self._handle_action(
                payload,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        if path == "/v1/browser/session/state":
            return 200, self._handle_state(
                payload,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        return 200, self._handle_takeover(
            path,
            payload,
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )

    def _handle_create(
        self,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = _required_text(payload, "session_id")
        n_agent_session_id = _required_text(
            payload, "n_agent_session_id"
        )
        profile_ref = _required_text(payload, "profile_ref")
        status = _required_text(payload, "status")
        with self._locked_session(
            session_id, deadline_monotonic, cancel_event
        ):
            self._require_healthy()
            snapshot = self._load_authorization(session_id)
            session = self._active_session(snapshot)
            if status != BrowserSessionStatus.ACTIVE.value:
                raise _BridgeDenied("session_not_active")
            if (
                session.id != session_id
                or session.bound_n_agent_session_id != n_agent_session_id
                or session.profile_ref != profile_ref
                or session.status.value != status
            ):
                raise _BridgeDenied("grant_not_found")
            self._evaluate_policy(session, "create", snapshot, None)
            with self._registry_lock:
                existing = self._sessions.get(session_id)
                if existing is None:
                    if (
                        len(self._sessions)
                        + len(self._session_reservations)
                        >= self.config.max_sessions
                    ):
                        raise _BridgeDenied("host_bridge_busy")
                    self._session_reservations.add(session_id)
            if existing is not None:
                if not _same_binding(existing.session, session):
                    raise _BridgeDenied("grant_not_found")
                self._preflight(deadline_monotonic, cancel_event)
                if not self._schedule_expiry(existing, snapshot):
                    raise _BridgeDenied("grant_expired")
                return {"status": "ok"}
            def release_session_reservation() -> None:
                with self._registry_lock:
                    self._session_reservations.discard(session_id)

            try:
                self._preflight(deadline_monotonic, cancel_event)
            except Exception:
                release_session_reservation()
                raise
            target_id, creation = self._create_target_with_deadline(
                snapshot.profile_ref,
                deadline_monotonic,
                cancel_event,
                on_released=release_session_reservation,
            )
            registered = _RegisteredSession(session=session, target_id=target_id)
            publish = False
            timed_out = False
            with self._registry_lock:
                timed_out = (
                    cancel_event.is_set()
                    or time.monotonic() >= deadline_monotonic
                )
                if not self._shutdown_started and not timed_out:
                    self._session_reservations.discard(session_id)
                    self._sessions[session_id] = registered
                    publish = True
            if not publish:
                self._close_created_target(creation, target_id)
                if timed_out:
                    raise _BridgeDenied("host_bridge_timeout")
                raise _BridgeDenied("host_bridge_unhealthy")
            self._release_creation_slot(creation)
            if not self._schedule_expiry(registered, snapshot):
                if not self.healthy:
                    raise _BridgeDenied("host_bridge_unhealthy")
                raise _BridgeDenied("grant_expired")
            return {"status": "ok"}

    def _handle_close(
        self,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = _required_text(payload, "session_id")
        with self._locked_session(
            session_id, deadline_monotonic, cancel_event
        ):
            self._preflight(deadline_monotonic, cancel_event)
            with self._registry_lock:
                registered = self._sessions.get(session_id)
            if registered is not None:
                self._reserve_controller_job(
                    deadline_monotonic, cancel_event
                )
                prepared = self._prepare_reserved_target_close(
                    registered.target_id
                )
                removed = self._unregister(session_id)
                if removed is None:
                    self._cancel_prepared_target_close(prepared)
                    return {"status": "ok"}
                close_done = self._commit_prepared_target_close(
                    prepared
                )
                self._wait_for_event(
                    close_done, deadline_monotonic, cancel_event
                )
        return {"status": "ok"}

    def _handle_action(
        self,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = _required_text(payload, "session_id")
        action_type = _required_text(payload, "action_type")
        if action_type not in _KNOWN_ACTION_TYPES:
            raise _BridgeDenied("unknown_capability")
        action = payload.get("action")
        document_revision = payload.get("document_revision")
        if (
            not isinstance(action, dict)
            or set(action) != _ACTION_FIELDS[action_type]
            or isinstance(document_revision, bool)
            or not isinstance(document_revision, int)
            or document_revision < 0
        ):
            raise _BridgeDenied("host_bridge_invalid_request")
        _validate_action_payload(
            action_type, action, document_revision
        )
        with self._locked_session(
            session_id, deadline_monotonic, cancel_event
        ):
            self._require_healthy()
            snapshot = self._load_authorization(session_id)
            session = self._active_session(snapshot)
            registered = self._registered(
                session_id, permit_expiring=True
            )
            self._require_matching_registration(registered, session)
            self._preflight(deadline_monotonic, cancel_event)
            if not self._schedule_expiry(registered, snapshot):
                raise _BridgeDenied("grant_expired")
            self._evaluate_policy(
                session,
                action_type,
                snapshot,
                (
                    BrowserScreenshotConsumer.DASHBOARD_INTERNAL
                    if action_type == "screenshot"
                    else None
                ),
            )
            self._preflight(deadline_monotonic, cancel_event)
            self._begin_in_flight(registered, cancel_event)
            controller_started = False
            try:
                try:
                    controller_started = True
                    result = self._cdp.execute_action(
                        registered.target_id,
                        action_type,
                        action,
                        document_revision,
                        deadline_monotonic=deadline_monotonic,
                        cancel_event=cancel_event,
                    )
                except TargetClosed:
                    self._unregister(session_id)
                    if cancel_event.is_set():
                        raise _BridgeDenied(
                            _cancellation_error(
                                action_type, controller_started
                            )
                        )
                    raise _BridgeDenied("target_closed")
                except _BridgeDenied:
                    raise
                except Exception as exc:
                    if cancel_event.is_set():
                        raise _BridgeDenied(
                            _cancellation_error(
                                action_type, controller_started
                            )
                        ) from exc
                    raise _BridgeDenied("target_unavailable") from exc
                if cancel_event.is_set() or time.monotonic() >= deadline_monotonic:
                    raise _BridgeDenied(
                        _cancellation_error(
                            action_type, controller_started
                        )
                    )
            finally:
                self._end_in_flight(registered, cancel_event)
            if not isinstance(result, dict):
                raise _BridgeDenied("host_bridge_invalid_response")
            result.setdefault("action_type", action_type)
            result.setdefault("status", "error")
            result.setdefault("document_revision", document_revision)
            return result

    def _handle_state(
        self,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = _required_text(payload, "session_id")
        with self._locked_session(
            session_id, deadline_monotonic, cancel_event
        ):
            self._require_healthy()
            snapshot = self._load_authorization(session_id)
            session = self._active_session(snapshot)
            with self._registry_lock:
                registered = self._sessions.get(session_id)
            if registered is None:
                return {
                    "safe_url": None,
                    "title": None,
                    "status": BrowserSessionStatus.CLOSED.value,
                    "document_revision": 0,
                    "latest_screenshot_ref": None,
                }
            self._require_matching_registration(registered, session)
            self._preflight(deadline_monotonic, cancel_event)
            if not self._schedule_expiry(registered, snapshot):
                raise _BridgeDenied("grant_expired")
            self._evaluate_policy(session, "observe", snapshot, None)
            self._preflight(deadline_monotonic, cancel_event)
            self._begin_in_flight(registered, cancel_event)
            try:
                if cancel_event.is_set():
                    raise _BridgeDenied("host_bridge_timeout")
                try:
                    state = self._cdp.get_state(
                        registered.target_id,
                        deadline_monotonic=deadline_monotonic,
                        cancel_event=cancel_event,
                    )
                except TargetClosed:
                    self._unregister(session_id)
                    raise _BridgeDenied("target_closed")
                except Exception as exc:
                    if cancel_event.is_set():
                        raise _BridgeDenied("host_bridge_timeout") from exc
                    raise _BridgeDenied("target_unavailable") from exc
                if cancel_event.is_set() or time.monotonic() >= deadline_monotonic:
                    raise _BridgeDenied("host_bridge_timeout")
            finally:
                self._end_in_flight(registered, cancel_event)
            if not isinstance(state, dict):
                raise _BridgeDenied("host_bridge_invalid_response")
            return state

    def _handle_takeover(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        session_id = _required_text(payload, "session_id")
        with self._locked_session(
            session_id, deadline_monotonic, cancel_event
        ):
            self._require_healthy()
            registered = self._registered(
                session_id, permit_expiring=True
            )
            snapshot = self._load_authorization(session_id)
            current = BrowserSession(
                id=snapshot.browser_session_id,
                bound_n_agent_session_id=snapshot.n_agent_session_id,
                backend_type=snapshot.backend_type,
                status=snapshot.status,
                profile_ref=snapshot.profile_ref,
            )
            self._require_matching_registration(registered, current)
            if current.status in {
                BrowserSessionStatus.DEGRADED,
                BrowserSessionStatus.CLOSED,
            }:
                raise _BridgeDenied("session_not_active")
            self._preflight(deadline_monotonic, cancel_event)
            if not self._schedule_expiry(registered, snapshot):
                raise _BridgeDenied("grant_expired")
        if path == "/v1/browser/session/takeover/begin":
            return {"status": "ok", "takeover_url": None}
        return {"status": "ok"}

    def _load_authorization(
        self, session_id: str
    ) -> HostAuthorizationSnapshot:
        try:
            snapshot = self._authorization_store.load_authorization(session_id)
        except BrowserAuthorizationStoreError as exc:
            raise _BridgeDenied("host_bridge_unhealthy") from exc
        except Exception as exc:
            raise _BridgeDenied("host_bridge_unhealthy") from exc
        if snapshot is None:
            raise _BridgeDenied("grant_not_found")
        if not isinstance(snapshot, HostAuthorizationSnapshot):
            raise _BridgeDenied("host_bridge_unhealthy")
        if snapshot.browser_session_id != session_id:
            raise _BridgeDenied("grant_not_found")
        if snapshot.expired:
            raise _BridgeDenied("grant_expired")
        if snapshot.policy_version != BROWSER_POLICY_VERSION:
            raise _BridgeDenied("host_policy_version_mismatch")
        if snapshot.backend_type is not BrowserBackendType.HOST_CDP:
            raise _BridgeDenied("host_grant_required")
        return snapshot

    @staticmethod
    def _active_session(snapshot: HostAuthorizationSnapshot) -> BrowserSession:
        session = BrowserSession(
            id=snapshot.browser_session_id,
            bound_n_agent_session_id=snapshot.n_agent_session_id,
            backend_type=snapshot.backend_type,
            status=snapshot.status,
            profile_ref=snapshot.profile_ref,
        )
        if session.status is not BrowserSessionStatus.ACTIVE:
            raise _BridgeDenied("session_not_active")
        return session

    def _evaluate_policy(
        self,
        session: BrowserSession,
        action_type: str,
        snapshot: HostAuthorizationSnapshot,
        screenshot_consumer: BrowserScreenshotConsumer | None,
    ) -> None:
        decision = self._policy.evaluate(
            BrowserPolicyRequest(
                run_context=None,
                session=session,
                action_type=action_type,
                requested_backend=BrowserBackendType.HOST_CDP,
                trusted_host_grant=snapshot,
                screenshot_consumer=screenshot_consumer,
                takeover_operation=None,
            )
        )
        if decision.outcome is not PolicyOutcome.ALLOW:
            raise _BridgeDenied(decision.reason)

    def _registered(
        self, session_id: str, *, permit_expiring: bool = False
    ) -> _RegisteredSession:
        with self._registry_lock:
            registered = self._sessions.get(session_id)
        if registered is None:
            raise _BridgeDenied("session_not_found")
        if registered.expiring and not permit_expiring:
            raise _BridgeDenied("grant_expired")
        return registered

    @staticmethod
    def _require_matching_registration(
        registered: _RegisteredSession, current: BrowserSession
    ) -> None:
        if not _same_binding(registered.session, current):
            raise _BridgeDenied("grant_not_found")

    def _schedule_expiry(
        self,
        registered: _RegisteredSession,
        snapshot: HostAuthorizationSnapshot,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        with self._registry_lock:
            if (
                self._sessions.get(snapshot.browser_session_id)
                is not registered
                or (
                    expected_generation is not None
                    and registered.generation != expected_generation
                )
            ):
                return False
            current_expires_at = (
                registered.expiry_authoritative_expires_at
            )
            if current_expires_at is not None:
                if snapshot.expires_at < current_expires_at:
                    # Authority shortened the grant. Never derive a later
                    # monotonic deadline from a rolled-back wall clock.
                    registered.expiring = True
                    return False
                if snapshot.expires_at == current_expires_at:
                    if (
                        registered.expiring
                        or time.monotonic()
                        >= registered.expiry_deadline_monotonic
                    ):
                        registered.expiring = True
                        return False
                    # An unchanged authoritative expiry preserves this
                    # generation, its monotonic deadline, and its timer.
                    return True
                deadline = (
                    registered.expiry_deadline_monotonic
                    + (snapshot.expires_at - current_expires_at).total_seconds()
                )
            else:
                remaining = max(
                    0.0,
                    (
                        snapshot.expires_at - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
                deadline = time.monotonic() + remaining
            remaining = max(0.0, deadline - time.monotonic())
            if registered.expiry_timer is not None:
                registered.expiry_timer.cancel()
            registered.generation += 1
            generation = registered.generation
            registered.expiring = False
            self._pending_expiry_cleanup.pop(
                snapshot.browser_session_id, None
            )
            registered.expiry_deadline_monotonic = deadline
            registered.expiry_authoritative_expires_at = snapshot.expires_at
            timer = threading.Timer(
                remaining,
                self._expire_session,
                args=(snapshot.browser_session_id, registered, generation),
            )
            timer.daemon = True
            registered.expiry_timer = timer
            timer.start()
            return True

    def _expire_session(
        self,
        session_id: str,
        registered: _RegisteredSession,
        generation: int,
    ) -> None:
        with self._registry_lock:
            if (
                self._sessions.get(session_id) is not registered
                or registered.generation != generation
            ):
                return
            remaining = (
                registered.expiry_deadline_monotonic - time.monotonic()
            )
            if remaining > 0:
                # Timer scheduling is not an authority source. An early
                # callback is re-armed against the stored monotonic deadline
                # without consulting wall time or the database.
                timer = threading.Timer(
                    remaining,
                    self._expire_session,
                    args=(session_id, registered, generation),
                )
                timer.daemon = True
                registered.expiry_timer = timer
                timer.start()
                return
            authoritative_expires_at = (
                registered.expiry_authoritative_expires_at
            )
            registered.expiring = True
        if authoritative_expires_at is None:
            self._unregister_expired(
                session_id, registered, expected_generation=generation
            )
            return
        if self._refresh_expiry_from_authority(
            session_id,
            registered,
            expected_generation=generation,
            previous_expires_at=authoritative_expires_at,
        ):
            # A grant may be renewed solely in SQLite with no intervening
            # bridge request. Only the timer generation that observed the old
            # deadline may publish this refreshed deadline.
            return
        with self._registry_lock:
            if (
                self._sessions.get(session_id) is not registered
                or registered.generation != generation
            ):
                return
            if registered.in_flight_cancel is not None:
                registered.in_flight_cancel.set()
            done = registered.in_flight_done
        done.wait(timeout=self.config.expiry_grace_seconds)
        if self._refresh_expiry_from_authority(
            session_id,
            registered,
            expected_generation=generation,
            previous_expires_at=authoritative_expires_at,
        ):
            return
        self._unregister_expired(
            session_id, registered, expected_generation=generation
        )

    def _refresh_expiry_from_authority(
        self,
        session_id: str,
        registered: _RegisteredSession,
        *,
        expected_generation: int,
        previous_expires_at: datetime,
    ) -> bool:
        try:
            snapshot = self._load_authorization(session_id)
            current = self._active_session(snapshot)
            self._require_matching_registration(registered, current)
        except _BridgeDenied:
            return False
        if snapshot.expires_at <= previous_expires_at:
            return False
        return self._schedule_expiry(
            registered,
            snapshot,
            expected_generation=expected_generation,
        )

    def _unregister_expired(
        self,
        session_id: str,
        registered: _RegisteredSession,
        *,
        expected_generation: int,
    ) -> None:
        # The controller owner may ignore cancellation forever. Expiry cleanup
        # therefore cannot wait on its per-session lock after the bounded
        # grace. Identity + generation make this atomic against renewal,
        # replacement, close, and shutdown.
        if not self._try_reserve_controller_job():
            self._queue_expiry_cleanup(
                session_id, registered, expected_generation
            )
            return
        self._unregister_expired_reserved(
            session_id,
            registered,
            expected_generation=expected_generation,
        )

    def _unregister_expired_reserved(
        self,
        session_id: str,
        registered: _RegisteredSession,
        *,
        expected_generation: int,
    ) -> None:
        try:
            prepared = self._prepare_reserved_target_close(
                registered.target_id
            )
        except _BridgeDenied:
            return
        with self._registry_lock:
            if (
                self._sessions.get(session_id) is not registered
                or registered.generation != expected_generation
                or registered.cleanup_claim is not None
            ):
                self._cancel_prepared_target_close(prepared)
                return
            self._sessions.pop(session_id, None)
            self._pending_expiry_cleanup.pop(session_id, None)
            if registered.expiry_timer is not None:
                registered.expiry_timer.cancel()
                registered.expiry_timer = None
        close_done = self._commit_prepared_target_close(
            prepared
        )
        close_done.wait(timeout=self.config.expiry_grace_seconds)

    def _queue_expiry_cleanup(
        self,
        session_id: str,
        registered: _RegisteredSession,
        expected_generation: int,
    ) -> None:
        with self._registry_lock:
            if (
                self._shutdown_started
                or self._sessions.get(session_id) is not registered
                or registered.generation != expected_generation
                or not registered.expiring
            ):
                return
            self._pending_expiry_cleanup[session_id] = (
                registered,
                expected_generation,
            )
            thread = self._expiry_cleanup_thread
            if thread is None or not thread.is_alive():
                self._expiry_cleanup_stop.clear()
                thread = threading.Thread(
                    target=self._run_expiry_cleanup_scheduler,
                    name="host-bridge-expiry-cleanup",
                    daemon=True,
                )
                self._expiry_cleanup_thread = thread
                thread.start()
            self._expiry_cleanup_wake.set()

    def _run_expiry_cleanup_scheduler(self) -> None:
        current_thread = threading.current_thread()
        try:
            while True:
                self._expiry_cleanup_wake.wait()
                self._expiry_cleanup_wake.clear()
                if self._expiry_cleanup_stop.is_set():
                    return
                while not self._expiry_cleanup_stop.is_set():
                    with self._registry_lock:
                        candidate = next(
                            iter(self._pending_expiry_cleanup.items()),
                            None,
                        )
                        if candidate is not None:
                            session_id, (
                                registered,
                                expected_generation,
                            ) = candidate
                            self._pending_expiry_cleanup.pop(
                                session_id, None
                            )
                    if candidate is None:
                        break
                    if not self._try_reserve_controller_job():
                        with self._registry_lock:
                            if (
                                not self._shutdown_started
                                and self._sessions.get(session_id)
                                is registered
                                and registered.generation
                                == expected_generation
                                and registered.expiring
                            ):
                                self._pending_expiry_cleanup[
                                    session_id
                                ] = (
                                    registered,
                                    expected_generation,
                                )
                        break
                    self._unregister_expired_reserved(
                        session_id,
                        registered,
                        expected_generation=expected_generation,
                    )
        finally:
            with self._registry_lock:
                if self._expiry_cleanup_thread is current_thread:
                    self._expiry_cleanup_thread = None

    def _stop_expiry_cleanup_scheduler(self) -> bool:
        with self._registry_lock:
            thread = self._expiry_cleanup_thread
            self._pending_expiry_cleanup.clear()
            self._expiry_cleanup_stop.set()
            self._expiry_cleanup_wake.set()
        if thread is None:
            return True
        thread.join(timeout=_SHUTDOWN_DISPATCH_ACK_SECONDS)
        return not thread.is_alive()

    def _begin_in_flight(
        self, registered: _RegisteredSession, cancel_event: threading.Event
    ) -> None:
        with self._registry_lock:
            if self._shutdown_started:
                raise _BridgeDenied("host_bridge_unhealthy")
            if (
                self._sessions.get(registered.session.id) is not registered
            ):
                raise _BridgeDenied("session_not_found")
            if registered.expiring:
                raise _BridgeDenied("grant_expired")
            registered.in_flight_count += 1
            registered.in_flight_cancel = cancel_event
            registered.in_flight_done.clear()

    def _end_in_flight(
        self, registered: _RegisteredSession, cancel_event: threading.Event
    ) -> None:
        with self._registry_lock:
            registered.in_flight_count = max(
                0, registered.in_flight_count - 1
            )
            if registered.in_flight_cancel is cancel_event:
                registered.in_flight_cancel = None
            if registered.in_flight_count == 0:
                registered.in_flight_done.set()

    def _claim_target_cleanup(
        self, session_id: str, registered: _RegisteredSession
    ) -> object | None:
        claim = object()
        with self._registry_lock:
            if (
                self._sessions.get(session_id) is not registered
                or registered.cleanup_claim is not None
            ):
                return None
            registered.cleanup_claim = claim
        return claim

    def _release_target_cleanup_claim(
        self,
        session_id: str,
        registered: _RegisteredSession,
        cleanup_claim: object,
    ) -> None:
        with self._registry_lock:
            if (
                self._sessions.get(session_id) is registered
                and registered.cleanup_claim is cleanup_claim
            ):
                registered.cleanup_claim = None

    def _unregister(
        self,
        session_id: str,
        *,
        expected_cleanup_claim: object | None = None,
    ) -> _RegisteredSession | None:
        with self._registry_lock:
            registered = self._sessions.get(session_id)
            if registered is None:
                return None
            if expected_cleanup_claim is None:
                if registered.cleanup_claim is not None:
                    return None
            elif registered.cleanup_claim is not expected_cleanup_claim:
                return None
            self._sessions.pop(session_id, None)
            self._pending_expiry_cleanup.pop(session_id, None)
            registered.cleanup_claim = None
            if registered is not None and registered.expiry_timer is not None:
                registered.expiry_timer.cancel()
                registered.expiry_timer = None
        return registered

    def _session_lock(self, session_id: str) -> threading.RLock:
        return self._session_locks[
            hash(session_id) % len(self._session_locks)
        ]

    @contextmanager
    def _locked_session(
        self,
        session_id: str,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> Iterator[None]:
        lock = self._session_lock(session_id)
        acquired = False
        try:
            while not acquired:
                self._preflight(deadline_monotonic, cancel_event)
                remaining = deadline_monotonic - time.monotonic()
                acquired = lock.acquire(
                    timeout=min(_WAIT_POLL_SECONDS, remaining)
                )
            self._preflight(deadline_monotonic, cancel_event)
            yield
        finally:
            if acquired:
                lock.release()

    @staticmethod
    def _preflight(
        deadline_monotonic: float, cancel_event: threading.Event
    ) -> None:
        if cancel_event.is_set() or time.monotonic() >= deadline_monotonic:
            raise _BridgeDenied("host_bridge_timeout")

    def _wait_for_event(
        self,
        done: threading.Event,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> None:
        while not done.is_set():
            self._preflight(deadline_monotonic, cancel_event)
            remaining = deadline_monotonic - time.monotonic()
            done.wait(timeout=min(_WAIT_POLL_SECONDS, remaining))
        self._preflight(deadline_monotonic, cancel_event)

    def _create_target_with_deadline(
        self,
        profile_ref: str,
        deadline_monotonic: float,
        cancel_event: threading.Event,
        *,
        on_released: Callable[[], None],
    ) -> tuple[str, _TargetCreation]:
        try:
            self._reserve_controller_job(deadline_monotonic, cancel_event)
        except Exception:
            on_released()
            raise
        creation = _TargetCreation(on_released=on_released)

        def create() -> None:
            try:
                target_id = self._cdp.create_target(profile_ref)
            except Exception as exc:
                release_slot = False
                with creation.lock:
                    if creation.abandoned:
                        release_slot = True
                    else:
                        creation.error = exc
                    creation.done.set()
                if release_slot:
                    self._release_creation_slot(creation)
                return
            close_late = False
            with creation.lock:
                if creation.abandoned:
                    close_late = True
                else:
                    creation.target_id = target_id
                creation.done.set()
            if close_late:
                try:
                    self._cdp.close_target(target_id)
                except Exception:
                    pass
                finally:
                    self._release_creation_slot(creation)

        try:
            threading.Thread(target=create, daemon=True).start()
        except Exception as exc:
            self._release_creation_slot(creation)
            raise _BridgeDenied("target_unavailable") from exc
        try:
            self._wait_for_event(
                creation.done, deadline_monotonic, cancel_event
            )
        except _BridgeDenied:
            self._abandon_creation(creation)
            raise
        with creation.lock:
            error = creation.error
            target_id = creation.target_id
            creation.target_id = None
        if error is not None:
            self._release_creation_slot(creation)
            raise _BridgeDenied("target_unavailable") from error
        if target_id is None:
            self._release_creation_slot(creation)
            raise _BridgeDenied("target_unavailable")
        return target_id, creation

    def _abandon_creation(self, creation: _TargetCreation) -> None:
        with creation.lock:
            creation.abandoned = True
            target_id = creation.target_id
            creation.target_id = None
            completed = creation.done.is_set()
        if target_id is not None:
            self._close_created_target(creation, target_id)
        elif completed:
            self._release_creation_slot(creation)

    def _close_created_target(
        self, creation: _TargetCreation, target_id: str
    ) -> threading.Event:
        done = threading.Event()

        def close() -> None:
            try:
                self._cdp.close_target(target_id)
            except Exception:
                pass
            finally:
                self._release_creation_slot(creation)
                done.set()

        try:
            threading.Thread(target=close, daemon=True).start()
        except Exception:
            self._release_creation_slot(creation)
            done.set()
        return done

    def _release_creation_slot(self, creation: _TargetCreation) -> None:
        release = False
        with creation.lock:
            if creation.slot_owned:
                creation.slot_owned = False
                release = True
        if release:
            self._release_controller_job()
            if creation.on_released is not None:
                creation.on_released()

    def _reserve_controller_job(
        self,
        deadline_monotonic: float,
        cancel_event: threading.Event,
    ) -> None:
        acquired = False
        while not acquired:
            self._preflight(deadline_monotonic, cancel_event)
            remaining = deadline_monotonic - time.monotonic()
            acquired = self._controller_job_slots.acquire(
                timeout=min(_WAIT_POLL_SECONDS, remaining)
            )
        with self._controller_jobs_lock:
            self._controller_jobs_active += 1
            self._controller_jobs_idle.clear()

    def _try_reserve_controller_job(self) -> bool:
        if not self._controller_job_slots.acquire(blocking=False):
            return False
        with self._controller_jobs_lock:
            self._controller_jobs_active += 1
            self._controller_jobs_idle.clear()
        return True

    def _release_controller_job(self) -> None:
        with self._controller_jobs_lock:
            self._controller_jobs_active -= 1
            if self._controller_jobs_active == 0:
                self._controller_jobs_idle.set()
        self._controller_job_slots.release()
        self._expiry_cleanup_wake.set()

    def _prepare_reserved_target_close(
        self, target_id: str
    ) -> _PreparedTargetClose:
        prepared = _PreparedTargetClose()

        def close() -> None:
            prepared.gate.wait()
            try:
                with prepared.dispatch_lock:
                    should_execute = (
                        prepared.execute.is_set()
                        and not prepared.cancelled
                    )
                    if should_execute:
                        prepared.dispatched.set()
                if should_execute:
                    self._cdp.close_target(target_id)
                    prepared.succeeded = True
            except Exception:
                pass
            finally:
                self._release_controller_job()
                prepared.done.set()

        try:
            threading.Thread(target=close, daemon=True).start()
        except Exception as exc:
            self._release_controller_job()
            raise _BridgeDenied("target_unavailable") from exc
        return prepared

    @staticmethod
    def _commit_prepared_target_close(
        prepared: _PreparedTargetClose,
    ) -> threading.Event:
        prepared.execute.set()
        prepared.gate.set()
        return prepared.done

    @staticmethod
    def _cancel_prepared_target_close(
        prepared: _PreparedTargetClose,
    ) -> bool:
        with prepared.dispatch_lock:
            if prepared.dispatched.is_set():
                return False
            prepared.cancelled = True
        prepared.gate.set()
        return True

    def _require_healthy(self) -> None:
        if not self.healthy:
            raise _BridgeDenied("host_bridge_unhealthy")


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise _BridgeDenied("host_bridge_invalid_request")
    return value


def _validate_action_payload(
    action_type: str,
    action: dict[str, Any],
    document_revision: int,
) -> None:
    """Validate wire values with the Domain action contracts before IO."""
    try:
        if action_type == "navigate":
            NavigateAction(url=action["url"])
            return
        if action_type == "observe":
            ObserveAction(
                max_text_chars=action["max_text_chars"],
                max_elements=action["max_elements"],
            )
            return
        if action_type == "click":
            nested_revision = action["document_revision"]
            if nested_revision != document_revision:
                raise ValueError("conflicting_document_revision")
            ClickAction(
                element_ref=action["element_ref"],
                document_revision=nested_revision,
            )
            return
        if action_type == "type":
            nested_revision = action["document_revision"]
            if (
                nested_revision != document_revision
                or type(action["clear_first"]) is not bool
                or not isinstance(action["text"], str)
                or not action["text"]
            ):
                raise ValueError("invalid_type_action")
            TypeAction(
                element_ref=action["element_ref"],
                document_revision=nested_revision,
                text=action["text"],
                clear_first=action["clear_first"],
            )
            return
        if action_type == "scroll":
            nested_revision = action["document_revision"]
            dx = action["dx"]
            dy = action["dy"]
            if (
                nested_revision != document_revision
                or type(dx) is not int
                or type(dy) is not int
                or abs(dx) > _MAX_SCROLL_DELTA
                or abs(dy) > _MAX_SCROLL_DELTA
            ):
                raise ValueError("invalid_scroll_action")
            ScrollAction(
                element_ref=action["element_ref"],
                document_revision=nested_revision,
                dx=dx,
                dy=dy,
            )
            return
        if action_type == "screenshot":
            if type(action["full_page"]) is not bool:
                raise ValueError("invalid_screenshot_action")
            ScreenshotAction(full_page=action["full_page"])
            return
        raise ValueError("unknown_action")
    except (KeyError, TypeError, ValueError):
        raise _BridgeDenied("host_bridge_invalid_request") from None


def _same_binding(left: BrowserSession, right: BrowserSession) -> bool:
    return (
        left.id == right.id
        and left.bound_n_agent_session_id == right.bound_n_agent_session_id
        and left.backend_type is right.backend_type
        and left.profile_ref == right.profile_ref
    )


def _cancellation_error(
    action_type: str, controller_started: bool
) -> str:
    if controller_started and action_type in _SIDE_EFFECTING_ACTION_TYPES:
        return "action_outcome_unknown"
    return "host_bridge_timeout"


def _start_cancellation_timer(
    deadline_monotonic: float, cancel_event: threading.Event
) -> threading.Timer | None:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        cancel_event.set()
        return None
    timer = threading.Timer(remaining, cancel_event.set)
    timer.daemon = True
    timer.start()
    return timer


def _error_response(error_code: str) -> dict[str, Any]:
    return {"status": "error", "error_code": error_code}


def _status_for_denial(error_code: str) -> int:
    if error_code == "not_found":
        return 404
    if error_code == "host_bridge_busy":
        return 409
    if error_code == "host_bridge_unhealthy":
        return 503
    return 200


class _BridgeDenied(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class TargetClosed(Exception):
    """Raised by controllers when the registered target no longer exists."""


__all__ = [
    "AuthorizationStore",
    "CdpTargetController",
    "HostBridge",
    "HostBridgeConfig",
    "TargetClosed",
]
