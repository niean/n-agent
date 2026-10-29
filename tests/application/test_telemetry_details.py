import json

import pytest

from app.application.telemetry_details import (
    StreamDetailsAccumulator, configure_details, flatten_details, project_details,
    validate_details,
)
from app.infrastructure.observability.sanitizer import LogSanitizer


@pytest.fixture(autouse=True)
def setup_details():
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({'secret-split-value'})
    configure_details(sanitizer.sanitize)
    yield
    configure_details(None)


def test_utf8_redaction_before_budget():
    configure_details(lambda s: s.replace('secret', '安全'), 7)
    item = project_details(user_message='secret😀中文')['user_message']
    assert item == {'content': '安全', 'original_bytes': 16, 'truncated': True}
    configure_details(lambda s: s, 1)
    assert project_details(error='😀')['error']['content'] == ''


def test_structures_credentials_media_and_no_mutation():
    value = {'api_key': 'hidden', 'nested': [{'Authorization': 'bearer'}],
             'image': 'data:image/png;base64,AAA', 'bytes': b'raw'}
    content = project_details(tool_arguments=value)['tool_arguments']['content']
    assert all(s not in content for s in ('hidden', 'bearer', 'AAA', 'raw'))
    assert value['api_key'] == 'hidden'
    assert json.loads(content)['api_key'] == '[REDACTED]'


def test_fail_closed_and_reject_objects_unknown_fields():
    assert project_details(unknown='text') == {}
    assert 'unsupported value' in project_details(tool_result=object())['tool_result']['content']
    for sanitizer in (None, lambda s: None, lambda s: 1):
        configure_details(sanitizer)
        assert project_details(error='private') == {}
    def broken(s):
        raise ValueError('secret')
    configure_details(broken)
    assert project_details(error='private') == {}


def test_export_validation_and_null_omission():
    details = project_details(error='safe')
    details['other'] = {'content': 'private'}
    details['error']['injected'] = 'private'
    assert validate_details(details) == {}
    item = {'output_messages': {'content': 'limit', 'original_bytes': None,
                               'truncated': True, 'incomplete': True, 'observed_bytes': 99}}
    flat = flatten_details(item)
    assert flat['nagent.details.output_messages.observed_bytes'] == 99
    assert 'nagent.details.output_messages.original_bytes' not in flat


def test_stream_cross_chunk_secret_and_tool_deltas():
    stream = StreamDetailsAccumulator()
    stream.append_text('secret-split-')
    stream.append_text('value')
    stream.append_tool_delta(0, id='call1', name='tool', arguments='{"token":')
    stream.append_tool_delta(0, arguments='"sensitive"}')
    content = stream.finish()['output_messages']['content']
    assert 'secret-split-value' not in content
    assert 'sensitive' not in content
    assert 'call1' in content


def test_stream_overflow_stops_growth_and_incomplete_drops_prefix():
    stream = StreamDetailsAccumulator(max_bytes=20)
    stream.append_text('-----BEGIN PRIVATE KEY-----' + 'x' * 100)
    for i in range(100):
        stream.append_tool_delta(i, id=str(i), arguments='secret')
    item = stream.finish(incomplete=True)['output_messages']
    assert item['original_bytes'] is None and item['observed_bytes'] > 127
    assert item['truncated'] and item['incomplete']
    assert len(item['content'].encode()) <= 20
    assert stream.buffered_bytes == 0
    short = StreamDetailsAccumulator()
    short.append_text('-----BEGIN PRIVATE KEY-----\nsecret')
    assert 'secret' not in short.finish(incomplete=True)['output_messages']['content']


def test_completed_overflow_is_still_incomplete():
    stream = StreamDetailsAccumulator(max_bytes=4)
    stream.append_text('x' * 17)
    item = stream.finish()['output_messages']
    assert item['incomplete'] is True
    assert item['truncated'] is True


