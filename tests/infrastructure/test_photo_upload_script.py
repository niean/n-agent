from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import types
from urllib.error import HTTPError
import urllib.parse

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "photo-and-upload"
    / "photo-upload.py"
)
_SCRIPT_SETTING = os.environ.get("N_AGENT_PHOTO_UPLOAD_SCRIPT")
_DEPLOYED_SCRIPT = (
    Path(_SCRIPT_SETTING).expanduser() if _SCRIPT_SETTING else None
)

FAKE_TEMP_TOKEN = "temp+/= token"


def _load_module():
    module = types.ModuleType("deployed_photo_upload")
    module.__file__ = str(SCRIPT)
    sys.modules[module.__name__] = module
    source = SCRIPT.read_bytes()
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)
    return module


def test_optional_deployed_artifact_matches_versioned_fixture_byte_for_byte():
    if _DEPLOYED_SCRIPT is None:
        return
    try:
        metadata = _DEPLOYED_SCRIPT.lstat()
    except FileNotFoundError:
        pytest.fail("configured deployed photo script is missing")
    assert stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)
    assert _DEPLOYED_SCRIPT.read_bytes() == SCRIPT.read_bytes()


class _Response:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


def _secure_env(path: Path, *, extra: str = "") -> None:
    path.write_text(
        "OSS_USER_AK=long-ak\n"
        "OSS_USER_SK=long-sk\n"
        "OSS_ROLE_ARN=acs:ram::123:role/photo\n"
        "OSS_BUCKET_NAME=safe-bucket\n"
        "OSS_BUCKET_PATH=photos/test\n"
        "OSS_REGION=oss-cn-beijing\n"
        + extra,
        encoding="utf-8",
    )
    path.chmod(0o600)


def _success_fakes(module, tmp_path: Path):
    requests = []

    def runner(argv, **kwargs):
        assert kwargs["shell"] is False
        assert "scale=-1:512" in argv
        output = Path(argv[-1])
        output.write_bytes(b"\xff\xd8" + b"x" * 256 + b"\xff\xd9")
        output.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    expiration = module._format_utc(1_700_000_000 + 7200)
    sts = json.dumps(
        {
            "Credentials": {
                "AccessKeyId": "temp-ak",
                "AccessKeySecret": "temp-sk",
                "SecurityToken": FAKE_TEMP_TOKEN,
                "Expiration": expiration,
            }
        }
    ).encode()

    def opener(request, timeout):
        requests.append((request, timeout))
        if request.full_url.startswith("https://sts.aliyuncs.com/"):
            return _Response(200, sts)
        if request.get_method() == "PUT":
            assert request.full_url.startswith(
                "https://safe-bucket.oss-cn-beijing.aliyuncs.com/"
            )
            return _Response(201)
        assert request.get_method() == "GET"
        assert request.headers["Range"] == "bytes=0-0"
        return _Response(206, b"x")

    return runner, opener, requests


