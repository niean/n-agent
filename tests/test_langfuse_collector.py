"""Collector fork configuration and host lifecycle contracts (no real Docker)."""
import importlib.util
import json
from pathlib import Path
import os
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'docker/observability'


def load(name='langfuse-config'):
    path = DIRECTORY / (name + '.py')
    assert path.is_file(), f'missing {name} implementation'
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_pipeline_is_independent_and_bounded():
    m = load()
    doc = m.build_config('public', 'secret')
    assert set(doc['service']['pipelines']) == {'traces/langfuse', 'traces/langfuse-kb'}
    pipe = doc['service']['pipelines']['traces/langfuse']
    assert pipe['processors'] == ['memory_limiter', 'filter/langfuse', 'transform/langfuse', 'linksanitize/langfuse', 'batch/langfuse']
    exporter = doc['exporters']['otlphttp/langfuse']
    assert exporter['traces_endpoint'] == 'http://n-langfuse-web:3000/api/public/otel/v1/traces'
    assert exporter['sending_queue'] == dict(enabled=True, sizer='bytes', queue_size=16777216, block_on_overflow=False, wait_for_result=False, num_consumers=2)
    assert exporter['timeout'] == '10s'
    assert doc['processors']['batch/langfuse']['send_batch_max_size'] == 1


def test_projection_is_exact_with_explicit_types_and_content_priority():
    m = load()
    doc = m.build_config('p', 's')
    text = json.dumps(doc)
    assert 'n-agent' in text and 'n-kb' in text
    for operation, kind in [('invoke_agent', 'agent'), ('chat', 'generation'), ('execute_tool', 'tool')]:
        assert any(operation in s and '"' + kind + '"' in s for s in m.span_statements())
    assert 'langfuse.observation.type' in text
    assert 'langfuse.session.id' in text and 'langfuse.trace.name' in text
    statements = m.span_statements()
    assert statements.index(next(s for s in statements if 'keep_keys(attributes,' in s)) < statements.index(next(s for s in statements if 'langfuse.observation.type' in s))
    for field in ('tool_arguments', 'tool_result', 'input_messages', 'output_messages', 'user_message', 'assistant_message'):
        assert any('nagent.content.' + field in s for s in statements)
    for suffix in ('original_bytes', 'truncated', 'incomplete', 'observed_bytes'):
        assert suffix in text
    assert 'system_prompt' in text and 'tool_definitions' in text
    assert 'cache_read' in text and 'cache_write' in text
    assert 'langfuse.observation.cost_details' in text
    assert 'keep_matching_keys' not in text
    assert 'set(status.message, "")' in statements


@pytest.mark.parametrize('endpoint', ['ftp://host/path', 'http://user:pass@host', 'http://host/path?x=1', 'http://host/#x', 'http://', 'http://host:bad'])
def test_endpoint_rejects_unsafe_addresses(endpoint):
    with pytest.raises(ValueError):
        load().build_config('p', 's', endpoint)


def test_artifacts_are_immutable_private_and_content_addressed(tmp_path):
    m = load()
    a = m.publish_json(tmp_path / 'generated', {'secret': 'first'})
    b = m.publish_json(tmp_path / 'generated', {'secret': 'second'})
    assert a != b and json.loads(a.read_text()) == {'secret': 'first'}
    assert m.publish_json(tmp_path / 'generated', {'secret': 'first'}) == a
    assert a.stat().st_mode & 0o777 == 0o600
    assert a.parent.stat().st_mode & 0o777 == 0o700


def test_disabled_never_reads_credentials_or_appends_override(tmp_path, monkeypatch):
    manager = load('manage-stack')
    monkeypatch.setenv('N_OBSERVABILITY_LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENV_FILE', '/missing/must-not-read')
    stack = manager.Stack(tmp_path, progress=False)
    calls = []
    stack.run = lambda *args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, '', '')
    stack.compose('config', '--quiet')
    assert calls[0].count('--file') == 1


def test_enable_invalid_configuration_preserves_base_and_never_calls_docker(tmp_path, monkeypatch):
    manager = load('manage-stack')
    base = tmp_path / 'docker-compose.yml'
    base.write_text('unchanged')
    monkeypatch.setenv('N_OBSERVABILITY_LANGFUSE_ENABLED', 'true')
    monkeypatch.setenv('LANGFUSE_ENV_FILE', '/missing/credentials')
    stack = manager.Stack(tmp_path, progress=False)
    calls = []
    stack.run = lambda *args, **kwargs: calls.append(args)
    with pytest.raises((ValueError, OSError, manager.StackError)):
        stack.compose('config', '--quiet')
    assert base.read_text() == 'unchanged' and calls == []


