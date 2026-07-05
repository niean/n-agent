import pytest
from unittest.mock import AsyncMock, MagicMock
from app.application.chat_service import ChatCompletionService


class FakeReader:
    def __init__(self, names): self._names = names
    def get_active_provider_names(self): return self._names


@pytest.fixture
def chat_service():
    memory_store = AsyncMock()
    memory_store.lock_session_external_memory = AsyncMock(side_effect=lambda sid, mem, slots=None: mem)
    memory_store.get_session = AsyncMock(return_value=None)
    memory_store.list_messages = AsyncMock(return_value=[])
    memory_store.append_message = AsyncMock()
    graph_runner = MagicMock()
    session_service = AsyncMock()
    session_service.ensure_title = AsyncMock()
    return ChatCompletionService(
        memory_store=memory_store, graph_runner=graph_runner,
        session_service=session_service, external_memory_reader=FakeReader(["mem0"]),
    )


def test_no_override_defaults_empty_profile(chat_service):
    # 未传 external_memory_enabled 字段，即使有 active provider 也不纳入
    from app.application.chat_service import ChatCompletionInput
    request = ChatCompletionInput(
        model="m", messages=[{"role": "user", "content": "hi"}],
        metadata={}, options={},
    )
    import asyncio
    asyncio.run(chat_service.complete(request))
    args = chat_service.memory_store.lock_session_external_memory.call_args
    assert args.args[1] == []


def test_explicit_builtin_stays_builtin(chat_service):
    from app.application.chat_service import ChatCompletionInput
    request = ChatCompletionInput(
        model="m", messages=[{"role": "user", "content": "hi"}],
        metadata={}, options={"external_memory_enabled": ["builtin"]},
    )
    import asyncio
    asyncio.run(chat_service.complete(request))
    args = chat_service.memory_store.lock_session_external_memory.call_args
    assert args.args[1] == ["builtin"]


def test_explicit_mem0_stays_mem0(chat_service):
    from app.application.chat_service import ChatCompletionInput
    request = ChatCompletionInput(
        model="m", messages=[{"role": "user", "content": "hi"}],
        metadata={}, options={"external_memory_enabled": ["builtin", "mem0"]},
    )
    import asyncio
    asyncio.run(chat_service.complete(request))
    args = chat_service.memory_store.lock_session_external_memory.call_args
    assert args.args[1] == ["builtin", "mem0"]


def test_explicit_project_and_external_query_provider_are_both_kept():
    memory_store = AsyncMock()
    memory_store.lock_session_external_memory = AsyncMock(side_effect=lambda sid, mem, slots=None: mem)
    memory_store.get_session = AsyncMock(return_value=None)
    memory_store.list_messages = AsyncMock(return_value=[])
    memory_store.append_message = AsyncMock()
    graph_runner = MagicMock()
    session_service = AsyncMock()
    session_service.ensure_title = AsyncMock()
    svc = ChatCompletionService(
        memory_store=memory_store,
        graph_runner=graph_runner,
        session_service=session_service,
        external_memory_reader=FakeReader(["holographic"]),
    )
    from app.application.chat_service import ChatCompletionInput
    request = ChatCompletionInput(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        metadata={},
        options={"external_memory_enabled": ["builtin", "project_memory_1", "holographic"]},
    )
    import asyncio
    asyncio.run(svc.complete(request))
    args = memory_store.lock_session_external_memory.call_args
    assert args.args[1] == ["builtin", "project_memory_1", "holographic"]


def test_no_active_provider_defaults_empty_profile():
    memory_store = AsyncMock()
    memory_store.lock_session_external_memory = AsyncMock(side_effect=lambda sid, mem, slots=None: mem)
    memory_store.get_session = AsyncMock(return_value=None)
    memory_store.list_messages = AsyncMock(return_value=[])
    memory_store.append_message = AsyncMock()
    graph_runner = MagicMock()
    session_service = AsyncMock()
    session_service.ensure_title = AsyncMock()
    svc = ChatCompletionService(
        memory_store=memory_store, graph_runner=graph_runner,
        session_service=session_service, external_memory_reader=FakeReader([]),
    )
    from app.application.chat_service import ChatCompletionInput
    request = ChatCompletionInput(
        model="m", messages=[{"role": "user", "content": "hi"}],
        metadata={}, options={},
    )
    import asyncio
    asyncio.run(svc.complete(request))
    args = memory_store.lock_session_external_memory.call_args
    assert args.args[1] == []
