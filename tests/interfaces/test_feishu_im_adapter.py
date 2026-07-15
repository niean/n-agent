import asyncio
import json

import pytest

from app.domain.gateway import GatewayOutboundMessage, InteractionMessage, InteractionResponse
from app.domain.tool import ApprovalRequest, RiskLevel
from app.interfaces.feishu_im_adapter import FeishuImAdapter


class FakeFeishuClient:
    def __init__(self, payload=None, image_bytes: bytes | None = None, image_mime: str = "image/png", download_error: Exception | None = None):
        self.payload = payload
        self.sent: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict, str]] = []
        self.updates: list[tuple[str, dict]] = []
        self.reactions: list[tuple[str, str]] = []
        self.events = []
        self.image_bytes = image_bytes
        self.image_mime = image_mime
        self.download_error = download_error
        self.download_calls: list[tuple[str, str]] = []
        self.markdown_replies: list[tuple[str, str, str]] = []

    def verify_long_connection_event(self, payload):
        self.events.append(payload)
        return self.payload or payload

    def verify_card_action_event(self, payload):
        self.events.append(payload)
        return self.payload or payload

    async def send_text(self, receive_id, text, receive_id_type="chat_id"):
        self.sent.append((receive_id, text, receive_id_type))

    async def send_markdown_reply(self, receive_id, content, receive_id_type="chat_id"):
        self.markdown_replies.append((receive_id, content, receive_id_type))
        await self.send_text(receive_id, content, receive_id_type)

    async def send_interactive_card(self, receive_id, card, receive_id_type="chat_id"):
        self.cards.append((receive_id, card, receive_id_type))
        return f"card-msg-{len(self.cards)}"

    async def update_card(self, message_id, card):
        self.updates.append((message_id, card))

    async def add_reaction(self, message_id, emoji_type="Typing"):
        self.reactions.append((message_id, emoji_type))

    async def listen_events(self, handler):
        await handler(self.payload)

    async def download_image(self, message_id, image_key):
        self.download_calls.append((message_id, image_key))
        if self.download_error is not None:
            raise self.download_error
        return self.image_bytes or b"\x89PNG\r\n\x1a\n", self.image_mime


class FakeGatewayService:
    def __init__(self, duplicate=False):
        self.events: list[InteractionMessage] = []
        self.confirmations = []
        self.discarded = []
        self.duplicate = duplicate
        self.response: InteractionResponse | None = None
        self.approval_deciders = []
        self.slash_confirmations = {"confirm-1"}
        self.tool_grants = []

    async def handle_message(self, event, *, approval_decider=None):
        self.events.append(event)
        self.approval_deciders.append(approval_decider)
        if self.duplicate:
            return InteractionResponse(session_id="", messages=[], metadata={"duplicate": True})
        return self.response or InteractionResponse(session_id="s1", messages=[GatewayOutboundMessage("reply")])

    async def handle_confirmation(
        self,
        session_key,
        actor_id,
        confirmation_id,
        choice,
        *,
        on_consumed=None,
    ):
        self.confirmations.append((session_key, actor_id, confirmation_id, choice))
        self.slash_confirmations.discard(confirmation_id)
        if on_consumed is not None:
            await on_consumed()
        return InteractionResponse(session_id="s1", messages=[GatewayOutboundMessage("confirmed")])

    def discard_confirmation(self, confirmation_id):
        self.discarded.append(confirmation_id)
        self.slash_confirmations.discard(confirmation_id)

    def owns_confirmation(self, confirmation_id):
        return confirmation_id in self.slash_confirmations

    def grant_tool_for_session(self, session_id, actor_id, tool_name):
        self.tool_grants.append((session_id, actor_id, tool_name))

    def is_tool_granted(self, session_id, actor_id, tool_name):
        return (session_id, actor_id, tool_name) in self.tool_grants