def test_success_contract_signing_and_cleanup(tmp_path, capsys, monkeypatch):
    module = _load_module()
    env_path = tmp_path / "oss.env"
    _secure_env(env_path)
    runner, opener, requests = _success_fakes(module, tmp_path)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    observed_temp_dirs = []
    real_mkdtemp = module.tempfile.mkdtemp

    def tracked_mkdtemp(**kwargs):
        path = real_mkdtemp(dir=private_root, **kwargs)
        observed_temp_dirs.append(Path(path))
        return path

    monkeypatch.setattr(module.tempfile, "mkdtemp", tracked_mkdtemp)
    fixed_nonce = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr(module.uuid, "uuid4", lambda: fixed_nonce)

    def cleanup(path):
        assert capsys.readouterr().out == ""
        shutil.rmtree(path)

    code = module.run_cli(
        env_path=env_path,
        ffmpeg="/opt/homebrew/bin/ffmpeg",
        runner=runner,
        opener=opener,
        clock=lambda: 1_700_000_000,
        token_factory=lambda: "unpredictable_token_123456",
        cleanup=cleanup,
    )

    captured = capsys.readouterr()
    assert code == 0
    lines = captured.out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("CAPTURED:photo-")
    assert lines[0].endswith(":260")
    assert lines[1] == "UPLOAD_HTTP:201"
    assert lines[2].startswith("URL:https://")
    assert captured.err == ""
    assert all(not path.exists() for path in observed_temp_dirs)
    assert requests[0][1] <= 15 and requests[1][1] <= 30 and requests[2][1] <= 15
    sts_query = requests[0][0].full_url
    assert "DurationSeconds=7200" in sts_query
    signed_url = lines[2][4:]
    assert "Expires=1700003600" in signed_url
    assert "long-ak" not in signed_url and "long-sk" not in signed_url
    object_leaf = lines[0].split(":", 2)[1]
    assert object_leaf in requests[1][0].full_url
    assert ".." not in object_leaf and "/" not in object_leaf

    expected_sts_params = {
        "Action": "AssumeRole",
        "RoleArn": "acs:ram::123:role/photo",
        "RoleSessionName": "n-agent-photo-upload",
        "DurationSeconds": "7200",
        "Format": "JSON",
        "Version": "2015-04-01",
        "AccessKeyId": "long-ak",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": fixed_nonce,
        "Timestamp": "2023-11-14T22:13:20Z",
    }
    sts_parts = urllib.parse.urlsplit(requests[0][0].full_url)
    sts_query_values = dict(
        urllib.parse.parse_qsl(sts_parts.query, keep_blank_values=True)
    )
    actual_sts_signature = sts_query_values.pop("Signature")
    assert sts_query_values == expected_sts_params
    expected_sts_query = "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}"
        for key, value in sorted(expected_sts_params.items())
    )
    expected_sts_canonical = (
        "GET&%2F&" + urllib.parse.quote(expected_sts_query, safe="")
    )
    expected_sts_signature = base64.b64encode(
        hmac.new(
            b"long-sk&", expected_sts_canonical.encode(), hashlib.sha1
        ).digest()
    ).decode()
    assert actual_sts_signature == expected_sts_signature

    expected_object_key = f"photos/test/{object_leaf}"
    expected_resource = f"/safe-bucket/{expected_object_key}"
    expected_date = datetime.fromtimestamp(
        1_700_000_000, timezone.utc
    ).strftime("%a, %d %b %Y %H:%M:%S GMT")
    expected_put_canonical = (
        f"PUT\n\nimage/jpeg\n{expected_date}\n"
        f"x-oss-security-token:{FAKE_TEMP_TOKEN}\n{expected_resource}"
    )
    expected_put_signature = base64.b64encode(
        hmac.new(
            b"temp-sk", expected_put_canonical.encode(), hashlib.sha1
        ).digest()
    ).decode()
    assert requests[1][0].get_header("Authorization") == (
        f"OSS temp-ak:{expected_put_signature}"
    )

    signed_get = urllib.parse.urlsplit(requests[2][0].full_url)
    signed_get_query = urllib.parse.parse_qs(
        signed_get.query, keep_blank_values=True, strict_parsing=True
    )
    assert signed_get_query["security-token"] == [FAKE_TEMP_TOKEN]
    assert signed_get_query["OSSAccessKeyId"] == ["temp-ak"]
    assert signed_get_query["Expires"] == ["1700003600"]
    expected_get_canonical = (
        "GET\n\n\n1700003600\n"
        f"{expected_resource}?security-token={FAKE_TEMP_TOKEN}"
    )
    expected_get_signature = base64.b64encode(
        hmac.new(
            b"temp-sk", expected_get_canonical.encode(), hashlib.sha1
        ).digest()
    ).decode()
    assert signed_get_query["Signature"] == [expected_get_signature]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.chmod(0o644), "ERROR:config_unsafe"),
        (lambda p: p.write_text(p.read_text() + "OSS_REGION=oss-cn-beijing\n"), "ERROR:config_invalid"),
        (lambda p: p.write_text(p.read_text().replace("safe-bucket", "bad/bucket")), "ERROR:config_invalid"),
        (lambda p: p.write_text(p.read_text().replace("photos/test", "../escape")), "ERROR:config_invalid"),
        (lambda p: p.write_text(p.read_text().replace("oss-cn-beijing", "https://evil.invalid")), "ERROR:config_invalid"),
    ],
)
def test_config_is_strict_and_never_executes(tmp_path, capsys, mutate, expected):
    module = _load_module()
    env_path = tmp_path / "oss.env"
    marker = tmp_path / "executed"
    _secure_env(env_path, extra=f"IGNORED=$(touch {marker})\n")
    mutate(env_path)
    env_path.chmod(0o600 if stat.S_IMODE(env_path.stat().st_mode) != 0o644 else 0o644)

    code = module.run_cli(env_path=env_path)

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert captured.err.strip() == expected
    assert not marker.exists()


