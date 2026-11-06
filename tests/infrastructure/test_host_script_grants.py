from dataclasses import replace
import hashlib
import pytest
from tests.domain.test_host_script_grant import grant
import importlib


def store_module():
    return importlib.import_module(
        "app.infrastructure.host_terminal.script_grant_store"
    )


def test_store_restart_revision_replay_and_tombstone(tmp_path):
    s = store_module().HostScriptGrantStore(
        tmp_path / "snapshots", forbidden_roots=(tmp_path / "skills",)
    )
    assert s.list() == []
    a = grant()
    assert s.apply("grant", a) == [a]
    assert s.apply("grant", a) == [a]
    b = replace(a, revision=2, sha256="b" * 64)
    s.apply("grant", b)
    with pytest.raises(store_module().HostScriptGrantStoreError):
        s.apply("grant", a)
    tomb = replace(b, state="revoked")
    assert s.apply("revoke", tomb) == [tomb]
    assert s.apply("revoke", tomb) == [tomb]
    with pytest.raises(store_module().HostScriptGrantStoreError):
        s.apply("grant", b)
    assert store_module().HostScriptGrantStore(
        tmp_path / "snapshots", forbidden_roots=()
    ).list() == [tomb]


def test_store_rejects_symlink_and_corrupt(tmp_path):
    s = store_module().HostScriptGrantStore(tmp_path / "snapshots", forbidden_roots=())
    s.apply("grant", grant())
    s.path.write_text("corrupt")
    with pytest.raises(store_module().HostScriptGrantStoreError):
        s.list()
    other = tmp_path / "other"
    other.write_text("[]")
    s.path.unlink()
    s.path.symlink_to(other)
    with pytest.raises(store_module().HostScriptGrantStoreError):
        s.list()


from tests.infrastructure.test_host_terminal_bridge import (
    _bridge,
    _skill_policy,
    _payload,
)
from app.infrastructure.host_terminal.http_client import (
    HostTerminalHttpClient,
    HostTerminalHttpClientConfig,
    HostTerminalBridgeClientError,
)
import httpx


def test_bridge_grants_verify_source_and_yaml_fallback(tmp_path):
    source = b'print("ok")'
    bridge = _bridge(tmp_path, _skill_policy(source))
    script = tmp_path / "skills/camera/scripts/run.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(source)
    g = grant(sha256=hashlib.sha256(source).hexdigest())
    assert hasattr(bridge, "script_grants_payload")
    assert bridge.script_grants_payload(
        {"operation": "grant", "grant": g.to_dict()}
    ) == (200, {"grants": [g.to_dict()], "error_code": None})
    script.write_bytes(b"changed")
    bad = replace(g, revision=2)
    assert (
        bridge.script_grants_payload({"operation": "grant", "grant": bad.to_dict()})[0]
        == 409
    )
    script.unlink()
    script.symlink_to(tmp_path / "outside")
    assert (
        bridge.script_grants_payload({"operation": "grant", "grant": bad.to_dict()})[0]
        != 200
    )
    bridge.grant_store.path.write_text("corrupt")
    script.unlink()
    script.write_bytes(source)
    target = {
        "type": "skill_script",
        "skill_name": "camera",
        "script_relative_path": "scripts/run.py",
        "sha256": g.sha256,
        "args": [],
    }
    bridge._execute_authorized = lambda *a, **kw: {"executed": True}
    assert bridge.execute_payload(_payload(bridge, target)) == (200, {"executed": True})


@pytest.mark.asyncio
async def test_client_grants_strict_protocol_and_status():
    g = grant()

    async def handle(req):
        assert req.url.path == "/v1/script-grants"
        assert req.headers["X-N-Agent-Host-Token"] == "z" * 32
        return httpx.Response(200, json={"grants": [g.to_dict()], "error_code": None})

    client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token="z" * 32,
            transport=httpx.MockTransport(handle),
        )
    )
    assert hasattr(client, "script_grants")
    assert await client.script_grants("list") == [g]
    for status, body in [
        (200, {"grants": [], "error_code": None, "extra": 1}),
        (500, {"grants": [], "error_code": None}),
    ]:
        client._config = replace(
            client._config,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(status, json=body)
            ),
        )
        with pytest.raises(HostTerminalBridgeClientError):
            await client.script_grants("list")


import http.client
import json
import threading
from app.infrastructure.host_terminal.bridge_server import make_server


