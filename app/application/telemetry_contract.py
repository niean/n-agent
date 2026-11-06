"""Pure, fixed public observability contract. No SDK or I/O dependencies."""
import math
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class OperationSpec:
    object: str
    action: str
    standard_operation: str | None
    span_name: str
    event_name: str
    local_attributes: frozenset[str]
    shared_attributes: frozenset[str]
    outcomes: frozenset[str]
    instruments: tuple[str, ...]

    @property
    def allowed_attributes(self):
        return self.local_attributes | self.shared_attributes


_definitions = {
    'invoke_agent': ('agent', 'invoke', 'invoke_agent'),
    'chat': ('model', 'chat', 'chat'),
    'execute_tool': ('tool', 'execute', 'execute_tool'),
    'context_compression': ('context', 'compress', None),
    'nagent.context.compress': ('context', 'compress', None),
    'context.compress': ('context', 'compress', None),
    'task.submit': ('task', 'submit', None), 'task.run': ('task', 'run', None),
    'delegation.member': ('delegation', 'run_member', None),
    'delegation.run_member': ('delegation', 'run_member', None),
    'schedule.execute': ('schedule', 'run', None), 'schedule.run': ('schedule', 'run', None),
    'skill.review': ('skill', 'review', None), 'curator.run': ('curator', 'run', None),
    'gateway.message': ('gateway', 'handle_message', None),
    'gateway.handle_message': ('gateway', 'handle_message', None),
    'cli.request': ('request', 'handle', None), 'acp.request': ('request', 'handle', None),
    'feishu.event': ('request', 'handle', None), 'request.handle': ('request', 'handle', None),
    'http.server': ('http', 'handle', None), 'http.client': ('http', 'handle', None),
}
for _old, _action in {'prompt': 'prepare_prompt', 'prefetch': 'prefetch', 'sync': 'sync', 'pre_compress': 'pre_compress', 'tool': 'execute_tool', 'probe': 'probe', 'search': 'search', 'prepare_prompt': 'prepare_prompt', 'execute_tool': 'execute_tool'}.items():
    _definitions[f'memory.{_old}'] = ('memory', _action, None)
