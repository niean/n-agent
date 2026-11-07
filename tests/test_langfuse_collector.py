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
    assert set(doc['service']['pipelines']) == {'traces/langfuse'}
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
    assert m.settings(tmp_path, ROOT) == (True, ROOT / 'custom.env', m.DEFAULT_ENDPOINT)
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
    monkeypatch.setattr(m, 'settings', lambda directory, root: (True, tmp_path / '.env', m.DEFAULT_ENDPOINT))
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