def test_shared_env_process_priority_and_repo_relative_path(tmp_path, monkeypatch):
    m = load()
    (tmp_path / '.env').write_text('N_OBSERVABILITY_LANGFUSE_ENABLED=true\nLANGFUSE_ENV_FILE=custom.env\nOTHER=$(never-run)\n')
    monkeypatch.delenv('N_OBSERVABILITY_LANGFUSE_ENABLED', raising=False)
    monkeypatch.delenv('LANGFUSE_ENV_FILE', raising=False)
    assert m.settings(tmp_path, ROOT) == (True, ROOT / 'custom.env', m.DEFAULT_ENDPOINT, m.DEFAULT_ENDPOINT)
    monkeypatch.setenv('N_OBSERVABILITY_LANGFUSE_ENABLED', 'false')
    assert m.settings(tmp_path, ROOT)[0] is False

@pytest.mark.parametrize('args', [('config',), ('config', '--format', 'json'), ('config', '--quiet', '--format', 'json')])
def test_enabled_diagnostic_rejects_config_output_before_docker(monkeypatch, args):
    m = load('manage-stack')
    monkeypatch.setattr(m.Stack, 'langfuse', lambda self: {'override': 'private'})
    calls = []
    monkeypatch.setattr(m.Stack, 'compose', lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess(a, 0, '', ''))
    monkeypatch.setattr(m.sys, 'argv', ['manage-stack.py', 'compose', *args])
    assert m.main() == 1
    assert not calls

def test_tool_content_falls_back_to_messages_before_specific_fields():
    statements = load().span_statements()
    for direction, generic, specific in [('input', 'input_messages', 'tool_arguments'), ('output', 'output_messages', 'tool_result')]:
        fallback = next(s for s in statements if f'set(attributes["langfuse.observation.{direction}"]' in s and f'nagent.content.{generic}' in s)
        preferred = next(s for s in statements if f'set(attributes["langfuse.observation.{direction}"]' in s and f'nagent.content.{specific}' in s)
        assert '!= "execute_tool"' not in fallback
        assert '== "execute_tool"' in preferred
        assert statements.index(fallback) < statements.index(preferred)


def test_judge_verdict_is_evaluation_output_not_input():
    statements=load().span_statements()
    assert not any('set(attributes["langfuse.observation.input"]' in s and 'nagent.content.judge_verdict' in s for s in statements)
    assert any('set(attributes["langfuse.observation.output"]' in s and 'nagent.content.judge_verdict' in s for s in statements)
    assert any('cache["metadata"]["judge_reason"]["content"]' in s for s in statements)

def test_enabled_override_can_read_private_config_without_changing_secret_mode(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, 'settings', lambda directory, root: (True, tmp_path / '.env', m.DEFAULT_ENDPOINT, m.DEFAULT_ENDPOINT))
    monkeypatch.setattr(m, 'load_credentials', lambda root, path: (None, {'LANGFUSE_INIT_PROJECT_PUBLIC_KEY': 'p', 'LANGFUSE_INIT_PROJECT_SECRET_KEY': 's'}))
    result = m.prepare(tmp_path, tmp_path)
    service = json.loads(result['override'].read_text())['services']['otel-collector']
    assert service['user'] == f'{os.getuid()}:{os.getgid()}'
    assert result['config'].stat().st_mode & 0o777 == 0o600
    assert all(mount['read_only'] for mount in service['volumes'])
    assert set(service) == {'image', 'build', 'user', 'command', 'volumes', 'healthcheck'}

def test_enabled_migration_refuses_before_reading_credentials(tmp_path, monkeypatch):
    m = load('manage-stack')
    monkeypatch.setenv('N_OBSERVABILITY_LANGFUSE_ENABLED', 'true')
    monkeypatch.setenv('LANGFUSE_ENV_FILE', '/missing/never-read')
    stack = m.Stack(tmp_path, progress=False)
    with pytest.raises(m.StackError, match='does not support migrate'):
        stack.migrate('old', tmp_path, tmp_path / 'old.yml')