def text_payload(text="hello", chat_type="p2p"):
    return {
        "schema": "2.0",
        "header": {"event_id": "event-1", "app_id": "app-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user"},
            "message": {
                "message_id": "msg-1",
                "chat_id": "oc_1",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def card_payload(
    choice="once",
    *,
    confirmation_id="confirm-1",
    confirmation_kind=None,
    operator_id="ou_1",
    chat_id="oc_1",
    thread_id="",
):
    value = {
        "confirmation_id": confirmation_id,
        "choice": choice,
        "platform_session_id": chat_id,
        "thread_id": thread_id,
    }
    if confirmation_kind is not None:
        value["confirmation_kind"] = confirmation_kind
    return {
        "schema": "2.0",
        "header": {"event_id": "card-event-1", "app_id": "app-1"},
        "event": {
            "operator": {"open_id": operator_id},
            "context": {"open_chat_id": chat_id, "open_message_id": "card-msg-1"},
            "action": {"value": value},
        },
    }


def approval_request() -> ApprovalRequest:
    return ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="mcp_site_probe",
        arguments={"url": "https://example.com", "api_key": "secret-value"},
        description="Probe an MCP site",
        risk_level=RiskLevel.CONFIRM,
    )


async def test_long_connection_start_listens_and_handles_event():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.start()

    assert gateway.events[0].text == "hello"
    assert client.sent == [("oc_1", "reply", "chat_id")]
    assert adapter.is_connected() is False
    assert adapter.fatal_error() is None


async def test_long_connection_start_marks_fatal_on_listen_error():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))

    async def fail_listen(handler):
        raise RuntimeError("boom")

    client.listen_events = fail_listen
    adapter = FeishuImAdapter(gateway, client)

    with pytest.raises(RuntimeError, match="boom"):
        await adapter.start()

    assert adapter.is_connected() is False
    assert adapter.fatal_error() == ("feishu_listen_error", "boom")


async def test_long_connection_start_marks_connected_while_listening():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def listen(handler):
        entered.set()
        await release.wait()

    client.listen_events = listen
    adapter = FeishuImAdapter(gateway, client)
    task = asyncio.create_task(adapter.start())
    await entered.wait()

    assert adapter.is_connected() is True
    assert adapter.fatal_error() is None

    release.set()
    await task
    assert adapter.is_connected() is False
    assert adapter.fatal_error() is None


async def test_long_connection_unsupported_message_type_returns_unsupported_without_gateway_call():
    payload = text_payload()
    payload["event"]["message"]["message_type"] = "file"
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event(payload)

    assert gateway.events == []
    assert client.sent == [("oc_1", "不支持该消息类型", "chat_id")]


def image_payload(chat_type="p2p", image_key="img_key_1"):
    return {
        "schema": "2.0",
        "header": {"event_id": "event-img-1", "app_id": "app-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}, "sender_type": "user"},
            "message": {
                "message_id": "msg-img-1",
                "chat_id": "oc_1",
                "chat_type": chat_type,
                "message_type": "image",
                "content": json.dumps({"image_key": image_key}),
            },
        },
    }


async def test_long_connection_p2p_image_message_calls_gateway_with_data_url():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(image_payload(), image_bytes=b"\x89PNG\r\n\x1a\n")
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert client.download_calls == [("msg-img-1", "img_key_1")]
    assert gateway.events
    event = gateway.events[0]
    assert event.text == ""
    assert len(event.images) == 1
    assert event.images[0].startswith("data:image/png;base64,")
    assert callable(gateway.approval_deciders[0])


async def test_long_connection_image_download_failure_replies_retry_without_gateway_call():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(
        image_payload(),
        download_error=RuntimeError("feishu 500"),
    )
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events == []
    assert client.sent == [("oc_1", "图片下载失败，请重试", "chat_id")]


async def test_long_connection_image_invalid_format_replies_invalid_without_gateway_call():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(
        image_payload(),
        download_error=ValueError("non-image content type"),
    )
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events == []
    assert client.sent == [("oc_1", "图片格式无效或过大", "chat_id")]


async def test_long_connection_group_image_message_is_ignored():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(image_payload(chat_type="group"), image_bytes=b"\x89PNG\r\n\x1a\n")
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events == []
    assert client.download_calls == []
    assert client.sent == []


async def test_long_connection_group_message_without_mention_is_ignored():
    payload = text_payload(chat_type="group")
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event(payload)

    assert gateway.events == []
    assert client.reactions == []
    assert client.sent == []


async def test_long_connection_text_message_calls_gateway_and_replies():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("<at user_id=\"bot\">bot</at> hello", chat_type="group"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].text.endswith("hello")
    assert gateway.events[0].metadata["message_id"] == "msg-1"
    assert gateway.events[0].metadata["platform"] == "feishu"
    assert gateway.events[0].metadata["receive_id"] == "oc_1"
    assert gateway.events[0].metadata["receive_id_type"] == "chat_id"
    assert "capabilities" not in gateway.events[0].metadata
    assert client.reactions == [("msg-1", "Typing")]
    assert client.sent == [("oc_1", "reply", "chat_id")]
    assert callable(gateway.approval_deciders[0])


async def test_long_connection_p2p_without_chat_id_replies_to_open_id():
    payload = text_payload("hello")
    payload["event"]["message"]["chat_id"] = ""
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].session_key.platform_session_id == "ou_1"
    assert gateway.events[0].metadata["receive_id"] == "ou_1"
    assert gateway.events[0].metadata["receive_id_type"] == "open_id"
    assert client.sent == [("ou_1", "reply", "open_id")]


