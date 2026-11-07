"""Standard-library generation of the opt-in, private Langfuse Collector fork."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = 'http://n-langfuse-web:3000/api/public/otel/v1/traces'
IMAGE = 'n-agent-langfuse-collector:0.137.0'
CONTENT = ('input_messages', 'output_messages', 'user_message', 'assistant_message', 'tool_arguments', 'tool_result', 'system_prompt', 'tool_definitions', 'error', 'retrieval_query', 'retrieval_results', 'judge_verdict', 'judge_reason')
SUFFIXES = ('original_bytes', 'truncated', 'incomplete', 'observed_bytes')
OBJECTS = ('agent', 'model', 'tool', 'context', 'task', 'delegation', 'schedule', 'memory', 'skill', 'curator', 'gateway', 'request', 'http', 'budget', 'policy', 'observation', 'knowledge')
IDENTITY = ('gen_ai.agent.name', 'nagent.agent.run_id', 'nagent.agent.source', 'nagent.agent.iteration', 'nagent.request.id', 'nagent.request.channel', 'nagent.task.id', 'nagent.task.run_id', 'nagent.task.state', 'nagent.delegation.id', 'nagent.delegation.member_id', 'nagent.delegation.role', 'nagent.schedule.task_id', 'nagent.schedule.claim_id', 'nagent.schedule.delivery_status')
FACTS = ('error.type', 'nagent.tool.executed', 'nagent.tool.policy_outcome', 'nagent.tool.risk_level', 'gen_ai.tool.name', 'gen_ai.tool.call.id', 'gen_ai.tool.type', 'gen_ai.provider.name', 'gen_ai.response.finish_reasons', 'gen_ai.request.stream', 'nagent.model.usage_known', 'nagent.model.message_count', 'nagent.agent.end_reason', 'nagent.agent.message_count', 'nagent.agent.inference_calls', 'nagent.agent.tool_calls', 'nagent.agent.usage_known', 'nagent.context.before_tokens', 'nagent.context.after_tokens', 'nagent.memory.provider', 'nagent.task.achieved', 'nagent.task.judge_attempt', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens', 'nagent.model.cost_status', 'nagent.model.pricing_version', *(f'nagent.{obj}.{field}' for obj in OBJECTS for field in ('operation', 'outcome')))
PARAMETERS = ('temperature', 'top_p', 'top_k', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'frequency_penalty', 'presence_penalty')
MODEL = ('gen_ai.request.model', 'gen_ai.response.model', 'nagent.model.completion_start_time', 'nagent.model.cost_usd', *(f'nagent.model.parameter.{key}' for key in PARAMETERS))
SPAN_KEYS = tuple(dict.fromkeys(('gen_ai.operation.name', 'gen_ai.conversation.id', *IDENTITY, *FACTS, *MODEL, *(f'nagent.content.{field}{suffix}' for field in CONTENT for suffix in ('', *(f'_{x}' for x in SUFFIXES))))))
BOOL_KEYS = ('nagent.tool.executed', 'gen_ai.request.stream', 'nagent.model.usage_known', 'nagent.agent.usage_known', 'nagent.task.achieved')
COUNT_KEYS = ('nagent.agent.iteration', 'nagent.agent.message_count', 'nagent.agent.inference_calls', 'nagent.agent.tool_calls', 'nagent.model.message_count', 'nagent.context.before_tokens', 'nagent.context.after_tokens', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'nagent.model.cache_read_tokens', 'nagent.model.cache_write_tokens')
CUSTOM_TYPES = {'task.run': 'chain', 'delegation.run_member': 'chain', 'schedule.run': 'chain', 'gateway.handle_message': 'chain', 'skill.review': 'chain', 'curator.run': 'chain', 'knowledge.search': 'retriever', 'memory.prefetch': 'retriever', 'memory.search': 'retriever', 'task.evaluate': 'evaluator'}
# This is a fixed diagnostic enum, never a pass-through of arbitrary error text.
ERROR_TYPES = ('TimeoutError', 'ConnectionError', 'CancelledError', 'ValueError', 'ValidationError', 'RateLimitError', 'BadRequestError', 'AuthenticationError', 'PermissionDeniedError', 'NotFoundError', 'PolicyDenied', 'APIError', 'APIConnectionError', 'APITimeoutError', 'APIStatusError', 'APIResponseValidationError', 'InternalServerError', 'ConflictError', 'UnprocessableEntityError', 'OverloadedError', 'RequestTooLargeError', 'tool_error', 'denied', 'cancelled', 'other')


def value_guard(key):
    """OTTL's typed guards reject malformed source attributes before projection."""
    value = f'attributes["{key}"]'
    if key in BOOL_KEYS or key.endswith(('_truncated', '_incomplete')):
        return f'IsBool({value})'
    if key == 'error.type':
        return 'IsString(' + value + ') and (' + ' or '.join(f'{value} == {json.dumps(error)}' for error in ERROR_TYPES) + ')'
    if key == 'nagent.task.judge_attempt':
        return f'IsInt({value}) and {value} > 0'
    if key in COUNT_KEYS or key.endswith(('_original_bytes', '_observed_bytes')):
        return f'IsInt({value}) and {value} >= 0'
    if key.startswith('nagent.model.parameter.') or key == 'nagent.model.cost_usd':
        # A finite double equals itself and lies inside IEEE's finite range.
        finite = f'((IsInt({value})) or (IsDouble({value}) and {value} == {value} and {value} <= 1.7976931348623157e308 and {value} >= -1.7976931348623157e308))'
        return finite + (f' and {value} >= 0' if key.endswith('cost_usd') else '')
    if key == 'gen_ai.response.finish_reasons':
        # OTel attributes are homogeneous slices; the JSON representation also
        # excludes numeric, boolean and nested list values at this boundary.
        pattern = r'^\[("([^"\\]|\\.)*"(, ?"([^"\\]|\\.)*")*)?\]$'
        return f'IsList({value}) and IsMatch(String({value}), {json.dumps(pattern)})'
    return f'IsString({value})'