@pytest.mark.parametrize('operation,kind', [
    ('task.run', 'chain'), ('delegation.run_member', 'chain'),
    ('schedule.run', 'chain'), ('gateway.handle_message', 'chain'),
    ('skill.review', 'chain'), ('curator.run', 'chain'),
    ('knowledge.search', 'retriever'), ('memory.prefetch', 'retriever'),
    ('memory.search', 'retriever'), ('task.evaluate', 'evaluator'),
])
def test_custom_types_use_exact_service_and_canonical_operation(operation, kind):
    obj, action = operation.split('.')
    statements = load().span_statements()
    typed = next(s for s in statements if f'set(attributes["langfuse.observation.type"], "{kind}")' in s
                 and f'attributes["nagent.{obj}.operation"] == "{action}"' in s)
    assert 'resource.attributes["service.name"] == "n-agent"' in typed
    assert 'gen_ai.operation.name' not in typed
    assert any(f'set(name, "{operation}")' in s and f'nagent.{obj}.operation' in s for s in statements)


def test_exact_typed_metadata_and_content_envelopes():
    m = load()
    text = '\n'.join(m.span_statements())
    expected = ('gen_ai.provider.name', 'gen_ai.response.finish_reasons', 'gen_ai.request.stream',
                'nagent.model.usage_known', 'nagent.agent.end_reason', 'nagent.agent.message_count',
                'nagent.agent.inference_calls', 'nagent.agent.tool_calls', 'nagent.agent.usage_known',
                'nagent.context.before_tokens', 'nagent.context.after_tokens', 'nagent.memory.provider',
                'nagent.task.achieved', 'nagent.task.judge_attempt')
    for key in expected:
        assert f'langfuse.observation.metadata.{key}' in text
    assert 'IsBool(attributes["nagent.task.achieved"])' in text
    assert 'IsInt(attributes["nagent.task.judge_attempt"])' in text
    assert 'attributes["nagent.task.judge_attempt"] > 0' in text
    for field in ('system_prompt', 'tool_definitions', 'error'):
        assert f'cache["metadata"]["{field}"]' in text
        assert f'nagent.content.{field}_incomplete' in text
    for field, obj, action in [('retrieval_query', 'knowledge', 'search'), ('retrieval_results', 'knowledge', 'search'),
                                ('judge_verdict', 'task', 'evaluate'), ('judge_reason', 'task', 'evaluate')]:
        target = 'set(cache["metadata"]["judge_reason"]["content"]' if field == 'judge_reason' else 'set(attributes["langfuse.observation.'
        mapped = next(s for s in m.span_statements() if target in s and f'nagent.content.{field}"]' in s)
        assert f'attributes["nagent.{obj}.operation"] == "{action}"' in mapped
        assert f'IsString(attributes["nagent.content.{field}"])' in mapped


def test_model_error_resource_and_trace_guards():
    m = load()
    statements = m.span_statements()
    text = '\n'.join(statements)
    for target in ('model.parameters', 'completion_start_time', 'cost_details', 'level', 'status_message'):
        assert f'langfuse.observation.{target}' in text
    assert 'STATUS_CODE_ERROR' in text
    assert 'nagent.model.cost_status"] == "estimated"' in text
    assert 'IsDouble(attributes["nagent.model.cost_usd"])' in text
    assert 'nagent.model.pricing_version"] != ""' in text
    assert 'nagent.model.parameter.presence_penalty' in text
    assert 'langfuse.environment' in text and 'langfuse.release' in text
    for s in statements:
        if 'set(attributes["langfuse.trace.input"]' in s or 'set(attributes["langfuse.trace.output"]' in s:
            assert 'parent_span_id == SpanID(0x0000000000000000)' in s
            assert 'cache["entry"] == true' in s
    assert 'set(attributes["langfuse.trace.input"]' in text
    contexts = m.build_config('p', 's')['processors']['transform/langfuse']['trace_statements']
    assert [c['context'] for c in contexts].index('span') < [c['context'] for c in contexts].index('resource')
    assert statements[0].startswith('keep_keys(attributes,')
    assert 'langfuse.' not in statements[0]
    assert all('set(attributes["langfuse.observation.type"], "event")' not in s for s in statements)


def test_resource_values_and_diagnostics_are_bounded_and_root_name_stays_stable():
    statements = load().span_statements()
    release = next(s for s in statements if 'set(attributes["langfuse.release"]' in s)
    environment = next(s for s in statements if 'set(attributes["langfuse.environment"]' in s)
    assert 'IsMatch(' in release and '{0,199}' in release
    assert 'IsMatch(' in environment and '{0,39}' in environment and '^langfuse' in environment
    assert any('set(name, "invoke_agent n-agent")' in s and 'cache["known_agent_root"] == true' in s for s in statements)
    error_guard = next(s for s in statements if 'delete_key(attributes, "error.type")' in s)
    assert 'APIConnectionError' in error_guard and 'tool_error' in error_guard


