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


def card_action_payload(open_id="ou_1", chat_id="oc_1"):
    return event_payload(
        operator={"open_id": open_id},
        context={"open_chat_id": chat_id},
        action={"value": {"confirmation_id": "confirm-1", "choice": "once"}},
    )


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


def test_verify_card_action_event_accepts_operator_and_context_allowlist():
    verified = client().verify_card_action_event(card_action_payload())

    assert verified["event"]["operator"]["open_id"] == "ou_1"


def test_verify_card_action_event_rejects_operator_allowlist_mismatch():
    with pytest.raises(FeishuVerificationError):
        client().verify_card_action_event(card_action_payload(open_id="ou_bad"))


def test_verify_card_action_event_rejects_chat_allowlist_mismatch():
    with pytest.raises(FeishuVerificationError):
        client().verify_card_action_event(card_action_payload(chat_id="oc_bad"))


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


def test_download_image_returns_bytes_and_content_type():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    data, mime = asyncio.run(feishu.download_image("msg-1", "img_key_1"))

    assert data == b"\x89PNG\r\n\x1a\n"
    assert mime == "image/png"
    image_request = requests[-1]
    assert image_request.headers["Authorization"] == "Bearer tenant-token"
    assert "msg-1" in image_request.url.path
    assert "img_key_1" in image_request.url.path


def test_download_image_rejects_non_image_content_type():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, content=b"plain", headers={"Content-Type": "text/plain"})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    with pytest.raises(ValueError, match="non-image"):
        asyncio.run(feishu.download_image("msg-1", "img_key_1"))


def test_download_image_rejects_oversized_payload():
    huge = b"\x00" * (15 * 1024 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, content=huge, headers={"Content-Type": "image/png"})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    with pytest.raises(ValueError, match="too large"):
        asyncio.run(feishu.download_image("msg-1", "img_key_1"))


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


def test_send_interactive_card_posts_interactive_message():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "card-msg-1"}})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    message_id = asyncio.run(feishu.send_interactive_card("oc_1", {"type": "template", "data": {}}))

    body = json.loads(requests[1].content)
    assert body["msg_type"] == "interactive"
    assert json.loads(body["content"])["type"] == "template"
    assert message_id == "card-msg-1"


def test_add_reaction_posts_message_reaction():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0})

    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn")

    asyncio.run(feishu.add_reaction("msg-1"))

    reaction_request = requests[1]
    assert reaction_request.url.path == "/open-apis/im/v1/messages/msg-1/reactions"
    assert reaction_request.headers["Authorization"] == "Bearer tenant-token"
    assert json.loads(reaction_request.content) == {"reaction_type": {"emoji_type": "Typing"}}


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


def test_parse_markdown_segments_splits_text_image_link():
    from app.infrastructure.feishu.client import _parse_markdown_segments

    url = "https://oss.example.com/photo.jpg?token=x"
    text = f"拍照上传成功！\n\n![照片]({url})\n\n[点击查看原图]({url})"

    assert _parse_markdown_segments(text) == [
        ("text", "拍照上传成功！\n\n"),
        ("image", "照片", url),
        ("text", "\n\n"),
        ("link", "点击查看原图", url),
    ]


def test_parse_markdown_segments_keeps_link_inside_image_text_unduplicated():
    from app.infrastructure.feishu.client import _parse_markdown_segments

    # the [alt](url) portion of an image must not also surface as a link
    segments = _parse_markdown_segments("![照片](https://oss.example.com/a.jpg)")
    assert segments == [("image", "照片", "https://oss.example.com/a.jpg")]


def test_build_post_content_emits_text_image_link_rows():
    from app.infrastructure.feishu.client import _build_post_content

    url = "https://oss.example.com/photo.jpg"
    segments = [
        ("text", "拍照上传成功！\n\n"),
        ("image", "照片", url),
        ("text", "\n\n"),
        ("link", "点击查看原图", url),
    ]
    rows = _build_post_content(segments, {url: "img_v3_1"})["zh_cn"]["content"]
    assert rows == [
        [{"tag": "text", "text": "拍照上传成功！"}],
        [{"tag": "text", "text": ""}],
        [{"tag": "img", "image_key": "img_v3_1"}],
        [{"tag": "text", "text": ""}],
        [{"tag": "a", "text": "点击查看原图", "href": url}],
    ]


