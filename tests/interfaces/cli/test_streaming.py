from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console

from app.application.events import ChatEvent, ChatEventType
from app.interfaces.cli.streaming import consume_stream


@pytest.mark.asyncio
async def test_consume_stream_renders_content_delta(fake_console):
    async def gen():
        yield ChatEvent(ChatEventType.MESSAGE_START)
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="hello ")
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="world")
        yield ChatEvent(ChatEventType.MESSAGE_DONE)
        yield ChatEvent(ChatEventType.DONE)

    await consume_stream(gen(), fake_console)


@pytest.mark.asyncio
async def test_consume_stream_terminates_streamed_content_line():
    output = StringIO()
    console = Console(file=output, force_terminal=False, no_color=True)

    async def gen():
        yield ChatEvent(ChatEventType.MESSAGE_START)
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="hello")
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
        yield ChatEvent(ChatEventType.DONE)

    rc = await consume_stream(gen(), console)

    assert rc == 0
    assert output.getvalue() == "hello\n"


@pytest.mark.asyncio
async def test_consume_stream_done_after_message_done_no_duplicate(fake_console):
    async def gen():
        yield ChatEvent(ChatEventType.MESSAGE_DONE, content="full content")
        yield ChatEvent(ChatEventType.DONE)

    await consume_stream(gen(), fake_console)


@pytest.mark.asyncio
async def test_consume_stream_error_returns_non_zero(fake_console):
    async def gen():
        yield ChatEvent(ChatEventType.ERROR, error="boom")
        yield ChatEvent(ChatEventType.DONE)

    rc = await consume_stream(gen(), fake_console)
    assert rc != 0


@pytest.mark.asyncio
async def test_consume_stream_cancellation(fake_console):
    started = asyncio.Event()

    async def gen():
        yield ChatEvent(ChatEventType.MESSAGE_START)
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(consume_stream(gen(), fake_console))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