@pytest.mark.parametrize('timestamp,valid', [
    ('2026-11-07T01:02:03.123Z', True), ('2024-02-29T01:02:03+08:00', True),
    ('2025-02-29T01:02:03Z', False), ('2026-04-31T01:02:03Z', False),
    ('2026-11-07T25:02:03Z', False), ('not-a-time', False),
])
def test_completion_timestamp_guard_validates_calendar(timestamp, valid):
    import re
    statement = next(s for s in load().span_statements() if 'set(attributes["langfuse.observation.completion_start_time"]' in s)
    pattern = json.loads(statement.rsplit(', ', 1)[1][:-1])
    assert bool(re.fullmatch(pattern, timestamp)) is valid


def kb_guard_accepts(guard, value):
    """Evaluate a generated KB guard against a Python value the way OTTL would type it."""
    import math
    import re
    if 'IsBool(' in guard:
        return isinstance(value, bool)
    if 'IsMatch(' in guard:
        pattern = json.loads(guard.rsplit(', ', 1)[1][:-1])
        return isinstance(value, str) and re.fullmatch(pattern, value) is not None
    if 'IsInt(' in guard or 'IsDouble(' in guard:
        # OTTL booleans pass neither IsInt nor IsDouble; NaN fails self-equality.
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value == value and value != math.inf and value != -math.inf and value >= 0)
    # Enum guards: IsString(...) and (value == "a" or value == "b" ...).
    allowed = [json.loads(item) for item in re.findall(r'== ("(?:[^"\\]|\\.)*")', guard)]
    return isinstance(value, str) and value in allowed


@pytest.mark.parametrize('key,value,expected', [
    ('nkb.index.document_id', 'a' * 129, False),  # 超长 ID 拒绝
    ('nkb.index.document_id', 'a.b:c-d/e', True),
    ('nkb.index.job_id', 'job-123_456:789', True),
    ('nkb.retrieval.duration_ms', 12.5, True),  # 浮点 duration 接受
    ('nkb.index.duration_ms', 0, True),
    ('nkb.index.duration_ms', True, False),  # bool duration 拒绝
    ('nkb.retrieval.duration_ms', float('nan'), False),
    ('nkb.retrieval.duration_ms', float('inf'), False),
    ('nkb.retrieval.duration_ms', -0.5, False),
    ('nkb.retrieval.outcome', 'exploded', False),  # 非枚举 outcome 拒绝
    ('nkb.retrieval.outcome', 'timed_out', True),
    ('nkb.index.operation', 'delete', False),
    ('nkb.index.operation', 'run', True),
    ('gen_ai.request.model', 'm' * 65, False),  # 超 64 字符 model 拒绝
    ('gen_ai.request.model', 'qwen3:0.6b', True),
    ('gen_ai.operation.name', 'chat', False),  # embeddings 之外拒绝
    ('gen_ai.operation.name', 'embeddings', True),
    ('gen_ai.provider.name', 'openai', False),
    ('gen_ai.provider.name', 'ollama', True),
    ('error.type', 'timeout', True),
    ('error.type', 'tool_error', False),
    ('nkb.model.usage_known', True, True),
    ('nkb.model.usage_known', 1, False),
    ('nkb.index.chunk_count', 3, True),
    ('nkb.index.chunk_count', True, False),
])
def test_kb_guards_reject_and_accept_real_values(key, value, expected):
    assert kb_guard_accepts(load().kb_value_guard(key), value) is expected


@pytest.mark.parametrize('direction,fields', [
    ('input', ('user_message', 'input_messages', 'tool_arguments')),
    ('output', ('assistant_message', 'output_messages', 'tool_result')),
])
def test_content_candidates_have_exact_ascending_priority(direction, fields):
    statements = load().span_statements()
    prefix = f'set(attributes["langfuse.observation.{direction}"], '
    # The ordinary source order is asserted independently in each direction.
    # A later candidate replaces both the selected body and its metadata.
    selected = [s for s in statements if s.startswith(prefix)
                and any(f'attributes["nagent.content.{field}"])' in s for field in fields)]
    assert len(selected) == 3
    assert [next(field for field in fields if f'nagent.content.{field}"]' in s)
            for s in selected] == list(fields)
    assert all('== "execute_tool"' not in s for s in selected[:2])
    assert 'attributes["gen_ai.operation.name"] == "execute_tool"' in selected[2]