OBJECTS = frozenset({'agent', 'model', 'tool', 'context', 'task', 'delegation', 'schedule', 'memory', 'skill', 'curator', 'gateway', 'request', 'http', 'budget', 'policy', 'observation', 'metric', 'content'})
OBJECT_ATTRIBUTES = MappingProxyType({
    'agent': ('source', 'run_id', 'iteration', 'end_reason', 'message_count', 'inference_calls', 'tool_calls', 'usage_known'),
    'model': ('message_count', 'usage_known', 'cache_read_tokens', 'cache_write_tokens'), 'tool': ('policy_outcome', 'risk_level', 'executed'),
    'context': ('before_tokens', 'after_tokens'), 'task': ('id', 'run_id', 'trace_context', 'state'),
    'delegation': ('id', 'member_id', 'role'), 'schedule': ('task_id', 'claim_id', 'delivery_status'),
    'memory': ('provider',), 'request': ('channel', 'id'), 'http': ('caller', 'direction'),
    'budget': ('kind',), 'policy': ('name', 'version', 'decision_kind', 'reason', 'outcome', 'scope', 'occurred_at'),
    'observation': ('error',), 'metric': ('overflow',),
})
CONTENT_FIELDS = ('input_messages', 'user_message', 'assistant_message', 'system_prompt', 'tool_definitions', 'output_messages', 'tool_arguments', 'tool_result', 'error', 'http_request', 'http_response', 'log_fields')
CONTENT_ATTRIBUTE_KEYS = frozenset(f'nagent.content.{field}{suffix}' for field in CONTENT_FIELDS for suffix in ('', '_original_bytes', '_truncated', '_incomplete', '_observed_bytes'))
_standard = {'session_id': 'gen_ai.conversation.id', 'model': 'gen_ai.request.model', 'provider': 'gen_ai.provider.name', 'response_model': 'gen_ai.response.model', 'tool': 'gen_ai.tool.name', 'call_id': 'gen_ai.tool.call.id', 'input_tokens': 'gen_ai.usage.input_tokens', 'output_tokens': 'gen_ai.usage.output_tokens', 'finish_reason': 'gen_ai.response.finish_reasons', 'stream': 'gen_ai.request.stream', 'error_type': 'error.type'}
OUTCOMES = frozenset({'success', 'failure', 'denied', 'cancelled', 'timed_out', 'skipped', 'deferred', 'unknown'})
SHARED_ATTRIBUTES = frozenset({'gen_ai.conversation.id', 'nagent.agent.run_id', 'nagent.agent.source', 'nagent.agent.iteration', 'nagent.request.id', 'nagent.task.id', 'nagent.task.run_id', 'nagent.delegation.id', 'nagent.delegation.member_id', 'nagent.schedule.task_id', 'nagent.schedule.claim_id'})
_standard_local = {
    'agent': ('gen_ai.agent.name', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens'),
    'model': tuple(value for key, value in _standard.items() if key not in {'session_id', 'tool', 'call_id'}),
    'tool': ('gen_ai.tool.name', 'gen_ai.tool.call.id', 'gen_ai.tool.type', 'gen_ai.agent.name'),
}
_instruments = {
    'agent': ('gen_ai.invoke_agent.duration', 'gen_ai.invoke_agent.inference_calls', 'gen_ai.invoke_agent.tool_calls', 'nagent.agent.runs'),
    'model': ('gen_ai.client.inference.usage.input_tokens', 'gen_ai.client.inference.usage.output_tokens', 'gen_ai.client.inference.operation.input_tokens', 'gen_ai.client.inference.operation.output_tokens', 'gen_ai.client.inference.duration', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens', 'nagent.model.normalized_tokens', 'nagent.model.cache_eligible_prompt_tokens', 'nagent.model.cache_eligible_read_tokens'),
    'tool': ('gen_ai.execute_tool.duration',), 'context': ('nagent.context.compression_attempts',),
}


def _operation_spec(obj, action, standard):
    local = frozenset(f'nagent.{obj}.{field}' for field in (*OBJECT_ATTRIBUTES.get(obj, ()), 'outcome', 'summary', 'duration_ms'))
    local |= frozenset(_standard_local.get(obj, ())) | {'error.type'} | CONTENT_ATTRIBUTE_KEYS
    local |= {'gen_ai.operation.name'} if standard else {f'nagent.{obj}.operation'}
    return OperationSpec(obj, action, standard, standard or f'{obj}.{action}', f'nagent.{obj}.completed', local, SHARED_ATTRIBUTES, OUTCOMES, _instruments.get(obj, ()))


OPERATIONS = MappingProxyType({name: _operation_spec(*definition) for name, definition in _definitions.items()})
EXECUTION_ATTRIBUTE_KEYS = frozenset(_standard.values()) | {'gen_ai.operation.name', 'gen_ai.agent.name', 'gen_ai.tool.type'} | CONTENT_ATTRIBUTE_KEYS | frozenset(f'nagent.{obj}.{field}' for obj, fields in OBJECT_ATTRIBUTES.items() for field in fields) | frozenset(f'nagent.{obj}.{field}' for obj in {s.object for s in OPERATIONS.values()} for field in ('operation', 'outcome', 'summary', 'duration_ms'))


# One exact type schema for canonical attributes at every execution outlet.
# Strings, IDs, counts, booleans and durations are distinct contracts; in
# particular Python bool is not an integer count. Content still requires the
# separate sanitized-envelope validator before any content outlet.
_attribute_types = {key: 'string' for key in EXECUTION_ATTRIBUTE_KEYS}
_attribute_types.update({key: 'integer' for key in (
    'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens',
    'nagent.agent.iteration', 'nagent.agent.message_count',
    'nagent.agent.inference_calls', 'nagent.agent.tool_calls',
    'nagent.model.message_count', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens', 'nagent.context.before_tokens', 'nagent.context.after_tokens',
)})
_attribute_types.update({key: 'boolean' for key in (
    'gen_ai.request.stream', 'nagent.agent.usage_known', 'nagent.model.usage_known',
    'nagent.tool.executed', 'nagent.metric.overflow',
)})
_attribute_types.update({f'nagent.{spec.object}.duration_ms': 'number' for spec in OPERATIONS.values()})
_attribute_types.update({f'nagent.content.{field}_{suffix}': kind
    for field in CONTENT_FIELDS for suffix, kind in (
        ('original_bytes', 'integer'), ('observed_bytes', 'integer'),
        ('truncated', 'boolean'), ('incomplete', 'boolean'))})
_attribute_types['gen_ai.response.finish_reasons'] = 'string_array'
ATTRIBUTE_TYPES = MappingProxyType(_attribute_types)


def _valid_attribute_value(key, value):
    kind = ATTRIBUTE_TYPES.get(key)
    if kind == 'string':
        return isinstance(value, str)
    if kind == 'integer':
        return type(value) is int and value >= 0
    if kind == 'boolean':
        return type(value) is bool
    if kind == 'number':
        return (type(value) is int and value >= 0
                or type(value) is float and math.isfinite(value) and value >= 0)
    if kind == 'string_array':
        return isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value)
    return False


def _typed_attributes(attributes):
    return {key: value for key, value in attributes.items() if _valid_attribute_value(key, value)}


def resolve_operation(name):
    if isinstance(name, OperationSpec):
        return name if name in OPERATIONS.values() else None
    if not isinstance(name, str):
        return None
    return OPERATIONS.get(name.split(' ', 1)[0])


def completion_event(operation):
    spec = resolve_operation(operation)
    return spec.event_name if spec else None


def normalize_outcome(value, *, cancelled=False, timed_out=False):
    if cancelled:
        return 'cancelled'
    if timed_out:
        return 'timed_out'
    return {'success': 'success', 'succeeded': 'success', 'completed': 'success', 'compressed': 'success', 'failure': 'failure', 'error': 'failure', 'failed': 'failure', 'crashed': 'failure', 'aborted': 'failure', 'denied': 'denied', 'cancelled': 'cancelled', 'timed_out': 'timed_out', 'skipped': 'skipped', 'deferred': 'deferred', 'waiting_approval': 'deferred', 'blocked': 'deferred'}.get(value, 'unknown') if isinstance(value, str) else 'unknown'


def project_execution_attributes(snapshot):
    if snapshot.get('_event') == 'nagent.budget.denied':
        operation = project_execution_attributes({key: value for key, value in snapshot.items() if key != '_event'})
        return _typed_attributes({**{key: value for key, value in operation.items() if key in SHARED_ATTRIBUTES},
                                  'nagent.budget.kind': snapshot.get('nagent.budget.kind')})
    spec = resolve_operation(snapshot.get('operation'))
    if spec is None:
        return {'nagent.observation.error': 'unknown_object'}
    obj = spec.object
    result = {key: value for key, value in snapshot.items() if key in spec.allowed_attributes and value is not None}
    outcome_key = f'nagent.{obj}.outcome'
    if outcome_key in result and isinstance(result[outcome_key], str):
        result[outcome_key] = normalize_outcome(result[outcome_key])
    for key, target in _standard.items():
        if key == 'provider' and obj != 'model':
            continue
        if target in spec.allowed_attributes and snapshot.get(key) is not None:
            result[target] = ([snapshot[key]] if key == 'finish_reason' and isinstance(snapshot[key], str) else snapshot[key])
    if spec.standard_operation:
        result['gen_ai.operation.name'] = spec.standard_operation
    else:
        result[f'nagent.{obj}.operation'] = spec.action
    for field in ('outcome', 'summary', 'duration_ms', *OBJECT_ATTRIBUTES.get(obj, ())):
        if snapshot.get(field) is not None:
            result[f'nagent.{obj}.{field}'] = normalize_outcome(snapshot[field]) if field == 'outcome' and isinstance(snapshot[field], str) else snapshot[field]
    if obj == 'memory' and isinstance(snapshot.get('operation'), str) and ' ' in snapshot['operation']:
        provider = snapshot['operation'].split(' ', 1)[1]
        result['nagent.memory.provider'] = provider if provider in {'builtin', 'multi-project', 'mem0', 'honcho', 'holographic', 'other'} else 'other'
    for field in ('agent_run_id', 'agent_source', 'iteration'):
        if snapshot.get(field) is not None:
            result[{'agent_run_id': 'nagent.agent.run_id', 'agent_source': 'nagent.agent.source', 'iteration': 'nagent.agent.iteration'}[field]] = snapshot[field]
    if obj == 'request':
        channel = {'cli.request': 'cli', 'acp.request': 'acp', 'feishu.event': 'feishu'}.get(snapshot.get('operation'))
        if channel:
            result['nagent.request.channel'] = channel
    return _typed_attributes(result)