def validate_endpoint(endpoint):
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        valid = parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None and parsed.password is None and '?' not in endpoint and '#' not in endpoint and not any(c.isspace() for c in endpoint)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Langfuse endpoint must be an http(s) URL without credentials, query or fragment')
    return endpoint


def settings(directory, root):
    """Read only our literal settings from shared dotenv; leave base Compose semantics alone."""
    keys = ('N_OBSERVABILITY_LANGFUSE_ENABLED', 'LANGFUSE_ENV_FILE', 'LANGFUSE_OTEL_ENDPOINT')
    values = {}
    path = Path(directory) / '.env'
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key in keys:
                if key in values:
                    raise ValueError('Duplicate Langfuse setting')
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    values.update({key: os.environ[key] for key in keys if key in os.environ})
    enabled = values.get(keys[0], 'false').lower()
    if enabled not in ('true', 'false', '1', '0'):
        raise ValueError('N_OBSERVABILITY_LANGFUSE_ENABLED must be true or false')
    path = Path(values.get(keys[1], 'docker/langfuse/.env'))
    path = path if path.is_absolute() else Path(root) / path
    endpoint = values.get(keys[2], DEFAULT_ENDPOINT)
    if enabled in ('true', '1'):
        validate_endpoint(endpoint)
    return enabled in ('true', '1'), path, endpoint