def test_http_grant_auth_and_body_bounds(tmp_path):
    bridge = _bridge(tmp_path, _skill_policy(b"print(1)"))
    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for token, content_type, body, expected in [
            (None, "application/json", "{}", 401),
            ("wrong", "application/json", "{}", 401),
            ("z" * 32, "application/json", '{"operation":"list","grant":null}', 200),
            ("z" * 32, "text/plain", "{}", 415),
            ("z" * 32, "application/json", "{}", 413),
            ("z" * 32, "application/json", '{"operation":"unknown","grant":null}', 400),
        ]:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {"Content-Type": content_type}
            if token is not None:
                headers["X-N-Agent-Host-Token"] = token
            if expected == 413:
                headers["Content-Length"] = "262145"
            connection.request("POST", "/v1/script-grants", body=body, headers=headers)
            response = connection.getresponse()
            assert response.status == expected
            assert set(json.loads(response.read())) == {"grants", "error_code"}
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_auto_grant_executes_pinned_source_and_literal_argv(tmp_path):
    source = b"import sys; print(sys.argv[1])"
    bridge = _bridge(tmp_path, _skill_policy(b"old"))
    script = tmp_path / "skills/camera/scripts/run.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(source)
    g = grant(sha256=hashlib.sha256(source).hexdigest())
    assert (
        bridge.script_grants_payload({"operation": "grant", "grant": g.to_dict()})[0]
        == 200
    )
    target = dict(
        type="skill_script",
        skill_name="camera",
        script_relative_path="scripts/run.py",
        sha256=g.sha256,
        args=["hello $literal"],
    )
    status, result = bridge.execute_payload(_payload(bridge, target))
    assert status == 200
    assert result["error_code"] is None
    assert result["stdout"] == "hello $literal\n"
    script.write_bytes(b'print("external")')
    assert (
        bridge.execute_payload(_payload(bridge, target))[1]["error_code"]
        == "skill_script_hash_mismatch"
    )


@pytest.mark.asyncio
async def test_client_grant_bounds_and_network():
    for transport, maximum, expected in [
        (
            httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 100)),
            20,
            "host_bridge_invalid_response",
        ),
        (
            httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline"))
            ),
            1000,
            "host_bridge_unavailable",
        ),
    ]:
        client = HostTerminalHttpClient(
            HostTerminalHttpClientConfig(
                "http://host.docker.internal:8765",
                token="z" * 32,
                transport=transport,
                max_response_bytes=maximum,
            )
        )
        with pytest.raises(HostTerminalBridgeClientError) as error:
            await client.script_grants("list")
        assert error.value.error_code == expected


def test_revoke_timestamp_can_change_without_new_write_revision(tmp_path):
    store = store_module().HostScriptGrantStore(tmp_path / "snapshots")
    active = grant()
    store.apply("grant", active)
    revoked = replace(active, state="revoked", updated_at="2026-11-06T01:00:00Z")
    assert store.apply("revoke", revoked) == [revoked]
    assert store.apply(
        "revoke", replace(revoked, updated_at="2026-11-06T02:00:00Z")
    ) == [revoked]


def test_fifo_store_read_is_bounded_and_yaml_remains_available(tmp_path):
    import os

    source = b'print("ok")'
    bridge = _bridge(tmp_path, _skill_policy(source))
    bridge.grant_store.list()
    os.mkfifo(bridge.grant_store.path, 0o600)
    finished = threading.Event()
    errors = []

    def read_store():
        try:
            bridge.grant_store.list()
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    reader = threading.Thread(target=read_store, daemon=True)
    reader.start()
    completed_without_writer = finished.wait(0.3)
    if not completed_without_writer:
        # Release an old blocking implementation so the regression never leaves
        # a worker holding the grant lock after the assertion fails.
        writer = os.open(bridge.grant_store.path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        reader.join(timeout=1)
    assert completed_without_writer
    assert len(errors) == 1
    assert isinstance(errors[0], store_module().HostScriptGrantStoreError)
    assert_yaml_fallback(bridge, source)


def assert_yaml_fallback(bridge, source):
    script = bridge.config.skills_root / "camera/scripts/run.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(source)
    target = dict(
        type="skill_script",
        skill_name="camera",
        script_relative_path="scripts/run.py",
        sha256=hashlib.sha256(source).hexdigest(),
        args=[],
    )
    bridge._execute_authorized = lambda *args, **kwargs: {"executed": True}
    assert bridge.execute_payload(_payload(bridge, target)) == (200, {"executed": True})


def test_nested_corrupt_store_normalizes_error_and_keeps_yaml(tmp_path):
    source = b'print("ok")'
    bridge = _bridge(tmp_path, _skill_policy(source))
    bridge.grant_store.list()
    bridge.grant_store.path.write_text("[" * 100000 + "0" + "]" * 100000)
    bridge.grant_store.path.chmod(0o600)
    with pytest.raises(store_module().HostScriptGrantStoreError):
        bridge.grant_store.list()
    assert_yaml_fallback(bridge, source)


@pytest.mark.asyncio
async def test_client_nested_json_response_has_stable_protocol_error():
    raw = b"[" * 100000 + b"0" + b"]" * 100000
    client = HostTerminalHttpClient(
        HostTerminalHttpClientConfig(
            "http://host.docker.internal:8765",
            token="z" * 32,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=raw)
            ),
        )
    )
    with pytest.raises(HostTerminalBridgeClientError) as error:
        await client.script_grants("list")
    assert error.value.error_code == "host_bridge_invalid_response"


def test_http_nested_grant_request_returns_stable_bad_request(tmp_path):
    bridge = _bridge(tmp_path, _skill_policy(b"print(1)"))
    server = make_server(bridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        body = "[" * 100000 + "0" + "]" * 100000
        connection.request(
            "POST",
            "/v1/script-grants",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-N-Agent-Host-Token": "z" * 32,
            },
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {
            "grants": [],
            "error_code": "host_bridge_invalid_request",
        }
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()