@pytest.mark.parametrize('direction,field,operation_guard', [
    ('input', 'user_message', 'true'),
    ('input', 'input_messages', 'true'),
    ('input', 'tool_arguments', 'attributes["gen_ai.operation.name"] == "execute_tool"'),
    ('input', 'retrieval_query', 'attributes["nagent.knowledge.operation"] == "search"'),
    ('output', 'assistant_message', 'true'),
    ('output', 'output_messages', 'true'),
    ('output', 'tool_result', 'attributes["gen_ai.operation.name"] == "execute_tool"'),
    ('output', 'retrieval_results', 'attributes["nagent.knowledge.operation"] == "search"'),
    ('output', 'judge_verdict', 'attributes["nagent.task.operation"] == "evaluate"'),
])
def test_content_selection_replaces_envelope_before_copying_completeness(direction, field, operation_guard):
    statements = load().span_statements()
    source = f'nagent.content.{field}'
    body_prefix = f'set(attributes["langfuse.observation.{direction}"], attributes["{source}"]) where '
    body = next(s for s in statements if s.startswith(body_prefix))
    condition = (f'resource.attributes["service.name"] == "n-agent" and {operation_guard}'
                 f' and IsString(attributes["{source}"])')
    assert body == body_prefix + condition
    index = statements.index(body)
    target = f'cache["metadata"]["{direction}"]'
    # Exact fresh-map assignment guarantees missing suffixes cannot inherit
    # lower-priority values; setting only target["source"] would fail this.
    assert statements[index + 1] == f'set({target}, {{"source": "{field}"}}) where {condition}'
    expected = []
    for suffix in ('original_bytes', 'truncated', 'incomplete', 'observed_bytes'):
        key = f'{source}_{suffix}'
        expected.append(f'set({target}["{suffix}"], attributes["{key}"]) where {condition} and attributes["{key}"] != nil')
    assert statements[index + 2:index + 6] == expected


def test_kb_pipeline_and_projection_contract():
    m = load()
    config = m.build_config('pk-na', 'sk-na')
    pipelines = config['service']['pipelines']
    assert set(pipelines) == {'traces/langfuse', 'traces/langfuse-kb'}
    # n-agent 管线收窄：filter 只认 n-agent；KB 管线只认 n-kb
    assert config['processors']['filter/langfuse']['traces']['span'] == \
        ['resource.attributes["service.name"] != "n-agent"']
    assert config['processors']['filter/langfuse-kb']['traces']['span'] == \
        ['resource.attributes["service.name"] != "n-kb"']
    kb_exporter = config['exporters']['otlphttp/langfuse-kb']
    assert kb_exporter['traces_endpoint'] == m.DEFAULT_ENDPOINT
    # KB exporter 与主 exporter 使用同一组项目凭据（同项目共享），traces_endpoint 可独立配置
    agent_exporter = config['exporters']['otlphttp/langfuse']
    assert kb_exporter['headers']['Authorization'] == agent_exporter['headers']['Authorization']
    assert kb_exporter['headers']['Authorization'] == 'Basic ' + __import__('base64').b64encode(b'pk-na:sk-na').decode()
    assert kb_exporter['headers']['x-langfuse-ingestion-version'] == '4'
    # 队列/批处理参数与 n-agent exporter 等价
    assert kb_exporter['sending_queue'] == config['exporters']['otlphttp/langfuse']['sending_queue']
    kb_pipeline = pipelines['traces/langfuse-kb']
    assert kb_pipeline == {'receivers': ['otlp'], 'processors': ['memory_limiter', 'filter/langfuse-kb', 'transform/langfuse-kb', 'linksanitize/langfuse-kb', 'batch/langfuse-kb'], 'exporters': ['otlphttp/langfuse-kb']}


