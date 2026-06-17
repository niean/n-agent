import asyncio
import json

import pytest

from app.domain.gateway import GatewayOutboundMessage, InteractionMessage, InteractionResponse
from app.interfaces.feishu_long_connection import FeishuLongConnectionGateway


class FakeFeishuClient:
    def __init__(self, payload=None):
        self.payload = payload
        self.sent: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self.events = []

    def verify_long_connection_event(self, payload):
        self.events.append(payload)
        return self.payload or payload

    def verify_card_action_event(self, payload):
        self.events.append(payload)
        return self.payload or payload

    async def send_text(self, receive_id, text, receive_id_type="chat_id"):
        self.sent.append((receive_id, text, receive_id_type))

    async def send_interactive_card(self, receive_id, card, receive_id_type="chat_id"):
        self.cards.append((receive_id, card, receive_id_type))

    async def add_reaction(self, message_id, emoji_type="Typing"):
        self.reactions.append((message_id, emoji_type))

    async def listen_events(self, handler):
        await handler(self.payload)


class FakeGatewayService:
    def __init__(self, duplicate=False):
        self.events: list[InteractionMessage] = []
        self.confirmations = []
        self.discarded = []
        self.duplicate = duplicate
        self.response: InteractionResponse | None = None

    async def handle_message(self, event):
        self.events.append(event)
        if self.duplicate:
            return InteractionResponse(session_id="", messages=[], metadata={"duplicate": True})
        return self.response or InteractionResponse(session_id="s1", messages=[GatewayOutboundMessage("reply")])

    async def handle_confirmation(self, session_key, actor_id, confirmation_id, choice):
        self.confirmations.append((session_key, actor_id, confirmation_id, choice))
        return InteractionResponse(session_id="s1", messages=[GatewayOutboundMessage("confirmed")])

    def discard_confirmation(self, confirmation_id):
        self.discarded.append(confirmation_id)


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


def card_payload(choice="once"):
    return {
        "schema": "2.0",
        "header": {"event_id": "card-event-1", "app_id": "app-1"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "context": {"open_chat_id": "oc_1"},
            "action": {"value": {"confirmation_id": "confirm-1", "choice": choice, "platform_session_id": "oc_1", "thread_id": ""}},
        },
    }


async def test_long_connection_start_listens_and_handles_event():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuLongConnectionGateway(gateway, client)

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
    adapter = FeishuLongConnectionGateway(gateway, client)

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
    adapter = FeishuLongConnectionGateway(gateway, client)
    task = asyncio.create_task(adapter.start())
    await entered.wait()

    assert adapter.is_connected() is True
    assert adapter.fatal_error() is None

    release.set()
    await task
    assert adapter.is_connected() is False
    assert adapter.fatal_error() is None


async def test_long_connection_non_text_message_returns_unsupported_without_gateway_call():
    payload = text_payload()
    payload["event"]["message"]["message_type"] = "image"
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event(payload)

    assert gateway.events == []
    assert client.sent == [("oc_1", "不支持该消息类型", "chat_id")]


async def test_long_connection_group_message_without_mention_is_ignored():
    payload = text_payload(chat_type="group")
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event(payload)

    assert gateway.events == []
    assert client.reactions == []
    assert client.sent == []


async def test_long_connection_text_message_calls_gateway_and_replies():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("<at user_id=\"bot\">bot</at> hello", chat_type="group"))
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].text.endswith("hello")
    assert gateway.events[0].metadata["message_id"] == "msg-1"
    assert gateway.events[0].metadata["platform"] == "feishu"
    assert gateway.events[0].metadata["receive_id"] == "oc_1"
    assert gateway.events[0].metadata["receive_id_type"] == "chat_id"
    assert "capabilities" not in gateway.events[0].metadata
    assert client.reactions == [("msg-1", "Typing")]
    assert client.sent == [("oc_1", "reply", "chat_id")]


async def test_long_connection_p2p_without_chat_id_replies_to_open_id():
    payload = text_payload("hello")
    payload["event"]["message"]["chat_id"] = ""
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuLongConnectionGateway(gateway, client)

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
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].text == "hello"
    assert client.sent == [("oc_1", "reply", "chat_id")]


async def test_long_connection_normalizes_null_thread_id():
    payload = text_payload("hello")
    payload["event"]["message"]["thread_id"] = None
    gateway = FakeGatewayService()
    client = FakeFeishuClient(payload)
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].session_key.thread_id == ""
    assert gateway.events[0].metadata["thread_id"] == ""


async def test_long_connection_duplicate_gateway_response_does_not_send_reply():
    gateway = FakeGatewayService(duplicate=True)
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert len(gateway.events) == 1
    assert client.sent == []


async def test_long_connection_text_message_sets_actor_id_metadata():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(text_payload("hello"))
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert gateway.events[0].metadata["actor_id"] == "ou_1"


async def test_long_connection_sends_confirmation_as_interactive_card():
    gateway = FakeGatewayService()
    gateway.response = InteractionResponse(
        session_id="s1",
        messages=[GatewayOutboundMessage("需要确认", metadata={"confirmation": {"id": "confirm-1", "action": "new", "command": "/new"}})],
    )
    client = FakeFeishuClient(text_payload("/new"))
    adapter = FeishuLongConnectionGateway(gateway, client)

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
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event({})

    assert gateway.discarded == ["confirm-1"]
    assert "稍后重试" in client.sent[0][1]


async def test_long_connection_card_action_routes_confirmation():
    gateway = FakeGatewayService()
    client = FakeFeishuClient(card_payload())
    adapter = FeishuLongConnectionGateway(gateway, client)

    await adapter.handle_event(card_payload())

    assert gateway.confirmations[0][1] == "ou_1"
    assert gateway.confirmations[0][2] == "confirm-1"
    assert client.sent == [("oc_1", "confirmed", "chat_id")]
