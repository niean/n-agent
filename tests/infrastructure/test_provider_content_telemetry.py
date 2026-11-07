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
        assert not attrs.get('nagent.content.output_messages_incomplete', False)
        assert 'initial-sensitive' not in str(attrs)
        assert 'lookup' in attrs['nagent.content.output_messages']
        assert '[REDACTED]' in attrs['nagent.content.output_messages']
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
        prefix = 'nagent.content.output_messages'
        assert attrs[prefix + '_truncated'] is (termination != 'complete')
        if termination != 'complete':
            assert attrs[prefix + '_incomplete'] is True
        else:
            assert 'lookup' in attrs[prefix]
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


def test_hook_protocol_and_noops_have_set_cache_usage():
    from app.infrastructure.llm.telemetry_hook import (
        ChatAttemptHook, NoopChatAttempt, NOOP_CHAT_ATTEMPT,
    )
    assert callable(ChatAttemptHook.set_cache_usage)
    NoopChatAttempt().set_cache_usage(cache_read_tokens=1, cache_write_tokens=2)
    NOOP_CHAT_ATTEMPT.set_cache_usage()
    from app.application.telemetry_semantics import NoopChatAttempt as AppNoop
    AppNoop().set_cache_usage(cache_read_tokens=1)


def test_optional_hook_isolates_missing_and_failing_cache_method():
    from app.infrastructure.llm.telemetry_hook import optional_hook, NoopChatAttempt

    class Legacy:
        pass

    class Broken:
        def set_cache_usage(self, **kwargs):
            raise RuntimeError('boom')

    # missing method, failing method, noop handle and non-handle are all isolated
    optional_hook(Legacy(), 'set_cache_usage', cache_read_tokens=50)
    optional_hook(Broken(), 'set_cache_usage', cache_read_tokens=50)
    optional_hook(NoopChatAttempt(), 'set_cache_usage', cache_read_tokens=50)
    optional_hook(None, 'set_cache_usage', cache_read_tokens=50)


def _openai_usage(prompt=None, completion=None, cached='missing'):
    usage = NS(prompt_tokens=prompt, completion_tokens=completion)
    if cached != 'missing':
        usage.prompt_tokens_details = NS(cached_tokens=cached)
    return usage


def _openai_response(usage, content='ok'):
    return NS(choices=[NS(message=NS(role='assistant', content=content), finish_reason='stop')],
              usage=usage)


def _call(hook, name):
    return next((c for c in hook.calls if c[0] == name), None)


@pytest.mark.asyncio
async def test_openai_non_stream_extracts_cache_and_fixes_input():
    hook = Attempt()
    result = await provider('openai', Client(_openai_response(
        _openai_usage(prompt=150, completion=10, cached=50))), hook).chat([], [], False, '', {})
    assert result.message['content'] == 'ok'
    assert _call(hook, 'set_usage')[1] == (100, 10)  # Ti = prompt - cached
    assert _call(hook, 'set_cache_usage')[2] == {'cache_read_tokens': 50, 'cache_write_tokens': 0}


