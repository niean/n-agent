import asyncio
import json
import logging

import httpx
import pytest

from app.infrastructure.feishu.client import FeishuClient, FeishuConfig, FeishuVerificationError, _event_to_dict


def client(**kwargs):
    values = {
        "app_id": "app-1",
        "app_secret": "secret",
        "tenant_key": "tenant-1",
        "allowed_open_ids": ["ou_1"],
        "allowed_chat_ids": ["oc_1"],
    }
    values.update(kwargs)
    return FeishuClient(FeishuConfig(**values))


def event_payload(**event):
    return {"schema": "2.0", "header": {"app_id": "app-1", "tenant_key": "tenant-1"}, "event": event}


def test_verify_long_connection_event_accepts_valid_allowed_event():
    feishu = client()
    payload = event_payload(sender={"sender_id": {"open_id": "ou_1"}}, message={"chat_id": "oc_1"})

    verified = feishu.verify_long_connection_event(payload)

    assert verified["event"]["message"]["chat_id"] == "oc_1"


@pytest.mark.parametrize("field", ["app_id", "tenant_key"])
def test_verify_long_connection_event_rejects_header_mismatch(field):
    feishu = client()
    payload = event_payload(sender={"sender_id": {"open_id": "ou_1"}}, message={"chat_id": "oc_1"})
    payload["header"][field] = "bad"

    with pytest.raises(FeishuVerificationError):
        feishu.verify_long_connection_event(payload)


def test_verify_long_connection_event_rejects_allowlist_mismatch():
    feishu = client()
    payload = event_payload(sender={"sender_id": {"open_id": "ou_bad"}}, message={"chat_id": "oc_bad"})

    with pytest.raises(FeishuVerificationError):
        feishu.verify_long_connection_event(payload)


def test_get_tenant_access_token_caches_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    assert asyncio.run(feishu.get_tenant_access_token()) == "tenant-token"
    assert asyncio.run(feishu.get_tenant_access_token()) == "tenant-token"

    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"app_id": "app-1", "app_secret": "secret"}


def test_send_text_posts_with_cached_token_without_exposing_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    asyncio.run(feishu.send_text("oc_1", "hello"))

    message_request = requests[1]
    assert message_request.headers["Authorization"] == "Bearer tenant-token"
    assert b"tenant-token" not in message_request.content
    assert json.loads(message_request.content)["receive_id"] == "oc_1"
    assert dict(message_request.url.params)["receive_id_type"] == "chat_id"


def test_send_text_can_use_open_id_receive_type():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    asyncio.run(feishu.send_text("ou_1", "hello", receive_id_type="open_id"))

    assert dict(requests[1].url.params)["receive_id_type"] == "open_id"


def test_event_to_dict_supports_sdk_raw_payload():
    class Event:
        raw = json.dumps({"event": {"message": {"chat_id": "oc_1"}}})

    assert _event_to_dict(Event())["event"]["message"]["chat_id"] == "oc_1"


def test_event_to_dict_supports_sdk_to_dict_payload():
    class Event:
        def to_dict(self):
            return {"event": {"message": {"chat_id": "oc_1"}}}

    assert _event_to_dict(Event())["event"]["message"]["chat_id"] == "oc_1"


def test_handle_callback_failure_logs_exception(caplog):
    async def failing_handler(payload):
        raise RuntimeError("gateway failed")

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run_callback_failure(failing_handler))

    assert "feishu event handler failed" in caplog.text
    assert "gateway failed" in caplog.text


async def _run_callback_failure(handler):
    from app.infrastructure.feishu.client import _submit_event_handler

    _submit_event_handler(handler, {"event": {}}, asyncio.get_running_loop())
    await asyncio.sleep(0)


def test_event_to_dict_supports_sdk_attribute_payload():
    class SenderId:
        open_id = "ou_1"

    class Sender:
        sender_id = SenderId()

    class Message:
        message_id = "msg-1"
        chat_id = "oc_1"
        chat_type = "p2p"
        message_type = "text"
        content = json.dumps({"text": "hello"})

    class EventBody:
        sender = Sender()
        message = Message()

    class Header:
        event_id = "event-1"
        app_id = "app-1"
        tenant_key = "tenant-1"

    class Event:
        header = Header()
        event = EventBody()

    payload = _event_to_dict(Event())

    assert payload["header"]["event_id"] == "event-1"
    assert payload["event"]["sender"]["sender_id"]["open_id"] == "ou_1"
    assert payload["event"]["message"]["chat_id"] == "oc_1"