def span_statements():
    agent = 'resource.attributes["service.name"] == "n-agent"'
    chat = f'{agent} and attributes["gen_ai.operation.name"] == "chat"'
    statements = [f'keep_keys(attributes, {json.dumps(SPAN_KEYS)})', 'set(status.message, "")']
    # The purge is first. Guards run before comparisons or downstream copying.
    for key in SPAN_KEYS:
        statements.append(f'delete_key(attributes, "{key}") where attributes["{key}"] != nil and not ({value_guard(key)})')
    statements += ['set(cache["known_agent_root"], false)', 'set(cache["known_agent_root"], true) where name == "invoke_agent n-agent"', 'set(name, "span")', 'set(attributes["langfuse.observation.type"], "span")', 'set(cache["entry"], false)']
    for operation, kind in [('invoke_agent', 'agent'), ('chat', 'generation'), ('execute_tool', 'tool')]:
        condition = f'{agent} and attributes["gen_ai.operation.name"] == "{operation}"'
        statements += [f'set(name, "{operation}") where {condition}', f'set(attributes["langfuse.observation.type"], "{kind}") where {condition}']
    statements.append(f'set(name, "invoke_agent n-agent") where {agent} and attributes["gen_ai.operation.name"] == "invoke_agent" and cache["known_agent_root"] == true')
    # Custom operations have their own canonical attribute, never gen_ai.operation.name.
    custom_names = dict(CUSTOM_TYPES)
    for operation in ('context.compress', 'task.submit', 'request.handle', 'http.handle', 'memory.prepare_prompt', 'memory.pre_compress', 'memory.sync', 'memory.execute_tool', 'memory.probe'):
        custom_names[operation] = 'span'
    for operation, kind in custom_names.items():
        obj, action = operation.split('.')
        condition = f'{agent} and attributes["nagent.{obj}.operation"] == "{action}"'
        statements += [f'set(name, "{operation}") where {condition}', f'set(attributes["langfuse.observation.type"], "{kind}") where {condition}']
        if kind == 'chain':
            statements += [f'set(cache["entry"], true) where {condition}']
    statements += ['set(attributes["langfuse.trace.name"], name) where parent_span_id == SpanID(0x0000000000000000)', 'set(attributes["langfuse.session.id"], attributes["gen_ai.conversation.id"]) where IsString(attributes["gen_ai.conversation.id"])']
    for source, target in [('service.version', 'langfuse.release'), ('deployment.environment.name', 'langfuse.environment')]:
        value = f'resource.attributes["{source}"]'
        pattern = r'^[a-zA-Z0-9][a-zA-Z0-9._+/-]{0,199}$' if source == 'service.version' else r'^[a-z0-9][a-z0-9_-]{0,39}$'
        condition = f'{agent} and IsString({value}) and IsMatch({value}, {json.dumps(pattern)})'
        if source == 'deployment.environment.name':
            condition += f' and not IsMatch({value}, "^langfuse")'
        statements.append(f'set(attributes["{target}"], {value}) where {condition}')
    statements += ['set(cache["metadata"], {})']
    metadata_keys = tuple(dict.fromkeys((*IDENTITY, *FACTS)))
    for key in metadata_keys:
        statements.append(f'set(cache["metadata"]["{key}"], attributes["{key}"]) where {agent} and attributes["{key}"] != nil')
    # Each replacement assigns a fresh envelope, so completeness cannot leak
    # from a lower-priority source. Metadata bodies are independent envelopes.
    def envelope(field, target, condition):
        source = f'nagent.content.{field}'
        condition = f'{agent} and {condition} and IsString(attributes["{source}"])'
        result = [f'set({target}, {{"source": "{field}"}}) where {condition}']
        for suffix in SUFFIXES:
            key = f'{source}_{suffix}'
            result.append(f'set({target}["{suffix}"], attributes["{key}"]) where {condition} and attributes["{key}"] != nil')
        return condition, result

    for direction, fields in [('input', ('user_message', 'input_messages', 'tool_arguments', 'retrieval_query')), ('output', ('assistant_message', 'output_messages', 'tool_result', 'retrieval_results', 'judge_verdict'))]:
        for field in fields:
            condition = 'true'
            if field.startswith('tool_'):
                condition = 'attributes["gen_ai.operation.name"] == "execute_tool"'
            elif field.startswith('retrieval_'):
                condition = 'attributes["nagent.knowledge.operation"] == "search"'
            elif field.startswith('judge_'):
                condition = 'attributes["nagent.task.operation"] == "evaluate"'
            target = f'cache["metadata"]["{direction}"]'
            condition, body = envelope(field, target, condition)
            statements += [f'set(attributes["langfuse.observation.{direction}"], attributes["nagent.content.{field}"]) where {condition}', *body]
    for field in ('system_prompt', 'tool_definitions', 'error', 'judge_reason'):
        target = f'cache["metadata"]["{field}"]'
        guard = 'attributes["nagent.task.operation"] == "evaluate"' if field == 'judge_reason' else 'true'
        condition, body = envelope(field, target, guard)
        statements += [*body, f'set({target}["content"], attributes["nagent.content.{field}"]) where {condition}']
    for direction in ('input', 'output'):
        statements.append(f'set(attributes["langfuse.trace.{direction}"], attributes["langfuse.observation.{direction}"]) where {agent} and cache["entry"] == true and parent_span_id == SpanID(0x0000000000000000) and attributes["langfuse.observation.{direction}"] != nil')
    statements += ['set(cache["usage"], {})', 'set(cache["parameters"], {})']
    for bucket, source in [('input', 'gen_ai.usage.input_tokens'), ('output', 'gen_ai.usage.output_tokens'), ('cache_read', 'nagent.model.cache_read_tokens'), ('cache_write', 'nagent.model.cache_write_tokens')]:
        statements.append(f'set(cache["usage"]["{bucket}"], attributes["{source}"]) where {chat} and IsInt(attributes["{source}"]) and attributes["{source}"] >= 0')
    statements += [f'set(attributes["langfuse.observation.usage_details"], String(cache["usage"])) where {chat} and Len(cache["usage"]) > 0']
    for source in ('gen_ai.request.model', 'gen_ai.response.model'):
        statements.append(f'set(attributes["langfuse.observation.model.name"], attributes["{source}"]) where {chat} and IsString(attributes["{source}"])')
    for parameter in PARAMETERS:
        source = f'nagent.model.parameter.{parameter}'
        statements.append(f'set(cache["parameters"]["{parameter}"], attributes["{source}"]) where {chat} and attributes["{source}"] != nil')
    statements.append(f'set(attributes["langfuse.observation.model.parameters"], String(cache["parameters"])) where {chat} and Len(cache["parameters"]) > 0')
    # SDK-produced UTC RFC3339; validate its structure before publishing.
    timestamp = 'attributes["nagent.model.completion_start_time"]'
    leap_year = r'([0-9]{2}(0[48]|[2468][048]|[13579][26])|(0[48]|[2468][048]|[13579][26])00)'
    date = rf'([0-9]{{4}}-((01|03|05|07|08|10|12)-(0[1-9]|[12][0-9]|3[01])|(04|06|09|11)-(0[1-9]|[12][0-9]|30)|02-(0[1-9]|1[0-9]|2[0-8]))|{leap_year}-02-29)'
    pattern = rf'^{date}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$'
    statements.append(f'set(attributes["langfuse.observation.completion_start_time"], {timestamp}) where {chat} and IsString({timestamp}) and IsMatch({timestamp}, {json.dumps(pattern)})')
    cost = 'attributes["nagent.model.cost_usd"]'
    statements.append(f'set(attributes["langfuse.observation.cost_details"], String({{"total": {cost}}})) where {chat} and {cost} != nil and attributes["nagent.model.cost_status"] == "estimated" and IsString(attributes["nagent.model.pricing_version"]) and attributes["nagent.model.pricing_version"] != ""')
    statements.append('set(attributes["langfuse.observation.level"], "ERROR") where status.code == STATUS_CODE_ERROR')
    # Fallback only for execution failure/timeouts; outcome alone never sets level.
    for obj in OBJECTS:
        for outcome in ('failure', 'timed_out'):
            statements.append(f'set(attributes["langfuse.observation.status_message"], "{outcome}") where status.code == STATUS_CODE_ERROR and attributes["nagent.{obj}.outcome"] == "{outcome}"')
    for error in ERROR_TYPES:
        statements.append(f'set(attributes["langfuse.observation.status_message"], "{error}") where status.code == STATUS_CODE_ERROR and attributes["error.type"] == "{error}"')
    envelope_keys = ('input', 'output', 'system_prompt', 'tool_definitions', 'error', 'judge_reason')
    for key in (*metadata_keys, *envelope_keys):
        source = f'cache["metadata"]["{key}"]'
        value = f'String({source})' if key in envelope_keys else source
        statements.append(f'set(attributes["langfuse.observation.metadata.{key}"], {value}) where {source} != nil')
    final = ('langfuse.observation.type', 'langfuse.trace.name', 'langfuse.session.id', 'langfuse.release', 'langfuse.environment', 'langfuse.trace.input', 'langfuse.trace.output', 'langfuse.observation.input', 'langfuse.observation.output', *(f'langfuse.observation.metadata.{key}' for key in (*metadata_keys, *envelope_keys)), 'langfuse.observation.usage_details', 'langfuse.observation.model.name', 'langfuse.observation.model.parameters', 'langfuse.observation.completion_start_time', 'langfuse.observation.cost_details', 'langfuse.observation.level', 'langfuse.observation.status_message')
    statements += [f'keep_keys(attributes, {json.dumps(final)})']
    return statements