@pytest.mark.asyncio
async def test_openai_missing_cached_tokens_leaves_input_unknown():
    hook = Attempt()
    await provider('openai', Client(_openai_response(
        _openai_usage(prompt=150, completion=10))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_usage')[1] == (None, 10)  # no prompt-as-Ti, no 0-subtraction
    assert _call(hook, 'set_cache_usage') is None


@pytest.mark.asyncio
async def test_openai_cached_exceeds_prompt_is_invalid():
    hook = Attempt()
    await provider('openai', Client(_openai_response(
        _openai_usage(prompt=150, completion=10, cached=200))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_usage')[1] == (None, 10)  # conflict: no truncation cover-up
    assert _call(hook, 'set_cache_usage') is None


@pytest.mark.asyncio
async def test_openai_prompt_missing_but_cached_valid_keeps_tic():
    hook = Attempt()
    await provider('openai', Client(_openai_response(
        _openai_usage(prompt=None, completion=10, cached=50))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_usage')[1] == (None, 10)
    assert _call(hook, 'set_cache_usage')[2] == {'cache_read_tokens': 50, 'cache_write_tokens': 0}


@pytest.mark.asyncio
async def test_openai_invalid_component_types_are_rejected():
    hook = Attempt()
    await provider('openai', Client(_openai_response(
        _openai_usage(prompt='150', completion=True, cached=1.5))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_usage')[1] == (None, None)
    assert _call(hook, 'set_cache_usage') is None


@pytest.mark.asyncio
async def test_openai_stream_usage_chunks_cover_both_shapes():
    hook = Attempt()
    usage_full = _openai_usage(prompt=150, completion=10, cached=50)
    items = [NS(choices=[NS(delta=NS(content='hello', tool_calls=None), finish_reason=None)]),
             NS(choices=[NS(delta=NS(content=None, tool_calls=None), finish_reason='stop')], usage=usage_full),
             NS(choices=[], usage=_openai_usage(prompt=150, completion=10, cached=50))]
    stream = await provider('openai', Client(events(items)), hook).chat([{'role': 'user', 'content': 'q'}], [], True, '', {})
    output = [e async for e in stream]
    # both usage-bearing chunks extracted; content delta not lost
    assert ('append_output_text', ('hello',), {}) in hook.calls
    assert output[-1].finish_reason == 'stop'
    set_usages = [c for c in hook.calls if c[0] == 'set_usage']
    assert set_usages[-1][1] == (100, 10)
    cache_calls = [c for c in hook.calls if c[0] == 'set_cache_usage']
    assert cache_calls[-1][2] == {'cache_read_tokens': 50, 'cache_write_tokens': 0}


def _anthropic_usage(input_tokens=None, output_tokens=None, cache_read='missing', cache_write='missing'):
    usage = NS(input_tokens=input_tokens, output_tokens=output_tokens)
    if cache_read != 'missing':
        usage.cache_read_input_tokens = cache_read
    if cache_write != 'missing':
        usage.cache_creation_input_tokens = cache_write
    return usage


def _anthropic_response(usage, text='ok'):
    return NS(content=[{'type': 'text', 'text': text}], stop_reason='end_turn', usage=usage)


@pytest.mark.asyncio
async def test_anthropic_non_stream_passes_cache_buckets():
    hook = Attempt()
    result = await provider('anthropic', Client(_anthropic_response(
        _anthropic_usage(100, 10, cache_read=50, cache_write=20))), hook).chat([], [], False, '', {})
    assert result.message['content'] == 'ok'
    assert _call(hook, 'set_usage')[1] == (100, 10)
    assert _call(hook, 'set_cache_usage')[2] == {'cache_read_tokens': 50, 'cache_write_tokens': 20}


@pytest.mark.asyncio
async def test_anthropic_stream_start_then_delta_replacement():
    hook = Attempt()
    items = [{'type': 'message_start',
              'message': {'usage': {'input_tokens': 100, 'output_tokens': 1,
                                    'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 20}}},
             {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hello'}},
             {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
              'usage': {'output_tokens': 10}}]
    stream = await provider('anthropic', Client(events(items)), hook).chat([{'role': 'user', 'content': 'q'}], [], True, '', {})
    output = [e async for e in stream]
    assert output[-1].finish_reason == 'stop'  # end_turn mapped to unified vocabulary
    # delta's cumulative output replaces the start value; delta lacks cache
    # fields so the start values survive (None never erases)
    set_usages = [c for c in hook.calls if c[0] == 'set_usage']
    assert set_usages[0][1] == (100, 1)
    assert set_usages[-1][1] == (None, 10)
    cache_calls = [c for c in hook.calls if c[0] == 'set_cache_usage']
    assert cache_calls[0][2] == {'cache_read_tokens': 50, 'cache_write_tokens': 20}
    assert cache_calls[-1][2] == {'cache_read_tokens': None, 'cache_write_tokens': None}


@pytest.mark.asyncio
async def test_anthropic_partial_cache_buckets():
    hook = Attempt()
    await provider('anthropic', Client(_anthropic_response(
        _anthropic_usage(100, 10, cache_read=50))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_cache_usage')[2] == {'cache_read_tokens': 50, 'cache_write_tokens': None}


@pytest.mark.asyncio
async def test_anthropic_invalid_component_types_are_rejected():
    hook = Attempt()
    await provider('anthropic', Client(_anthropic_response(
        _anthropic_usage('100', True, cache_read=1.5, cache_write=-1))), hook).chat([], [], False, '', {})
    assert _call(hook, 'set_usage')[1] == (None, None)
    assert _call(hook, 'set_cache_usage')[2] == {'cache_read_tokens': None, 'cache_write_tokens': None}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_final_parameters_reported_before_sdk_create(kind, streaming):
    hook = Attempt()
    response = (NS(choices=[NS(message=NS(role='assistant', content='ok'), finish_reason='stop')])
                if kind == 'openai' else NS(content=[], stop_reason='end_turn'))
    class CheckingClient(Client):
        async def create(self, **kwargs):
            parameters = next(c for c in hook.calls if c[0] == 'set_model_parameters')[1][0]
            assert parameters == {key: value for key, value in kwargs.items() if key in ("temperature", "max_tokens")}
            assert "messages" not in parameters
            return await super().create(**kwargs)
    client = CheckingClient(events([]) if streaming else response)
    result = await provider(kind, client, hook).chat([], [], streaming, '', {'temperature': .3, 'max_tokens': 123})
    if streaming:
        _ = [event async for event in result]
    assert client.kwargs['max_tokens'] == 123

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openai', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_actual_response_model_is_reported(kind, streaming):
    hook = Attempt()
    response = (NS(model='actual-response', choices=[NS(message=NS(role='assistant', content='ok'), finish_reason='stop')])
                if kind == 'openai' else NS(model='actual-response', content=[], stop_reason='end_turn'))
    items = [NS(model='actual-response', choices=[])] if kind == 'openai' else [{'type': 'message_start', 'message': {'model': 'actual-response'}}]
    result = await provider(kind, Client(events(items) if streaming else response), hook).chat([], [], streaming, '', {})
    if streaming:
        _ = [event async for event in result]
    assert ('set_response_model', ('actual-response',), {}) in hook.calls
