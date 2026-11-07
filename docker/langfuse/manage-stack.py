#!/usr/bin/env python3
"""Host-only, standard-library lifecycle control for the independent Langfuse stack."""
import base64
import json
import urllib.request
import fcntl
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PORT_KEYS = ('LANGFUSE_PORT', 'LANGFUSE_S3_PORT')
SECRET_KEYS = (
    'POSTGRES_PASSWORD', 'CLICKHOUSE_PASSWORD', 'REDIS_AUTH',
    'MINIO_ROOT_PASSWORD', 'SALT', 'NEXTAUTH_SECRET', 'ENCRYPTION_KEY',
    'LANGFUSE_INIT_PROJECT_PUBLIC_KEY', 'LANGFUSE_INIT_PROJECT_SECRET_KEY',
    'LANGFUSE_INIT_USER_PASSWORD',
)


def read_env(path):
    """Read only allowed KEY=literal pairs; never evaluate shell or interpolation."""
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in SECRET_KEYS + PORT_KEYS or key in values:
            raise ValueError(f'Invalid dotenv key or syntax at line {number}')
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def validate_env(values):
    """Require complete generated hexadecimal credentials without exposing values."""
    if set(values) - set(PORT_KEYS) != set(SECRET_KEYS):
        raise ValueError('Incomplete Langfuse .env; restore all required credentials; file was not rewritten')
    if any(not re.fullmatch(r'[0-9a-f]{64}', value) or len(set(value)) < 8 for value in (values[key] for key in SECRET_KEYS)):
        raise ValueError('Invalid or placeholder Langfuse credentials; file was not rewritten')
    for key in PORT_KEYS:
        if key in values and (not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535):
            raise ValueError('Invalid Langfuse port')
    if len({values[key] for key in SECRET_KEYS}) != len(SECRET_KEYS):
        raise ValueError('Langfuse credentials must be independently generated')


class Stack:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(__file__).resolve().parent).resolve()
        self.env_path = self.directory / '.env'

    def init(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            validate_env(read_env(self.env_path))
            if self.env_path.stat().st_mode & 0o777 != 0o600:
                raise ValueError('Langfuse .env permissions must be 0600; file was not rewritten')
        else:
            with os.fdopen(fd, 'w') as output:
                for key in SECRET_KEYS:
                    output.write(f'{key}={secrets.token_hex(32)}\n')
        port = os.environ.get('LANGFUSE_PORT', read_env(self.env_path).get('LANGFUSE_PORT', '13001'))
        print(f'Credentials: {self.env_path}\nUI: http://127.0.0.1:{port}')

    def execute(self, command, extra=()):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / '.lifecycle.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if command == 'init':
                return self.init()
            values = read_env(self.env_path)
            validate_env(values)
            if self.env_path.stat().st_mode & 0o777 != 0o600:
                raise ValueError('Langfuse .env permissions must be 0600')
            # Compose cannot inherit caller overrides or read/interpolate the secret dotenv.
            env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'XDG_RUNTIME_DIR', 'SSH_AUTH_SOCK')}
            env.update(values)
            for key in PORT_KEYS:
                if key in os.environ:
                    env[key] = os.environ[key]
            validate_env({key: env[key] for key in SECRET_KEYS + PORT_KEYS if key in env})
            env['COMPOSE_DISABLE_ENV_FILE'] = '1'
            base = ['docker', 'compose', '--project-name', 'n-langfuse', '--file', str(self.directory / 'docker-compose.yml'), '--env-file', os.devnull]
            if command in ('start', 'restart'):
                print('[1/4] Validating Langfuse configuration...', flush=True)
                self._run(base + ['config', '--quiet'], env)
                print('[2/4] Checking shared n-telemetry network...', flush=True)
                result = self._run(['docker', 'network', 'inspect', 'n-telemetry', '--format', '{{.Driver}}'], env, check=False)
                if result.returncode or result.stdout.strip() != 'bridge':
                    raise RuntimeError('Missing n-telemetry network; start the shared observability stack first')
            if command in ('start', 'restart'):
                args = ['up', '-d', '--wait', '--wait-timeout', '540']
                if command == 'restart':
                    args.append('--force-recreate')
            elif command == 'stop':
                args = ['stop']
            elif command == 'status':
                args = ['ps']
            elif command == 'compose':
                # Never expose resolved environment, permit destructive down, or override project scope.
                if not extra or extra[0] not in ('config', 'pull', 'ps', 'images', 'up', 'stop', 'restart'):
                    raise ValueError('Unsupported Compose subcommand')
                if extra[0] == 'config' and tuple(extra) != ('config', '--quiet'):
                    raise ValueError('Only compose config --quiet is allowed')
                if any(arg.startswith('-') for arg in extra[1:]) and tuple(extra) != ('config', '--quiet'):
                    raise ValueError('Compose option overrides are not allowed')
                args = list(extra)
            else:
                raise ValueError('Expected init|start|restart|stop|status|compose SUBCOMMAND')
            if command in ('start', 'restart'):
                print('[3/4] Starting containers and waiting for health checks (timeout: 540s)...', flush=True)
            result = self._run(base + args, env, progress=command in ('start', 'restart'))
            if command in ('start', 'restart'):
                print('[4/4] Verifying Langfuse project authentication...', flush=True)
                self.verify_project(env)
            if command == 'status' or (command == 'compose' and extra[0] in ('ps', 'images')):
                print(result.stdout, end='')
            else:
                print(f"Langfuse operation completed. UI: http://127.0.0.1:{env.get('LANGFUSE_PORT', '13001')}", flush=True)

    @staticmethod
    def verify_project(env):
        token = base64.b64encode((env['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'] + ':' + env['LANGFUSE_INIT_PROJECT_SECRET_KEY']).encode()).decode()
        request = urllib.request.Request(f"http://127.0.0.1:{env.get('LANGFUSE_PORT', '13001')}/api/public/projects", headers={'Authorization': 'Basic ' + token})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise ValueError('Unexpected authentication status')
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError('Authentication response exceeds limit')
                payload = json.loads(body)
            if not any(project.get('id') == 'n-agent-local-project' for project in payload.get('data', [])):
                raise ValueError('Unexpected project')
        except Exception:
            raise RuntimeError('Langfuse project authentication failed; check saved credentials against existing storage') from None

    @staticmethod
    def _run(args, env, check=True, progress=False):
        done = threading.Event()

        def report_wait():
            elapsed = 0
            while not done.wait(10):
                elapsed += 10
                print(f'  Still waiting for Docker startup and health checks ({elapsed}s elapsed)...', flush=True)

        reporter = threading.Thread(target=report_wait, daemon=True) if progress else None
        if reporter:
            reporter.start()
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=600, check=check)
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError('Docker operation failed or timed out; credential-bearing output withheld') from None
        finally:
            done.set()
            if reporter:
                reporter.join()
        if check and result.returncode:
            raise RuntimeError('Docker operation failed; credential-bearing output withheld')
        return result


if __name__ == '__main__':
    try:
        Stack().execute(sys.argv[1] if len(sys.argv) > 1 else 'restart', sys.argv[2:])
    except (ValueError, RuntimeError, OSError) as exc:
        print(f'Langfuse: {exc}', file=sys.stderr)
        sys.exit(1)