def test_build_post_content_marks_failed_image_as_placeholder():
    from app.infrastructure.feishu.client import _build_post_content

    rows = _build_post_content(
        [("image", "照片", "https://oss.example.com/a.jpg")],
        {"https://oss.example.com/a.jpg": None},
    )["zh_cn"]["content"]
    assert rows == [[{"tag": "text", "text": "[图片加载失败]"}]]


def _mock_client(handler):
    feishu = client(allowed_open_ids=[], allowed_chat_ids=[])
    feishu.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    )
    return feishu


def _token_handler(extra=None):
    extra = extra or (lambda request: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        custom = extra(request)
        if custom is not None:
            return custom
        return httpx.Response(200, json={"code": 0})

    return handler


def test_download_url_returns_bytes_and_mime_for_absolute_url():
    def extra(request):
        if request.url.host == "oss.example.com":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"})
        return None

    feishu = _mock_client(_token_handler(extra))
    data, mime = asyncio.run(feishu.download_url("https://oss.example.com/photo.jpg?token=x"))
    assert data == b"\x89PNG\r\n\x1a\n"
    assert mime == "image/png"


def test_download_url_rejects_non_http_scheme():
    feishu = _mock_client(_token_handler())
    with pytest.raises(ValueError, match="unsupported url scheme"):
        asyncio.run(feishu.download_url("javascript:alert(1)"))


def test_upload_image_posts_multipart_and_returns_image_key():
    requests: list[httpx.Request] = []

    def extra(request):
        requests.append(request)
        if request.url.path.endswith("/im/v1/images"):
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v3_1"}})
        return None

    feishu = _mock_client(_token_handler(extra))
    key = asyncio.run(feishu.upload_image(b"\x89PNG\r\n\x1a\n", "image/png"))

    assert key == "img_v3_1"
    upload_request = requests[-1]
    assert upload_request.url.path.endswith("/im/v1/images")
    assert upload_request.headers["Authorization"] == "Bearer tenant-token"
    assert upload_request.headers["Content-Type"].startswith("multipart/form-data")
    body = upload_request.content.decode("utf-8", errors="ignore")
    assert "image_type" in body
    assert "message" in body
    assert b"tenant-token" not in upload_request.content


def test_upload_image_raises_on_feishu_error_code():
    def extra(request):
        if request.url.path.endswith("/im/v1/images"):
            # Feishu returns 200 with a non-zero code on scope/permission failures
            return httpx.Response(200, json={"code": 99991663, "msg": "permission denied"})
        return None

    feishu = _mock_client(_token_handler(extra))
    with pytest.raises(RuntimeError, match="permission denied"):
        asyncio.run(feishu.upload_image(b"\x89PNG\r\n\x1a\n", "image/png"))


def test_send_post_sends_post_message():
    requests: list[httpx.Request] = []

    def extra(request):
        requests.append(request)
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(200, json={"code": 0})
        return None

    feishu = _mock_client(_token_handler(extra))
    asyncio.run(feishu.send_post("oc_1", {"zh_cn": {"content": [[{"tag": "text", "text": "hi"}]]}}))

    body = json.loads(requests[-1].content)
    assert body["msg_type"] == "post"
    assert body["receive_id"] == "oc_1"
    assert dict(requests[-1].url.params)["receive_id_type"] == "chat_id"


def test_send_markdown_reply_without_media_sends_text():
    requests: list[httpx.Request] = []

    def extra(request):
        requests.append(request)
        return None

    feishu = _mock_client(_token_handler(extra))
    asyncio.run(feishu.send_markdown_reply("oc_1", "hello world"))

    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["msg_type"] == "text"
    assert json.loads(body["content"])["text"] == "hello world"


def test_send_markdown_reply_with_image_downloads_uploads_and_sends_post():
    requests: list[httpx.Request] = []
    oss_url = "https://oss.example.com/photo.jpg?token=x"

    def extra(request):
        requests.append(request)
        if request.url.host == "oss.example.com":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n", headers={"Content-Type": "image/png"})
        if request.url.path.endswith("/im/v1/images"):
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_v3_1"}})
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(200, json={"code": 0})
        return None

    feishu = _mock_client(_token_handler(extra))
    asyncio.run(
        feishu.send_markdown_reply(
            "oc_1", f"拍照上传成功！\n\n![照片]({oss_url})\n\n[点击查看原图]({oss_url})"
        )
    )

    upload_requests = [r for r in requests if r.url.path.endswith("/im/v1/images")]
    assert len(upload_requests) == 1
    message_requests = [r for r in requests if r.url.path.endswith("/im/v1/messages")]
    assert len(message_requests) == 1
    body = json.loads(message_requests[0].content)
    assert body["msg_type"] == "post"
    rows = json.loads(body["content"])["zh_cn"]["content"]
    assert [{"tag": "img", "image_key": "img_v3_1"}] in rows
    assert [{"tag": "a", "text": "点击查看原图", "href": oss_url}] in rows
    # raw url text must not appear as a standalone text row (only the friendly link)
    assert not any(
        row == [{"tag": "text", "text": oss_url}] for row in rows
    )


def test_send_markdown_reply_with_link_only_sends_post():
    requests: list[httpx.Request] = []

    def extra(request):
        requests.append(request)
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(200, json={"code": 0})
        return None

    feishu = _mock_client(_token_handler(extra))
    asyncio.run(feishu.send_markdown_reply("oc_1", "详情见 [文档](https://example.com/doc)"))

    body = json.loads(requests[-1].content)
    assert body["msg_type"] == "post"
    rows = json.loads(body["content"])["zh_cn"]["content"]
    assert [{"tag": "a", "text": "文档", "href": "https://example.com/doc"}] in rows


def test_send_markdown_reply_image_fetch_failure_sends_post_with_placeholder():
    requests: list[httpx.Request] = []
    oss_url = "https://oss.example.com/photo.jpg"

    def extra(request):
        requests.append(request)
        if request.url.host == "oss.example.com":
            return httpx.Response(500)
        if request.url.path.endswith("/im/v1/messages"):
            return httpx.Response(200, json={"code": 0})
        return None

    feishu = _mock_client(_token_handler(extra))
    asyncio.run(feishu.send_markdown_reply("oc_1", f"![照片]({oss_url})"))

    # no upload attempted because download failed first
    assert not [r for r in requests if r.url.path.endswith("/im/v1/images")]
    body = json.loads([r for r in requests if r.url.path.endswith("/im/v1/messages")][-1].content)
    assert body["msg_type"] == "post"
    rows = json.loads(body["content"])["zh_cn"]["content"]
    assert [{"tag": "text", "text": "[图片加载失败]"}] in rows


def test_send_markdown_reply_post_failure_falls_back_to_text():
    requests: list[httpx.Request] = []

    def extra(request):
        requests.append(request)
        if request.url.path.endswith("/im/v1/messages"):
            body = json.loads(request.content)
            if body["msg_type"] == "post":
                return httpx.Response(500)
            return httpx.Response(200, json={"code": 0})
        return None

    feishu = _mock_client(_token_handler(extra))
    content = "[点击查看](https://example.com)"
    asyncio.run(feishu.send_markdown_reply("oc_1", content))

    message_requests = [r for r in requests if r.url.path.endswith("/im/v1/messages")]
    types = [json.loads(r.content)["msg_type"] for r in message_requests]
    assert types == ["post", "text"]
    text_body = json.loads(message_requests[-1].content)
    assert json.loads(text_body["content"])["text"] == content