def build_config(public_key, secret_key, endpoint=DEFAULT_ENDPOINT):
    validate_endpoint(endpoint)
    auth = base64.b64encode(f'{public_key}:{secret_key}'.encode()).decode()
    event_keys = ('error.type', 'nagent.tool.executed', 'nagent.tool.policy_outcome', *('nagent.' + obj + '.outcome' for obj in OBJECTS))
    safe_events = [f'nagent.{obj}.completed' for obj in OBJECTS]
    return {'processors': {
        'filter/langfuse': {'error_mode': 'propagate', 'traces': {'span': ['resource.attributes["service.name"] != "n-agent" and resource.attributes["service.name"] != "n-kb"']}},
        'transform/langfuse': {'error_mode': 'propagate', 'trace_statements': [
            {'context': 'scope', 'statements': ['set(name, "")', 'set(version, "")', 'keep_keys(attributes, [])']},
            {'context': 'span', 'statements': span_statements()},
            {'context': 'resource', 'statements': ['keep_keys(attributes, ["service.name"])']},
            {'context': 'spanevent', 'statements': [f'keep_keys(attributes, {json.dumps(event_keys)})', 'set(name, "event") where not IsMatch(name, "^(' + '|'.join(x.replace('.', '[.]') for x in safe_events) + ')$")']},
        ]},
        'linksanitize/langfuse': {},
        'batch/langfuse': {'timeout': '1s', 'send_batch_size': 1, 'send_batch_max_size': 1},
    }, 'exporters': {'otlphttp/langfuse': {
        'traces_endpoint': endpoint, 'headers': {'Authorization': 'Basic ' + auth, 'x-langfuse-ingestion-version': '4'}, 'timeout': '10s',
        'sending_queue': dict(enabled=True, sizer='bytes', queue_size=16777216, block_on_overflow=False, wait_for_result=False, num_consumers=2),
        'retry_on_failure': dict(initial_interval='1s', max_interval='5s', max_elapsed_time='60s'),
    }}, 'service': {'pipelines': {'traces/langfuse': {'receivers': ['otlp'], 'processors': ['memory_limiter', 'filter/langfuse', 'transform/langfuse', 'linksanitize/langfuse', 'batch/langfuse'], 'exporters': ['otlphttp/langfuse']}}}}