def test_settings_env_positive(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv('N_AGENT_OTEL_CONTENT_MAX_BYTES', raising=False)
    assert Settings(_env_file=None).otel_content_max_bytes == 2048
    monkeypatch.setenv('N_AGENT_OTEL_CONTENT_MAX_BYTES', '17')
    assert Settings(_env_file=None).otel_content_max_bytes == 17
    monkeypatch.setenv('N_AGENT_OTEL_CONTENT_MAX_BYTES', '0')
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_main_injects_catalog_and_budget(monkeypatch):
    from types import SimpleNamespace
    from app.main import _wire_log_sanitizer
    from app.infrastructure.observability import logging as obs
    from app.infrastructure.observability import http_capture
    from app.application import telemetry_semantics as ts
    monkeypatch.setattr(obs, '_sanitizer', obs._sanitizer)
    monkeypatch.setattr(obs, '_content_max_bytes', obs._content_max_bytes)
    monkeypatch.setattr(http_capture, '_content_limit', http_capture._content_limit)
    monkeypatch.setattr(ts, '_span_attr_sanitizer', ts._span_attr_sanitizer)
    _wire_log_sanitizer(SimpleNamespace(secrets=SimpleNamespace(secret_values={'secret'})), max_bytes=4)
    result = project_details(error='secret!')['error']
    assert result['content'] == '[RED' and result['original_bytes'] == 11


def test_nested_json_arguments_credentials():
    content = project_details(output_messages=[{'arguments': '{"Authorization":"private-value"}'}])
    assert 'private-value' not in str(content)


def test_media_source_base64_is_omitted():
    content = project_details(input_messages=[{'type': 'base64', 'data': 'raw-image-body'},
        {'type': 'image', 'source': {'type': 'base64', 'data': 'raw-image-body'}},
        {'type': 'input_audio', 'input_audio': {'data': 'raw-image-body'}}])
    assert 'raw-image-body' not in str(content)


@pytest.mark.parametrize('incomplete', [False, True])
def test_finish_clears_raw_buffers(incomplete):
    stream = StreamDetailsAccumulator()
    stream.append_text('raw text')
    stream.append_tool_delta(0, id='call', name='tool', arguments='{}')
    assert stream.finish(incomplete=incomplete)
    assert stream.buffered_bytes == 0
    assert stream._text == '' and stream._tools == {}


@pytest.mark.parametrize('secret', ['abc\ndef', 'abc"def', 'abc\\def'])
def test_catalog_secrets_are_redacted_before_json_escaping(secret):
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({secret})
    configure_details(sanitizer.sanitize)
    item = project_details(tool_result={secret: secret, 'nested': json.dumps({'stdout': secret})})
    parsed = json.loads(item['tool_result']['content'])
    assert secret not in parsed
    assert secret not in parsed.values()
    assert json.loads(parsed['nested'])['stdout'] == '[REDACTED]'


def test_stream_utf8_count_uses_bounded_encoding_chunks():
    from app.application.telemetry_details import _utf8_size
    class TrackedString(str):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            assert len(value) <= 1024
            return value
        def encode(self, *args, **kwargs):
            raise AssertionError('whole delta must not be encoded')
    assert _utf8_size(TrackedString('😀' * 10000)) == 40000


def test_primitive_secrets_redacted_without_changing_normal_types():
    sanitizer = LogSanitizer()
    sanitizer.configure_secret_catalog({'123456', 'true'})
    configure_details(sanitizer.sanitize)
    assert project_details(tool_result=123456)['tool_result']['content'] == '[REDACTED]'
    result = json.loads(project_details(tool_result={'stdout': 123456, 'flag': True,
        'normal': 42, 'empty': None, 'ratio': 1.5})['tool_result']['content'])
    assert result == {'stdout': '[REDACTED]', 'flag': '[REDACTED]',
                      'normal': 42, 'empty': None, 'ratio': 1.5}
