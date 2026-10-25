"""Provider content hooks receive transport inputs and raw output deltas."""
from types import SimpleNamespace as NS

import pytest

from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.llm.anthropic_provider import AnthropicProvider


class Attempt:
    def __init__(self):
        self.calls = []
        self.active = False
    def __enter__(self):
        self.active = True
        return self
    def __exit__(self, *args):
        self.active = False
    def __getattr__(self, name):
        def call(*args, **kwargs):
            assert self.active
            self.calls.append((name, args, kwargs))
        return call


class Client:
    def __init__(self, response):
        self.chat = NS(completions=self)
        self.messages = self
        self.response = response
        self.kwargs = None
    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


async def events(items):
    for item in items:
        if isinstance(item, Exception):
            raise item
        yield item


def provider(kind, client, hook):
    cls = OpenAICompatibleProvider if kind == 'openai' else AnthropicProvider
    return cls('', '', 'test-model', client=client, telemetry_callback=lambda **_: hook)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
async def test_nonstream_captures_final_transport_input_and_normalized_output(kind):
    hook = Attempt()
    response = (NS(choices=[NS(message=NS(role='assistant', content='answer'), finish_reason='stop')])
                if kind == 'openai' else NS(content=[{'type': 'text', 'text': 'answer'}], stop_reason='end_turn'))
    client = Client(response)
    options = {'messages': [{'role': 'user', 'content': 'override'}]} if kind == 'openai' else {}
    result = await provider(kind, client, hook).chat(
        [{'role': 'system', 'content': 'policy'}, {'role': 'user', 'content': 'question'}],
        [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}],
        False, '', options)
    call = next(c for c in hook.calls if c[0] == 'set_input')
    assert call[1] == (client.kwargs['messages'],)
    assert call[2] == {'tools': client.kwargs.get('tools'), 'system': client.kwargs.get('system')}
    assert next(c for c in hook.calls if c[0] == 'set_output')[1] == (result.message,)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
async def test_stream_captures_raw_multiple_tool_deltas_and_error(kind):
    hook = Attempt()
    if kind == 'openai':
        items = [NS(choices=[NS(delta=NS(content='hello', tool_calls=[NS(index=2, id='a', function=NS(name='first', arguments='{"x":'))]), finish_reason=None)]),
                 NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=2, id=None, function=NS(name=None, arguments='1}')), NS(index=4, id='b', function=NS(name='second', arguments='{}'))]), finish_reason=None)])]
    else:
        items = [{'type': 'content_block_start', 'index': 2, 'content_block': {'type': 'tool_use', 'id': 'a', 'name': 'first', 'input': {}}},
                 {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '{"x":'}},
                 {'type': 'content_block_delta', 'index': 2, 'delta': {'type': 'input_json_delta', 'partial_json': '1}'}},
                 {'type': 'content_block_start', 'index': 4, 'content_block': {'type': 'tool_use', 'id': 'b', 'name': 'second', 'input': {'ok': True}}},
                 {'type': 'content_block_delta', 'index': 5, 'delta': {'type': 'text_delta', 'text': 'hello'}}]
    client = Client(events(items + [RuntimeError('broken transport')]))
    stream = await provider(kind, client, hook).chat([{'role': 'user', 'content': 'q'}], [], True, '', {})
    output = [e async for e in stream]
    assert output[-1].error == 'broken transport'
    assert ('append_output_text', ('hello',), {}) in hook.calls
    tools = [c for c in hook.calls if c[0] == 'append_output_tool_delta']
    assert {c[1][0] for c in tools} == {2, 4}
    assert [c[2]['arguments'] for c in tools if c[1][0] == 2 and c[2].get('arguments')] == ['{"x":', '1}']
    assert ('set_error_detail', ('broken transport',), {}) in hook.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
async def test_optional_content_hook_failure_never_changes_result(kind):
    class Broken(Attempt):
        def set_input(self, *args, **kwargs):
            raise RuntimeError('telemetry failure')
        def set_output(self, *args, **kwargs):
            raise RuntimeError('telemetry failure')
    response = (NS(choices=[NS(message=NS(role='assistant', content='ok'), finish_reason='stop')])
                if kind == 'openai' else NS(content=[{'type': 'text', 'text': 'ok'}], stop_reason='end_turn'))
    result = await provider(kind, Client(response), Broken()).chat([], [], False, '', {})
    assert result.message['content'] == 'ok'