def test_config_rejects_symlink_wrong_owner_and_missing_field(tmp_path, capsys, monkeypatch):
    module = _load_module()
    real = tmp_path / "real.env"
    _secure_env(real)
    link = tmp_path / "link.env"
    link.symlink_to(real)
    assert module.run_cli(env_path=link) != 0
    assert "URL:" not in capsys.readouterr().out

    monkeypatch.setattr(module.os, "geteuid", lambda: os.geteuid() + 1)
    assert module.run_cli(env_path=real) != 0
    assert "URL:" not in capsys.readouterr().out
    monkeypatch.undo()

    real.write_text(real.read_text().replace("OSS_ROLE_ARN=acs:ram::123:role/photo\n", ""))
    real.chmod(0o600)
    assert module.run_cli(env_path=real) != 0
    assert "URL:" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [b"not-jpeg", b"\xff\xd8\xff\xd9", b"\xff\xd8" + b"x" * 21_000_000 + b"\xff\xd9"],
    ids=["not-jpeg", "too-small", "too-large"],
)
def test_photo_validation_fails_without_output_or_url(tmp_path, capsys, payload):
    module = _load_module()
    env_path = tmp_path / "oss.env"
    _secure_env(env_path)

    def runner(argv, **_kwargs):
        Path(argv[-1]).write_bytes(payload)
        return subprocess.CompletedProcess(argv, 0, b"secret stderr", b"")

    code = module.run_cli(env_path=env_path, runner=runner)
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert captured.err == "ERROR:capture_invalid\n"


@pytest.mark.parametrize("failure", ["ffmpeg", "sts", "put", "probe", "short_sts"])
def test_each_failure_has_only_stage_code_and_no_secret_or_url(
    tmp_path, capsys, monkeypatch, failure
):
    module = _load_module()
    env_path = tmp_path / "oss.env"
    _secure_env(env_path)
    runner, opener, _requests = _success_fakes(module, tmp_path)
    observed_temp_dirs = []
    real_mkdtemp = module.tempfile.mkdtemp

    def tracked_mkdtemp(**kwargs):
        path = real_mkdtemp(dir=tmp_path, **kwargs)
        observed_temp_dirs.append(Path(path))
        return path

    monkeypatch.setattr(module.tempfile, "mkdtemp", tracked_mkdtemp)

    if failure == "ffmpeg":
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2, b"", b"long-sk temp-token")
    elif failure == "sts":
        def opener(request, timeout):
            raise HTTPError(request.full_url, 403, "long-sk", {}, io.BytesIO(b"temp-token"))
    elif failure == "put":
        good = opener
        def opener(request, timeout):
            if request.get_method() == "PUT":
                return _Response(500, b"long-sk temp-token")
            return good(request, timeout)
    elif failure == "probe":
        good = opener
        def opener(request, timeout):
            if request.get_method() == "GET" and not request.full_url.startswith("https://sts"):
                return _Response(416, b"long-sk temp-token")
            return good(request, timeout)
    else:
        def opener(request, timeout):
            if request.full_url.startswith("https://sts"):
                body = json.dumps({"Credentials": {
                    "AccessKeyId": "temp-ak", "AccessKeySecret": "temp-sk",
                    "SecurityToken": "temp-token",
                    "Expiration": module._format_utc(1_700_000_000 + 3700),
                }}).encode()
                return _Response(200, body)
            raise AssertionError("upload must not run")

    code = module.run_cli(
        env_path=env_path, runner=runner, opener=opener,
        clock=lambda: 1_700_000_000,
        token_factory=lambda: "unpredictable_token_123456",
    )
    captured = capsys.readouterr()
    assert code != 0
    assert observed_temp_dirs and all(not path.exists() for path in observed_temp_dirs)
    assert captured.out == ""
    assert captured.err.startswith("ERROR:") and captured.err.count("\n") == 1
    for secret in ("long-ak", "long-sk", "temp-ak", "temp-sk", "temp-token", "URL:"):
        assert secret not in captured.out + captured.err


def test_cleanup_failure_suppresses_all_success_output(tmp_path, capsys):
    module = _load_module()
    env_path = tmp_path / "oss.env"
    _secure_env(env_path)
    runner, opener, _requests = _success_fakes(module, tmp_path)
    cleanup_paths = []

    def failing_cleanup(path):
        cleanup_paths.append(path)
        raise OSError("sensitive cleanup detail")

    code = module.run_cli(
        env_path=env_path,
        runner=runner,
        opener=opener,
        clock=lambda: 1_700_000_000,
        token_factory=lambda: "unpredictable_token_123456",
        cleanup=failing_cleanup,
    )

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert captured.err == "ERROR:cleanup_failed\n"
    assert "URL:" not in captured.out + captured.err
    assert "sensitive cleanup detail" not in captured.err
    assert len(cleanup_paths) == 1
    shutil.rmtree(cleanup_paths[0])