def test_kb_span_statements_type_mapping():
    m = load()
    text = '\n'.join(m.span_statements_kb())
    # 精确操作判别，不按名称猜测
    assert 'attributes["nkb.retrieval.operation"] == "search"' in text
    assert 'attributes["nkb.index.operation"] == "run"' in text
    assert 'attributes["gen_ai.operation.name"] == "embeddings"' in text
    assert 'resource.attributes["service.name"] == "n-kb"' in text
    assert 'gen_ai.operation.name"] == "chat"' not in text  # 不混入 n-agent 判别
    # 类型：retriever/chain/embedding；默认 span；冲突回落
    for kind in ('"retriever"', '"chain"', '"embedding"'):
        assert f'set(attributes["langfuse.observation.type"], {kind})' in text
    assert 'set(cache["entry"], false) where' in text  # 冲突回落重置
    # trace 正文仅全局根 chain
    assert 'parent_span_id == SpanID(0x0000000000000000)' in text
    assert 'cache["entry"] == true' in text
    # 内容源精确映射
    for field in ('index_input', 'index_result', 'retrieval_input', 'retrieval_output', 'embedding_input'):
        assert f'nkb.content.{field}' in text
    assert 'nkb.content.embedding_result' not in text  # 不导出 embedding_result/向量
    assert 'langfuse.session.id' not in text  # N-KB 无会话身份
    # embedding 字段：model.name 与 usage（usage_known 门控）
    assert 'gen_ai.request.model' in text
    assert 'attributes["nkb.model.usage_known"] == true' in text
    # KB error.type 枚举
    assert '"unsupported_file_type"' in text and '"timeout"' in text
    assert '"tool_error"' not in text  # 不混用 n-agent 枚举


def test_kb_exporter_reuses_main_project_credentials():
    # 单共享项目：KB 投影与 n-agent 投影写入同一 Langfuse 项目，凭据同主项目
    config = load().build_config('pk-na', 'sk-na', kb_endpoint='http://kb-endpoint:3000/api/public/otel/v1/traces')
    agent_exporter = config['exporters']['otlphttp/langfuse']
    kb_exporter = config['exporters']['otlphttp/langfuse-kb']
    assert kb_exporter['headers']['Authorization'] == agent_exporter['headers']['Authorization']
    # traces_endpoint 可不同：KB 分支 endpoint 保留独立配置能力
    assert agent_exporter['traces_endpoint'] != kb_exporter['traces_endpoint']
    assert kb_exporter['traces_endpoint'] == 'http://kb-endpoint:3000/api/public/otel/v1/traces'


def test_kb_value_guards_mirror_telemetry_contract():
    m = load()
    # duration_ms 是契约的 number：接受非负有限 int/double，不强制 int
    duration = m.kb_value_guard('nkb.retrieval.duration_ms')
    assert 'IsDouble' in duration and '>= 0' in duration and 'IsBool' not in duration
    assert 'IsInt' in duration and 'IsDouble' in duration  # int 与 double 都接受
    assert m.kb_value_guard('nkb.index.duration_ms') == duration.replace('nkb.retrieval', 'nkb.index')
    # 计数：IsInt >= 0（bool 不是 int）
    assert m.kb_value_guard('nkb.index.chunk_count') == 'IsInt(attributes["nkb.index.chunk_count"]) and attributes["nkb.index.chunk_count"] >= 0'
    assert m.kb_value_guard('nkb.retrieval.result_count') == 'IsInt(attributes["nkb.retrieval.result_count"]) and attributes["nkb.retrieval.result_count"] >= 0'
    assert m.kb_value_guard('gen_ai.usage.input_tokens') == 'IsInt(attributes["gen_ai.usage.input_tokens"]) and attributes["gen_ai.usage.input_tokens"] >= 0'
    assert m.kb_value_guard('nkb.model.usage_known') == 'IsBool(attributes["nkb.model.usage_known"])'
    # operation 精确枚举（源自 telemetry_contract OPERATIONS）
    operation = m.kb_value_guard('nkb.index.operation')
    for action in ('run', 'parse', 'split', 'embed', 'write'):
        assert f'"{action}"' in operation
    assert '"search"' not in operation
    assert '"search"' in m.kb_value_guard('nkb.retrieval.operation')
    # outcome 精确枚举
    outcome = m.kb_value_guard('nkb.retrieval.outcome')
    for value in ('success', 'failure', 'denied', 'cancelled', 'timed_out', 'skipped', 'deferred', 'unknown'):
        assert f'"{value}"' in outcome
    # provider 精确枚举
    assert '== "ollama"' in m.kb_value_guard('gen_ai.provider.name')
    # id/model 长度与字符模式
    assert '{1,128}' in m.kb_value_guard('nkb.index.document_id')
    assert '{1,128}' in m.kb_value_guard('nkb.index.job_id')
    assert '{1,64}' in m.kb_value_guard('gen_ai.request.model')
    # error.type 用 KB 枚举，不混 n-agent 枚举
    error = m.kb_value_guard('error.type')
    for value in ('unsupported_file_type', 'file_too_large', 'embedding_failed', 'vector_index_failed', 'sqlite_failed', 'timeout', 'other'):
        assert f'"{value}"' in error
    assert 'tool_error' not in error and 'TimeoutError' not in error