@pytest.mark.asyncio
async def test_anthropic_nonempty_initial_input_is_omitted_without_serialization(monkeypatch):
    from app.application import telemetry_semantics as ts
    from app.application.telemetry_details import configure_details
    from app.infrastructure.observability.sanitizer import LogSanitizer
    from tests.infrastructure.test_provider_telemetry_callback import FakeTracer
    tracer = FakeTracer()
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    configure_details(LogSanitizer().sanitize)
    items = [{'type': 'content_block_start', 'index': 0, 'content_block':
              {'type': 'tool_use', 'id': 't', 'name': 'lookup', 'input': {'password': 'initial-sensitive'}}},
             {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}]
    client = Client(events(items))
    service = AnthropicProvider('', '', 'test-model', client=client,
                                telemetry_callback=ts.SemanticsTelemetry().begin_chat_attempt)
    try:
        stream = await service.chat([], [], True, '', {})
        result = [e async for e in stream]
        assert result[-1].finish_reason == 'tool_calls'
        attrs = tracer.spans[0].attributes
        assert not attrs.get('nagent.details.output_messages.incomplete', False)
        assert 'initial-sensitive' not in str(attrs)
        assert 'lookup' in attrs['nagent.details.output_messages.content']
        assert '[REDACTED]' in attrs['nagent.details.output_messages.content']
    finally:
        configure_details(None)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
@pytest.mark.parametrize('termination', ['complete', 'error', 'cancel', 'close'])
async def test_stream_lifecycle_content_details_are_bounded_and_fail_closed(kind, termination, monkeypatch):
    import asyncio
    from app.application import telemetry_semantics as ts
    from app.application.telemetry_details import configure_details
    from app.infrastructure.observability.sanitizer import LogSanitizer
    from tests.infrastructure.test_provider_telemetry_callback import FakeTracer
    tracer = FakeTracer()
    monkeypatch.setattr(ts, 'get_tracer', lambda: tracer)
    configure_details(LogSanitizer().sanitize)
    if kind == 'openai':
        items = [NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=0, id='a', function=NS(name='lookup', arguments='{"password":"never-export"}'))]), finish_reason=None)]),
                 NS(choices=[NS(delta=NS(content=None, tool_calls=None), finish_reason='tool_calls')])]
    else:
        items = [{'type': 'content_block_start', 'content_block': {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {}}},
                 {'type': 'content_block_delta', 'delta': {'type': 'input_json_delta', 'partial_json': '{"password":"never-export"}'}},
                 {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}]
    async def source():
        for event in items:
            yield event
        if termination == 'cancel':
            raise asyncio.CancelledError()
        if termination == 'error':
            raise RuntimeError('transport error')
    service = (OpenAICompatibleProvider if kind == 'openai' else AnthropicProvider)(
        '', '', 'test-model', client=Client(source()), telemetry_callback=ts.SemanticsTelemetry().begin_chat_attempt)
    try:
        stream = await service.chat([], [], True, '', {})
        if termination == 'close':
            await anext(stream)  # message_start precedes the attempt
            await anext(stream)  # real tool delta
            await stream.aclose()
        elif termination == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                _ = [event async for event in stream]
        else:
            _ = [event async for event in stream]
        attrs = tracer.spans[0].attributes
        assert 'never-export' not in str(attrs)
        prefix = 'nagent.details.output_messages.'
        assert attrs[prefix + 'truncated'] is (termination != 'complete')
        if termination != 'complete':
            assert attrs[prefix + 'incomplete'] is True
        else:
            assert 'lookup' in attrs[prefix + 'content']
        assert tracer.spans[0].ended == 1
    finally:
        configure_details(None)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
async def test_legacy_hook_without_content_methods_remains_compatible(kind):
    class Legacy:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def set_usage(self, *args):
            pass
        def set_finish_reasons(self, *args):
            pass
        def set_error(self, *args):
            pass
    response = (NS(choices=[NS(message=NS(role='assistant', content='ok'), finish_reason='stop')])
                if kind == 'openai' else NS(content=[{'type': 'text', 'text': 'ok'}], stop_reason='end_turn'))
    result = await provider(kind, Client(response), Legacy()).chat([], [], False, '', {})
    assert result.message['content'] == 'ok'