async def test_long_connection_reaction_failure_does_not_block_reply():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))

    async def fail_reaction(*args, **kwargs):
        raise RuntimeError("reaction failed")

    client.add_reaction = fail_reaction
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].text == "hello"
    assert client.sent == [("oc_1", "reply", "chat_id")]


async def test_long_connection_normalizes_null_thread_id():
    payload = text_payload("hello")
    payload["event"]["message"]["thread_id"] = None
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].session_key.thread_id == ""
    assert gateway.events[0].metadata["thread_id"] == ""


async def test_long_connection_duplicate_gateway_response_does_not_send_reply():
    gateway = FakeGatewayService(duplicate=True)
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert len(gateway.events) == 1
    assert client.sent == []


async def test_long_connection_text_message_sets_actor_id_metadata():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].metadata["actor_id"] == "ou_1"


async def test_long_connection_sends_confirmation_as_interactive_card():
    gateway = FakeGatewayService()
    gateway.response = InteractionResponse(
        session_id="s1",
        messages=[GatewayOutboundMessage("需要确认", metadata={"confirmation": {"id": "confirm-1", "action": "new", "command": "/new"}})],
    )
    client = FakeFeishuClient(text_payload("/new"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert client.cards[0][0] == "oc_1"
    assert client.sent == []


async def test_long_connection_card_send_failure_discards_pending_and_sends_retry_text():
    gateway = FakeGatewayService()
    gateway.response = InteractionResponse(
        session_id="s1",
        messages=[GatewayOutboundMessage("需要确认", metadata={"confirmation": {"id": "confirm-1", "action": "new", "command": "/new"}})],
    )
    client = FakeFeishuClient(text_payload("/new"))

    async def fail_card(*args, **kwargs):
        raise RuntimeError("card failed")

    client.send_interactive_card = fail_card
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert gateway.discarded == ["confirm-1"]
    assert "稍后重试" in client.sent[0][1]
    assert adapter._last_confirmations == {}


async def test_long_connection_card_action_routes_confirmation():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(card_payload())
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event(card_payload())

    assert gateway.confirmations[0][1] == "ou_1"
    assert gateway.confirmations[0][2] == "confirm-1"
    assert client.sent == [("oc_1", "confirmed", "chat_id")]
    assert adapter._last_confirmations == {}


@pytest.mark.parametrize("choice", ["once", "trust_session", "cancel"])
async def test_slash_confirmation_any_button_disables_all_buttons(choice):
    gateway = FakeGatewayService()
    client = FakeFeishuClient(card_payload(choice=choice))
    adapter = FeishuImAdapter(gateway, client)
    adapter._last_confirmations["confirm-1"] = {
        "id": "confirm-1",
        "command": "/new",
    }

    await adapter.handle_event(card_payload(choice=choice))

    assert len(client.updates) == 1
    assert all(
        action["disabled"] is True
        for action in client.updates[0][1]["elements"][-1]["actions"]
    )
    assert f"已点击「{ {'once': '执行一次', 'trust_session': '本会话信任', 'cancel': '取消'}[choice] }」" in json.dumps(
        client.updates[0][1], ensure_ascii=False
    )


async def test_slash_confirmation_rejects_invalid_choice_without_consuming_pending():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(card_payload(choice="invalid"))
    adapter = FeishuImAdapter(gateway, client)
    adapter._last_confirmations["confirm-1"] = {
        "id": "confirm-1",
        "command": "/new",
    }

    await adapter.handle_event(card_payload(choice="invalid"))

    assert gateway.confirmations == []
    assert gateway.owns_confirmation("confirm-1") is True
    assert client.updates == []
    assert "确认选项无效" in client.sent[-1][1]


async def test_slash_confirmation_cache_removes_expired_entries_on_next_event():
    gateway = FakeGatewayService()
    payload = text_payload()
    payload["event"]["message"]["message_type"] = "file"
    client = FakeFeishuClient(payload)
    adapter = FeishuImAdapter(gateway, client)
    adapter._last_confirmations["expired"] = {
        "id": "expired",
        "command": "/new",
        "expires_at": "2000-01-01T00:00:00+00:00",
    }

    await adapter.handle_event(payload)

    assert "expired" not in adapter._last_confirmations


async def test_tool_approval_card_action_allows_once_without_gateway_confirmation():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    await adapter.handle_event({})
    client.sent.clear()

    decision_task = asyncio.create_task(gateway.approval_deciders[0](approval_request()))
    while not client.cards:
        await asyncio.sleep(0)
    card = client.cards[0][1]
    rendered = json.dumps(card, ensure_ascii=False)
    confirmation_id = card["elements"][-1]["actions"][0]["value"]["confirmation_id"]
    assert "mcp_site_probe" in rendered
    assert "secret-value" not in rendered
    assert [
        action["text"]["content"] for action in card["elements"][-1]["actions"]
    ] == ["执行一次", "本会话信任", "取消"]

    client.payload = None
    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="tool_policy",
        )
    )

    decision = await decision_task
    assert decision.allowed is True
    assert decision.scope == "once"
    assert gateway.confirmations == []
    assert client.updates
    assert all(
        action["disabled"] is True
        for action in client.updates[0][1]["elements"][-1]["actions"]
    )
    assert client.sent == []