def publish_json(directory, value):
    """Atomic content-addressed publication; never replace a mounted inode."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    path = directory / (hashlib.sha256(data).hexdigest() + '.json')
    if path.exists():
        if path.read_bytes() != data or path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('Invalid existing generated configuration')
        return path
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('Generated configuration hash collision')
    finally:
        temporary.unlink()
    return path


def load_credentials(root, path):
    spec = importlib.util.spec_from_file_location('langfuse_stack_credentials', Path(root) / 'docker/langfuse/manage-stack.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = module.read_env(path)
    module.validate_env(values)
    if Path(path).stat().st_mode & 0o777 != 0o600:
        raise ValueError('Langfuse credentials must have permissions 0600')
    return module, values


def prepare(directory, root):
    enabled, env_file, endpoint = settings(directory, root)
    if not enabled:
        return None
    module, values = load_credentials(root, env_file)
    folder = Path(root) / 'locals/harness_tmp/langfuse-collector'
    config = publish_json(folder, build_config(values['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'], values['LANGFUSE_INIT_PROJECT_SECRET_KEY'], endpoint))
    command = ['--config=/etc/otelcol-contrib/config.yaml', '--config=/etc/otelcol-contrib/langfuse.json']
    override = publish_json(folder, {'services': {'otel-collector': {
        # The private 0600 bind is owned by the invoking host account.
        'user': f'{os.getuid()}:{os.getgid()}',
        'image': IMAGE, 'build': {'context': str(Path(directory) / 'langfuse-collector')},
        'command': command,
        'volumes': [{'type': 'bind', 'source': str(config), 'target': '/etc/otelcol-contrib/langfuse.json', 'read_only': True}],
        'healthcheck': {'test': ['CMD', '/otelcol-contrib', 'validate', *command]},
    }}})
    return {'override': override, 'config': config, 'module': module, 'credentials': values}
