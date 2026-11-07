"""Standalone Langfuse lifecycle contract; never touches the real deployment."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / 'docker/langfuse/manage-stack.py'


def load():
    assert CONTROLLER.is_file(), 'standalone Langfuse controller is missing'
    spec = importlib.util.spec_from_file_location('langfuse_stack', CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_loads():
    assert callable(load().read_env)


def test_init_is_private_idempotent_and_independent(tmp_path):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    env = tmp_path / '.env'
    original = env.read_bytes()
    stack.init()
    assert env.read_bytes() == original
    assert env.stat().st_mode & 0o777 == 0o600
    values = m.read_env(env)
    m.validate_env(values)
    assert len(set(values.values())) == len(values)
    assert all(len(v) == 64 for v in values.values())


def test_partial_or_placeholder_config_is_not_rewritten(tmp_path):
    m = load()
    stack = m.Stack(tmp_path)
    env = tmp_path / '.env'
    env.write_text('POSTGRES_PASSWORD=changeme\n')
    before = env.read_bytes()
    with pytest.raises(ValueError):
        stack.init()
    assert env.read_bytes() == before


def test_dotenv_is_literal_and_whitelisted(tmp_path):
    m = load()
    env = tmp_path / '.env'
    marker = tmp_path / 'executed'
    env.write_text(f'POSTGRES_PASSWORD=$(touch {marker})\n')
    assert m.read_env(env)['POSTGRES_PASSWORD'] == f'$(touch {marker})'
    assert not marker.exists()
    env.write_text('UNRECOGNIZED=value\n')
    with pytest.raises(ValueError):
        m.read_env(env)


def test_lifecycle_is_scoped_sanitized_and_preserves_storage(tmp_path, monkeypatch):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(m.subprocess, 'run', fake_run)
    stack.execute('stop')
    args, options = calls[-1]
    assert args[:5] == ['docker', 'compose', '--project-name', 'n-langfuse', '--file']
    assert str(tmp_path / 'docker-compose.yml') in args
    assert args[-1] == 'stop'
    assert options['timeout'] == 600
    assert options['env']['POSTGRES_PASSWORD'] == m.read_env(tmp_path / '.env')['POSTGRES_PASSWORD']
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr='SECRET_MUST_NOT_LEAK')
    monkeypatch.setattr(m.subprocess, 'run', fail)
    with pytest.raises(RuntimeError) as exc:
        stack.execute('status')
    assert 'SECRET_MUST_NOT_LEAK' not in str(exc.value)


def test_missing_shared_network_fails_before_up(tmp_path, monkeypatch):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    def fail(args, **kwargs):
        return subprocess.CompletedProcess(args, 1 if args[:3] == ['docker', 'network', 'inspect'] else 0, '', '')
    monkeypatch.setattr(m.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='n-telemetry'):
        stack.execute('start')


def test_compose_has_fixed_isolated_topology():
    compose = ROOT / 'docker/langfuse/docker-compose.yml'
    assert compose.is_file(), 'Langfuse Compose is missing'
    c = yaml.safe_load(compose.read_text())
    assert set(c['services']) == {'langfuse-web', 'langfuse-worker', 'postgres', 'redis', 'clickhouse', 'minio'}
    assert c['networks']['telemetry'] == {'external': True, 'name': 'n-telemetry'}
    for name, service in c['services'].items():
        image = service['image']
        assert ':latest' not in image and image.rsplit(':', 1)[-1] not in {'4', '7', '17'}
        assert service['healthcheck']
        assert service['mem_limit']
        assert service['logging']['options'] == {'max-size': '10m', 'max-file': '3'}
        if name == 'langfuse-web':
            assert service['ports'] == ['127.0.0.1:${LANGFUSE_PORT:-13001}:3000']
            assert service['networks']['telemetry']['aliases'] == ['n-langfuse-web']
        elif name == 'minio':
            assert all(p.startswith('127.0.0.1:') for p in service['ports'])
        else:
            assert not service.get('ports')
        if name != 'langfuse-web':
            assert 'telemetry' not in service.get('networks', {})
    assert len(c['volumes']) == 5
    assert 'appendonly' in str(c['services']['redis']['command'])
    assert 'noeviction' in str(c['services']['redis']['command'])


def test_optional_ports_and_start_preflight(tmp_path, monkeypatch):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    monkeypatch.setenv('LANGFUSE_PORT', '13123')
    monkeypatch.setenv('LANGFUSE_S3_PORT', '19123')
    calls = []
    def fake(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, 'bridge', '')
    monkeypatch.setattr(m.subprocess, 'run', fake)
    monkeypatch.setattr(stack, 'verify_project', lambda env: None)
    stack.execute('start')
    assert calls[0][0][-2:] == ['config', '--quiet']
    assert calls[-1][1]['env']['LANGFUSE_PORT'] == '13123'
    assert calls[-1][1]['env']['LANGFUSE_S3_PORT'] == '19123'
    os.chmod(tmp_path / '.env', 0o644)
    with pytest.raises(ValueError, match='0600'):
        stack.execute('status')


def test_lock_contention_returns_promptly(tmp_path):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    with (tmp_path / '.lifecycle.lock').open('a') as lock:
        m.fcntl.flock(lock, m.fcntl.LOCK_EX | m.fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            stack.execute('status')


@pytest.mark.parametrize('value', ['0', '99999', '$(touch /tmp/unsafe)', '13001:3000'])
def test_invalid_port_is_rejected(tmp_path, value):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    values = m.read_env(tmp_path / '.env')
    values['LANGFUSE_PORT'] = value
    with pytest.raises(ValueError, match='port'):
        m.validate_env(values)


def test_minio_does_not_publish_console_and_apps_bind_all_interfaces():
    c = yaml.safe_load((ROOT / 'docker/langfuse/docker-compose.yml').read_text())
    assert len(c['services']['minio']['ports']) == 1
    for name in ['langfuse-web', 'langfuse-worker']:
        assert c['services'][name]['environment']['HOSTNAME'] == '0.0.0.0'


def test_project_authentication_rejects_mismatched_project(monkeypatch):
    m = load()
    import io
    class Response(io.BytesIO):
        status = 200
    class Opener:
        body = b'{"data":[{"id":"wrong"}]}'
        def open(self, *a, **k):
            return Response(self.body)
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda *a: Opener())
    env = {key: 'a' * 64 for key in m.SECRET_KEYS}
    with pytest.raises(RuntimeError, match='authentication'):
        m.Stack.verify_project(env)
    Opener.body = b'{"data":[{"id":"n-agent-local-project"}]}'
    m.Stack.verify_project(env)


def test_auth_probe_disables_proxy_redirects_and_bounds_response(monkeypatch):
    m = load()
    import io
    class Response(io.BytesIO):
        status = 200
        def read(self, size=-1):
            assert 0 < size <= 65537
            return super().read(size)
    handlers_seen = []
    class Opener:
        def open(self, request, timeout):
            return Response(b'{"data":[{"id":"n-agent-local-project"}]}')
    def build(*handlers):
        handlers_seen.extend(handlers)
        return Opener()
    monkeypatch.setattr(m.urllib.request, 'build_opener', build)
    env = {key: 'a' * 64 for key in m.SECRET_KEYS}
    m.Stack.verify_project(env)
    assert any(isinstance(h, m.urllib.request.ProxyHandler) and h.proxies == {} for h in handlers_seen)
    redirect = next(h for h in handlers_seen if isinstance(h, m.urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, 302, 'redirect', {}, 'http://example.invalid') is None
    Response.status = 201
    with pytest.raises(RuntimeError, match='authentication'):
        m.Stack.verify_project(env)


def test_compose_status_commands_display_output(tmp_path, monkeypatch, capsys):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    capsys.readouterr()
    monkeypatch.setattr(m.subprocess, 'run', lambda args, **kw: subprocess.CompletedProcess(args, 0, 'SAFE_STATUS\n', ''))
    for command in ['ps', 'images']:
        stack.execute('compose', [command])
        assert 'SAFE_STATUS' in capsys.readouterr().out


@pytest.mark.parametrize('command', ['start', 'restart'])
def test_startup_reports_stages_without_docker_output(tmp_path, monkeypatch, capsys, command):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    capsys.readouterr()

    def fake(args, **kwargs):
        output = capsys.readouterr().out
        if args[-2:] == ['config', '--quiet']:
            assert '[1/4]' in output
        elif args[:3] == ['docker', 'network', 'inspect']:
            assert '[2/4]' in output
            return subprocess.CompletedProcess(args, 0, 'bridge', '')
        else:
            assert '[3/4]' in output
        return subprocess.CompletedProcess(args, 0, 'SECRET_MUST_NOT_LEAK', '')

    def verify(env):
        output = capsys.readouterr().out
        assert '[4/4]' in output
        assert 'SECRET_MUST_NOT_LEAK' not in output

    monkeypatch.setattr(m.subprocess, 'run', fake)
    monkeypatch.setattr(stack, 'verify_project', verify)
    stack.execute(command)
    assert 'operation completed' in capsys.readouterr().out


@pytest.mark.parametrize('extra', [('config',), ('down',), ('ps', '--project-name', 'other'), ('ps', '--file', 'other.yml'), ('ps', '--env-file', 'other.env')])
def test_compose_rejects_secret_output_and_scope_overrides(tmp_path, extra):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    with pytest.raises(ValueError):
        stack.execute('compose', extra)


def test_host_compose_and_secret_overrides_are_ignored(tmp_path, monkeypatch):
    m = load()
    stack = m.Stack(tmp_path)
    stack.init()
    monkeypatch.setenv('COMPOSE_PROJECT_NAME', 'other')
    monkeypatch.setenv('COMPOSE_FILE', 'other.yml')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'host-secret')
    seen = []
    def fake(args, **kwargs):
        seen.append(kwargs['env'])
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(m.subprocess, 'run', fake)
    stack.execute('status')
    assert 'COMPOSE_PROJECT_NAME' not in seen[0]
    assert 'COMPOSE_FILE' not in seen[0]
    assert seen[0]['POSTGRES_PASSWORD'] == m.read_env(tmp_path / '.env')['POSTGRES_PASSWORD']


def test_auth_probe_rejects_oversized_body(monkeypatch):
    m = load()
    import io
    class Response(io.BytesIO):
        status = 200
    class Opener:
        def open(self, *a, **k):
            return Response(b' ' * 65537)
    monkeypatch.setattr(m.urllib.request, 'build_opener', lambda *a: Opener())
    with pytest.raises(RuntimeError, match='authentication'):
        m.Stack.verify_project({key: 'a' * 64 for key in m.SECRET_KEYS})