async def test_tool_approval_unauthorized_actor_does_not_disable_or_consume_card():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    await adapter.handle_event({})
    client.sent.clear()
    decision_task = asyncio.create_task(gateway.approval_deciders[0](approval_request()))
    while not client.cards:
        await asyncio.sleep(0)
    confirmation_id = client.cards[0][1]["elements"][-1]["actions"][0]["value"]["confirmation_id"]

    client.payload = None
    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="tool_policy",
            operator_id="ou_2",
        )
    )

    assert decision_task.done() is False
    assert client.updates == []
    assert "只有发起者" in client.sent[-1][1]

    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="tool_policy",
        )
    )
    assert (await decision_task).allowed is True


async def test_tool_approval_card_send_failure_denies_and_cleans_cache():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    await adapter.handle_event({})
    client.sent.clear()

    async def fail_card(*args, **kwargs):
        raise RuntimeError("card failed")

    client.send_interactive_card = fail_card
    decision = await gateway.approval_deciders[0](approval_request())

    assert decision.allowed is False
    assert decision.reason == "card_send_failed"
    assert "确认卡片发送失败" in client.sent[-1][1]
    assert adapter._last_confirmations == {}


async def test_tool_approval_rejects_tampered_confirmation_kind_without_disabling():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    await adapter.handle_event({})
    client.sent.clear()
    decision_task = asyncio.create_task(gateway.approval_deciders[0](approval_request()))
    while not client.cards:
        await asyncio.sleep(0)
    confirmation_id = client.cards[0][1]["elements"][-1]["actions"][0]["value"]["confirmation_id"]
    client.payload = None

    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="slash_command",
        )
    )

    assert decision_task.done() is False
    assert client.updates == []
    assert "确认类型无效" in client.sent[-1][1]
    decision_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await decision_task


async def test_tool_approval_update_failure_still_completes_decision_and_cleans_cache():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    await adapter.handle_event({})
    decision_task = asyncio.create_task(gateway.approval_deciders[0](approval_request()))
    while not client.cards:
        await asyncio.sleep(0)
    confirmation_id = client.cards[0][1]["elements"][-1]["actions"][0]["value"]["confirmation_id"]
    client.payload = None

    async def fail_update(*args, **kwargs):
        raise RuntimeError("update failed")

    client.update_card = fail_update
    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="tool_policy",
        )
    )

    assert (await decision_task).allowed is True
    assert adapter._last_confirmations == {}


async def test_tool_approval_resumes_original_message_and_sends_final_reply():
    class WaitingGatewayService(FakeGatewayService):
        async def handle_message(self, event, *, approval_decider=None):
            self.events.append(event)
            self.approval_deciders.append(approval_decider)
            decision = await approval_decider(approval_request())
            return InteractionResponse(
                session_id="s1",
                messages=[GatewayOutboundMessage(f"tool allowed: {decision.allowed}")],
            )

    gateway = WaitingGatewayService()
    client = FakeFeishuClient(text_payload("probe mcp"))
    adapter = FeishuImAdapter(gateway, client)
    message_task = asyncio.create_task(adapter.handle_event({}))
    while not client.cards:
        await asyncio.sleep(0)
    confirmation_id = client.cards[0][1]["elements"][-1]["actions"][0]["value"]["confirmation_id"]
    client.payload = None

    await adapter.handle_event(
        card_payload(
            confirmation_id=confirmation_id,
            confirmation_kind="tool_policy",
        )
    )
    await message_task

    assert client.sent == [("oc_1", "tool allowed: True", "chat_id")]


async def test_send_response_routes_normal_reply_through_markdown_reply():
    markdown = "拍照上传成功！\n\n![照片](https://oss.example.com/a.jpg)\n\n[点击查看原图](https://oss.example.com/a.jpg)"
    gateway = FakeGatewayService()
    gateway.response = InteractionResponse(
        session_id="s1",
        messages=[GatewayOutboundMessage(markdown)],
    )
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuImAdapter(gateway, client)

    await adapter.handle_event({})

    assert client.markdown_replies == [("oc_1", markdown, "chat_id")]
    assert client.sent[0][1] == markdown
