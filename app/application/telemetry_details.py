"""Fail-closed, bounded content projection shared by telemetry outlets.

Only startup wiring supplies the sanitizer; this module has no infrastructure
imports and never mutates business values.
"""
from collections.abc import Callable
import json
import re

FIELDS = frozenset(('user_message', 'assistant_message', 'system_prompt',
                    'input_messages', 'tool_definitions', 'output_messages',
                    'tool_arguments', 'tool_result', 'error', 'http_request',
                    'http_response', 'log_fields'))
_METADATA = frozenset(('content', 'original_bytes', 'truncated', 'incomplete', 'observed_bytes'))
_config: tuple[Callable[[str], str] | None, int] = (None, 2048)
_SENSITIVE = re.compile(r'api.?key|token|password|authorization|cookie|credential|secret|private.?key', re.I)
_MEDIA = re.compile(r'^data:[^,]*;base64,', re.I)
_TOKEN_COUNTS = frozenset(('max_tokens', 'max_completion_tokens', 'max_output_tokens',
                          'input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens',
                          'reasoning_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))


def configure_details(sanitizer: Callable[[str], str] | None, max_bytes: int = 2048) -> None:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes must be a positive integer')
    global _config
    _config = (sanitizer, max_bytes)


def _sanitize(text, sanitizer):
    result = sanitizer(text)
    if type(result) is not str:
        raise ValueError('sanitizer must return text')
    return result


def _clean(value, sanitizer, depth=0):
    if depth > 64:
        raise ValueError('detail nesting too deep')
    if type(value) is str:
        if _MEDIA.match(value):
            return '[REDACTED MEDIA]'
        # Providers often encode nested tool arguments as JSON strings.
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                return json.dumps(_clean(parsed, sanitizer, depth + 1), ensure_ascii=False, allow_nan=False)
        return _sanitize(value, sanitizer)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return '[REDACTED MEDIA]'
    if value is None or type(value) in (bool, int, float):
        text = json.dumps(value, allow_nan=False)
        sanitized = _sanitize(text, sanitizer)
        return value if sanitized == text else sanitized
    if type(value) in (list, tuple):
        return [_clean(v, sanitizer, depth + 1) for v in value]
    if type(value) is dict:
        value = {(k if type(k) is str else json.dumps(k) if type(k) in (int, float, bool, type(None))
                  else '[unsupported key]'): v for k, v in value.items()}
        if value.get('type') in ('base64', 'image', 'image_url', 'input_image', 'input_audio', 'audio'):
            return '[REDACTED MEDIA]'
        return {_sanitize(k, sanitizer): '[REDACTED]' if _SENSITIVE.search(k) and not (
                k.lower() in _TOKEN_COUNTS and type(v) in (int, float)) else
                '[REDACTED MEDIA]' if k.lower() in ('base64', 'b64_json', 'image_data', 'audio_data') else
                _clean(v, sanitizer, depth + 1) for k, v in value.items()}
    return _sanitize('[unsupported value: ' + type(value).__name__[:64] + ']', sanitizer)


def _truncate(text: str, limit: int) -> str:
    return text[:limit].encode('utf-8')[:limit].decode('utf-8', errors='ignore')


def _utf8_size(text: str) -> int:
    return sum(len(text[start:start + 1024].encode('utf-8'))
               for start in range(0, len(text), 1024))


def project_details(**fields) -> dict:
    sanitizer, limit = _config
    if sanitizer is None:
        return {}
    result = {}
    for name, value in fields.items():
        if name not in FIELDS:
            continue
        try:
            # Even all-redacted or primitive-only structures fail closed when
            # the installed sanitizer is unavailable or violates its contract.
            _sanitize('', sanitizer)
            clean = _clean(value, sanitizer)
            text = clean if type(clean) is str else json.dumps(clean, ensure_ascii=False, allow_nan=False)
            size = _utf8_size(text)
            result[name] = dict(content=_truncate(text, limit), original_bytes=size, truncated=size > limit)
        except Exception:
            continue
    return result


def validate_details(details) -> dict:
    """Return only a fully valid projected envelope; reject extra metadata."""
    if type(details) is not dict:
        return {}
    result = {}
    for name, item in details.items():
        if name not in FIELDS or type(item) is not dict or set(item) - _METADATA:
            continue
        if not {'content', 'original_bytes', 'truncated'} <= item.keys():
            continue
        if type(item['content']) is not str or type(item['truncated']) is not bool:
            continue
        size = item['original_bytes']
        if size is not None and (type(size) is not int or size < 0):
            continue
        if 'incomplete' in item and type(item['incomplete']) is not bool:
            continue
        if 'observed_bytes' in item and (type(item['observed_bytes']) is not int or item['observed_bytes'] < 0):
            continue
        try:
            content_size = len(item['content'].encode('utf-8'))
        except UnicodeError:
            continue
        if content_size > _config[1]:
            continue
        result[name] = dict(item)
    return result


def flatten_details(details, prefix='nagent.details.') -> dict:
    return {f'{prefix}{name}.{key}': value for name, item in validate_details(details).items()
            for key, value in item.items() if value is not None}


class StreamDetailsAccumulator:
    """Keep at most 4 * max_bytes raw UTF-8 bytes across text/tool deltas.

    Once exhausted, no raw prefix or new tool IDs are retained. Incomplete
    streams deliberately omit all raw content, including unclosed credentials.
    """
    def __init__(self, max_bytes: int | None = None):
        self.max_bytes = _config[1] if max_bytes is None else max_bytes
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError('max_bytes must be positive')
        self.observed_bytes = 0
        self.buffered_bytes = 0
        self._overflow = False
        self._text = ''
        self._tools = {}

    def _reserve(self, parts) -> bool:
        size = sum(_utf8_size(v) for v in parts if type(v) is str)
        self.observed_bytes += size
        # A per-call slot charge bounds empty tool deltas and dictionary overhead.
        if self._overflow or self.buffered_bytes + size + 1 > 4 * self.max_bytes:
            self._overflow = True
            return False
        self.buffered_bytes += size + 1
        return True

    def append_text(self, text: str) -> None:
        if type(text) is str:
            if self._reserve((text,)):
                self._text += text
            else:
                remaining = max(0, 4 * self.max_bytes - self.buffered_bytes)
                prefix = _truncate(text, remaining)
                self._text += prefix
                self.buffered_bytes += len(prefix.encode('utf-8'))

    def append_tool_delta(self, index: int, *, id: str | None = None,
                          name: str | None = None, arguments: str | None = None) -> None:
        if type(index) is not int or index < 0:
            return
        if not self._reserve((id, name, arguments)):
            return
        item = self._tools.setdefault(index, dict(id='', name='', arguments=''))
        for key, value in (('id', id), ('name', name), ('arguments', arguments)):
            if type(value) is str:
                item[key] += value

    def finish(self, incomplete: bool = False) -> dict:
        try:
            return self._finish(incomplete)
        finally:
            self._text = ''
            self._tools.clear()
            self.buffered_bytes = 0

    def _finish(self, incomplete: bool) -> dict:
        partial = self._overflow or incomplete
        sanitizer = _config[0]
        owner = getattr(sanitizer, '__self__', None)
        partial_sanitizer = getattr(owner, 'sanitize_partial', sanitizer)
        tools = []
        for tool in self._tools.values():
            item = dict(tool)
            try:
                item['arguments'] = json.loads(item['arguments'])
            except (ValueError, TypeError):
                # Incomplete JSON may contain credentials not recognized in plain text.
                item['arguments'] = _sanitize(item['arguments'], partial_sanitizer) if partial_sanitizer else '[REDACTED]'
            tools.append(item)
        text = _sanitize(self._text, partial_sanitizer) if partial and partial_sanitizer else self._text
        value = {'text': text, 'tool_calls': tools} if tools else text
        details = project_details(output_messages=value)
        if details:
            item = details['output_messages']
            item['content'] = _truncate(item['content'], self.max_bytes)
            item['truncated'] = item['original_bytes'] > len(item['content'].encode('utf-8'))
            if partial:
                item.update(original_bytes=None, truncated=True, incomplete=True,
                            observed_bytes=self.observed_bytes)
        return details
