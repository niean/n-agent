from app.application.events import ChatEvent, ChatEventType


def test_chat_event_metadata_defaults_to_empty_dict():
    event = ChatEvent(ChatEventType.MESSAGE_DONE)
    assert event.metadata == {}


def test_chat_event_metadata_can_be_set():
    event = ChatEvent(ChatEventType.MESSAGE_DONE, metadata={"confirmation_id": "abc"})
    assert event.metadata == {"confirmation_id": "abc"}