def test_kb_settings_kb_endpoint_priority_and_disabled_skip(tmp_path, monkeypatch):
    m = load()
    (tmp_path / '.env').write_text('N_OBSERVABILITY_LANGFUSE_ENABLED=true\nLANGFUSE_KB_OTEL_ENDPOINT=http://kb-dotenv:3000/api/public/otel/v1/traces\n')
    monkeypatch.delenv('N_OBSERVABILITY_LANGFUSE_ENABLED', raising=False)
    monkeypatch.delenv('LANGFUSE_KB_OTEL_ENDPOINT', raising=False)
    assert m.settings(tmp_path, ROOT) == (True, ROOT / 'docker/langfuse/.env', m.DEFAULT_ENDPOINT, 'http://kb-dotenv:3000/api/public/otel/v1/traces')
    # 进程环境优先于 dotenv
    monkeypatch.setenv('LANGFUSE_KB_OTEL_ENDPOINT', 'http://kb-env:3000/api/public/otel/v1/traces')
    assert m.settings(tmp_path, ROOT)[3] == 'http://kb-env:3000/api/public/otel/v1/traces'
    monkeypatch.delenv('LANGFUSE_KB_OTEL_ENDPOINT')
    # 重复键拒绝
    (tmp_path / '.env').write_text('N_OBSERVABILITY_LANGFUSE_ENABLED=true\nLANGFUSE_KB_OTEL_ENDPOINT=http://a/x\nLANGFUSE_KB_OTEL_ENDPOINT=http://b/x\n')
    with pytest.raises(ValueError, match='Duplicate'):
        m.settings(tmp_path, ROOT)
    # 启用时非法 KB endpoint 拒绝
    (tmp_path / '.env').write_text('N_OBSERVABILITY_LANGFUSE_ENABLED=true\nLANGFUSE_KB_OTEL_ENDPOINT=ftp://bad/x\n')
    with pytest.raises(ValueError):
        m.settings(tmp_path, ROOT)
    # 禁用时不校验未使用的 endpoint
    (tmp_path / '.env').write_text('N_OBSERVABILITY_LANGFUSE_ENABLED=false\nLANGFUSE_KB_OTEL_ENDPOINT=ftp://bad/x\n')
    enabled, _, _, _ = m.settings(tmp_path, ROOT)
    assert enabled is False


def test_kb_prepare_uses_main_credentials_only_and_disabled_short_circuits(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, 'settings', lambda directory, root: (True, tmp_path / '.env', m.DEFAULT_ENDPOINT, m.DEFAULT_ENDPOINT))
    monkeypatch.setattr(m, 'load_credentials', lambda root, path: (None, {'LANGFUSE_INIT_PROJECT_PUBLIC_KEY': 'p', 'LANGFUSE_INIT_PROJECT_SECRET_KEY': 's'}))
    # 单共享项目：prepare 只需主项目凭据，无 LANGFUSE_KB_PROJECT_* 也能发布
    result = m.prepare(tmp_path, tmp_path)
    config = json.loads(result['config'].read_text())
    assert config['exporters']['otlphttp/langfuse-kb']['headers']['Authorization'] == config['exporters']['otlphttp/langfuse']['headers']['Authorization']
    # 禁用时在读凭据前退出
    monkeypatch.setattr(m, 'settings', lambda directory, root: (False, tmp_path / '.env', m.DEFAULT_ENDPOINT, m.DEFAULT_ENDPOINT))
    reads = []
    monkeypatch.setattr(m, 'load_credentials', lambda root, path: reads.append(path))
    assert m.prepare(tmp_path, tmp_path) is None and reads == []


def test_kb_transform_contexts_and_event_masking():
    m = load()
    contexts = m.build_config('p', 's')['processors']['transform/langfuse-kb']['trace_statements']
    assert [c['context'] for c in contexts] == ['scope', 'span', 'resource', 'spanevent']
    assert contexts[0]['statements'] == ['set(name, "")', 'set(version, "")', 'keep_keys(attributes, [])']
    assert contexts[2]['statements'] == ['keep_keys(attributes, ["service.name"])']
    spanevent = json.dumps(contexts[3]['statements'])
    for event in ('nkb.document.completed', 'nkb.index.completed', 'nkb.retrieval.completed', 'nkb.model.completed', 'nkb.mcp.completed', 'nkb.http.completed'):
        assert event.replace('.', '[.]') in spanevent  # IsMatch 内的转义形式
    assert any('set(name, "event") where not IsMatch(name,' in s for s in contexts[3]['statements'])  # 异常/未知事件固定名称
    assert 'nkb.content.' not in spanevent  # 事件不透传正文
    # 伪装成合法键的秘密字符串会被类型/枚举守卫删除
    delete = next(s for s in contexts[3]['statements'] if 'delete_key(attributes, "error.type")' in s)
    assert 'IsString' in delete and '"timeout"' in delete and '"tool_error"' not in delete


def test_kb_envelopes_are_independent_and_never_synthesize():
    m = load()
    statements = m.span_statements_kb()
    bodies = [s for s in statements if s.startswith('set(attributes["langfuse.observation.input"], attributes["nkb.content.') or s.startswith('set(attributes["langfuse.observation.output"], attributes["nkb.content.')]
    # chain in/out、retriever in/out、embedding in；embedding 无 output
    assert len(bodies) == 5
    assert all('IsString(attributes["nkb.content.' in s for s in bodies)
    assert all('attributes["langfuse.observation.type"] ==' in s for s in bodies)
    # 失败/取消不合成成功 output：output 只可能来自真实属性拷贝
    for s in statements:
        if s.startswith('set(attributes["langfuse.observation.output"], attributes'):
            assert s.startswith('set(attributes["langfuse.observation.output"], attributes["nkb.content.')
    # 每个方向替换全新 envelope，suffix 独立缺失不补造
    fresh = [s for s in statements if '{"source": "' in s]
    assert len(fresh) == 5
    assert all('where' in s for s in fresh)
    for s in statements:
        if '["original_bytes"]' in s or '["truncated"]' in s or '["incomplete"]' in s or '["observed_bytes"]' in s:
            assert '!= nil' in s


def test_kb_conflict_fallback_covers_all_pairs_without_residue():
    m = load()
    statements = m.span_statements_kb()
    conflict = next(s for s in statements if s.startswith('set(cache["entry"], false) where '))
    pairs = [
        ('attributes["nkb.retrieval.operation"] == "search"', 'attributes["nkb.index.operation"] == "run"'),
        ('attributes["nkb.retrieval.operation"] == "search"', 'attributes["gen_ai.operation.name"] == "embeddings"'),
        ('attributes["nkb.index.operation"] == "run"', 'attributes["gen_ai.operation.name"] == "embeddings"'),
    ]
    for first, second in pairs:
        assert f'{first} and {second}' in conflict
    reset = [s for s in statements if s.startswith('set(') and ' where ' in conflict and s.endswith(f'where {conflict.split(" where ", 1)[1]}')]
    assert any(s.startswith('set(name, "span") where ') for s in reset)
    assert any(s.startswith('set(attributes["langfuse.observation.type"], "span") where ') for s in reset)
    # 回落后不得残留 model/usage：二者以投影后 type == embedding 为条件
    for s in statements:
        if s.startswith('set(') and ('langfuse.observation.model.name' in s or 'cache["usage"]["input"]' in s or 'langfuse.observation.usage_details' in s):
            assert 'attributes["langfuse.observation.type"] == "embedding"' in s
            assert 'gen_ai.operation.name' not in s


def test_kb_usage_tokens_gate_real_values_only():
    usage = next(s for s in load().span_statements_kb() if 'cache["usage"]["input"]' in s)
    # 真实 0 通过（>= 0）；unknown 缺失不投影；负数与 bool 被 IsInt/>=0 拒绝
    assert 'attributes["nkb.model.usage_known"] == true' in usage
    assert 'IsInt(attributes["gen_ai.usage.input_tokens"])' in usage
    assert 'attributes["gen_ai.usage.input_tokens"] >= 0' in usage


def test_kb_trace_bodies_require_global_root_entry():
    for s in load().span_statements_kb():
        if 'set(attributes["langfuse.trace.input"]' in s or 'set(attributes["langfuse.trace.output"]' in s:
            assert 'parent_span_id == SpanID(0x0000000000000000)' in s
            assert 'cache["entry"] == true' in s
