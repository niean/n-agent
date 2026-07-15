#!/usr/bin/env python3
"""Capture one 512px-high JPEG and upload it to a fixed Alibaba OSS region."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo


DEFAULT_ENV_PATH = Path("/Users/niean/install/n-agent/secrets/oss.env")
DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
URL_LIFETIME_SECONDS = 3600
# Alibaba Cloud STS AssumeRole enforces DurationSeconds within [900, 3600].
STS_DURATION_SECONDS = 3600
STS_EXPIRY_MARGIN_SECONDS = 300
MIN_JPEG_BYTES = 128
MAX_JPEG_BYTES = 20_000_000
MAX_HTTP_BODY_BYTES = 1_048_576
REQUIRED_KEYS = frozenset(
    {
        "OSS_USER_AK",
        "OSS_USER_SK",
        "OSS_ROLE_ARN",
        "OSS_BUCKET_NAME",
        "OSS_BUCKET_PATH",
        "OSS_REGION",
    }
)
OSS_ENDPOINTS = {
    "oss-cn-beijing": "oss-cn-beijing.aliyuncs.com",
    "oss-cn-hangzhou": "oss-cn-hangzhou.aliyuncs.com",
    "oss-cn-shanghai": "oss-cn-shanghai.aliyuncs.com",
    "oss-cn-shenzhen": "oss-cn-shenzhen.aliyuncs.com",
}
_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
_PREFIX_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HOSTNAME_MAX_LENGTH = 32
_PHOTO_TIMEZONE = ZoneInfo("Asia/Shanghai")


class PhotoUploadError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _format_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_expiration(value: object) -> float:
    if not isinstance(value, str):
        raise PhotoUploadError("sts_invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError as exc:
        raise PhotoUploadError("sts_invalid") from exc


def _read_secure_file(path: Path, *, max_bytes: int = 65_536) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PhotoUploadError("config_unsafe") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) & ~0o600
    ):
        raise PhotoUploadError("config_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) & ~0o600
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise PhotoUploadError("config_unsafe")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(8192, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise PhotoUploadError("config_invalid")
            return b"".join(chunks)
        finally:
            os.close(fd)
    except PhotoUploadError:
        raise
    except OSError as exc:
        raise PhotoUploadError("config_unsafe") from exc


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        if len(value) < 2 or value[-1] != value[0]:
            raise PhotoUploadError("config_invalid")
        value = value[1:-1]
        if value[0:1] == "\"":
            raise PhotoUploadError("config_invalid")
    return value


def load_config(path: Path) -> dict[str, str]:
    try:
        text = _read_secure_file(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PhotoUploadError("config_invalid") from exc
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PhotoUploadError("config_invalid")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key) or key in values:
            raise PhotoUploadError("config_invalid")
        values[key] = _unquote_env_value(raw_value)
    if set(values) != REQUIRED_KEYS or any(not values[key] for key in REQUIRED_KEYS):
        raise PhotoUploadError("config_invalid")
    if not _BUCKET_RE.fullmatch(values["OSS_BUCKET_NAME"]):
        raise PhotoUploadError("config_invalid")
    if values["OSS_REGION"] not in OSS_ENDPOINTS:
        raise PhotoUploadError("config_invalid")
    prefix = values["OSS_BUCKET_PATH"].strip("/")
    if prefix != values["OSS_BUCKET_PATH"] or not prefix:
        raise PhotoUploadError("config_invalid")
    parts = prefix.split("/")
    if any(part in {".", ".."} or not _PREFIX_PART_RE.fullmatch(part) for part in parts):
        raise PhotoUploadError("config_invalid")
    values["OSS_BUCKET_PATH"] = prefix
    return values


def _percent(value: object, *, safe: str = "") -> str:
    return urllib.parse.quote(str(value), safe=safe)


def _request(
    request: urllib.request.Request,
    *,
    opener: Callable = urllib.request.urlopen,
    timeout: float,
    max_body_bytes: int = MAX_HTTP_BODY_BYTES,
) -> tuple[int, bytes]:
    try:
        with opener(request, timeout=timeout) as response:
            status_code = int(response.status)
            body = response.read(max_body_bytes + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise PhotoUploadError("http_failed") from exc
    if len(body) > max_body_bytes:
        raise PhotoUploadError("http_failed")
    return status_code, body


def _capture(
    output: Path,
    *,
    ffmpeg: str,
    runner: Callable = subprocess.run,
) -> int:
    argv = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-framerate",
        "30",
        "-video_size",
        "1280x720",
        "-i",
        "0:none",
        "-vframes",
        "1",
        "-vf",
        "scale=-1:512",
        "-q:v",
        "2",
        "-y",
        str(output),
    ]
    try:
        result = runner(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhotoUploadError("capture_failed") from exc
    if result.returncode != 0:
        raise PhotoUploadError("capture_failed")
    try:
        output.chmod(0o600)
        size = output.stat().st_size
        with output.open("rb") as handle:
            head = handle.read(2)
            handle.seek(-2, os.SEEK_END)
            tail = handle.read(2)
    except OSError as exc:
        raise PhotoUploadError("capture_invalid") from exc
    if not MIN_JPEG_BYTES <= size <= MAX_JPEG_BYTES or head != b"\xff\xd8" or tail != b"\xff\xd9":
        raise PhotoUploadError("capture_invalid")
    return size


def _assume_role(config: dict[str, str], *, opener: Callable, now: float) -> dict[str, str]:
    parameters = {
        "Action": "AssumeRole",
        "RoleArn": config["OSS_ROLE_ARN"],
        "RoleSessionName": "n-agent-photo-upload",
        "DurationSeconds": str(STS_DURATION_SECONDS),
        "Format": "JSON",
        "Version": "2015-04-01",
        "AccessKeyId": config["OSS_USER_AK"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
        "Timestamp": _format_utc(now),
    }
    canonical_query = "&".join(
        f"{_percent(key)}={_percent(value)}" for key, value in sorted(parameters.items())
    )
    string_to_sign = f"GET&{_percent('/')}&{_percent(canonical_query)}"
    signature = base64.b64encode(
        hmac.new(
            (config["OSS_USER_SK"] + "&").encode(),
            string_to_sign.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    request = urllib.request.Request(
        f"https://sts.aliyuncs.com/?{canonical_query}&Signature={_percent(signature)}"
    )
    try:
        status_code, body = _request(request, opener=opener, timeout=15)
        if not 200 <= status_code < 300:
            raise PhotoUploadError("sts_failed")
        parsed = json.loads(body)
        credentials = parsed["Credentials"]
        result = {
            key: credentials[key]
            for key in ("AccessKeyId", "AccessKeySecret", "SecurityToken", "Expiration")
        }
        if any(not isinstance(value, str) or not value for value in result.values()):
            raise PhotoUploadError("sts_invalid")
        if _parse_expiration(result["Expiration"]) < now + URL_LIFETIME_SECONDS - STS_EXPIRY_MARGIN_SECONDS:
            raise PhotoUploadError("sts_expiry_short")
        return result
    except PhotoUploadError as exc:
        if exc.code in {"sts_invalid", "sts_expiry_short"}:
            raise
        raise PhotoUploadError("sts_failed") from exc
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PhotoUploadError("sts_invalid") from exc


def _upload_and_sign(
    config: dict[str, str],
    credentials: dict[str, str],
    image: bytes,
    object_key: str,
    *,
    opener: Callable,
    now: float,
) -> tuple[int, str]:
    bucket = config["OSS_BUCKET_NAME"]
    endpoint = OSS_ENDPOINTS[config["OSS_REGION"]]
    resource = f"/{bucket}/{object_key}"
    object_url = f"https://{bucket}.{endpoint}/{_percent(object_key, safe='/')}"
    date_value = datetime.fromtimestamp(now, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    content_type = "image/jpeg"
    upload_string = (
        f"PUT\n\n{content_type}\n{date_value}\n"
        f"x-oss-security-token:{credentials['SecurityToken']}\n{resource}"
    )
    upload_signature = base64.b64encode(
        hmac.new(
            credentials["AccessKeySecret"].encode(),
            upload_string.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    upload_request = urllib.request.Request(
        object_url,
        data=image,
        headers={
            "Date": date_value,
            "Content-Type": content_type,
            "x-oss-security-token": credentials["SecurityToken"],
            "Authorization": f"OSS {credentials['AccessKeyId']}:{upload_signature}",
        },
        method="PUT",
    )
    try:
        upload_status, _ = _request(upload_request, opener=opener, timeout=30)
    except PhotoUploadError as exc:
        raise PhotoUploadError("upload_failed") from exc
    if not 200 <= upload_status < 300:
        raise PhotoUploadError("upload_failed")

    expires = int(now) + URL_LIFETIME_SECONDS
    signing_resource = (
        f"{resource}?security-token={credentials['SecurityToken']}"
    )
    download_string = f"GET\n\n\n{expires}\n{signing_resource}"
    download_signature = base64.b64encode(
        hmac.new(
            credentials["AccessKeySecret"].encode(),
            download_string.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    query = urllib.parse.urlencode(
        {
            "security-token": credentials["SecurityToken"],
            "OSSAccessKeyId": credentials["AccessKeyId"],
            "Expires": str(expires),
            "Signature": download_signature,
        }
    )
    signed_url = f"{object_url}?{query}"
    probe = urllib.request.Request(
        signed_url, method="GET", headers={"Range": "bytes=0-0"}
    )
    try:
        probe_status, _ = _request(
            probe, opener=opener, timeout=15, max_body_bytes=2
        )
    except PhotoUploadError as exc:
        raise PhotoUploadError("probe_failed") from exc
    if not 200 <= probe_status < 300:
        raise PhotoUploadError("probe_failed")
    return upload_status, signed_url


def _normalize_hostname(raw: str) -> str:
    """Normalize the host name into a lowercase, path-safe key segment."""
    if not isinstance(raw, str) or not raw:
        return "host"
    base = raw.split(".")[0].lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", base) if part]
    normalized = "-".join(parts)
    if not normalized:
        return "host"
    return normalized[:_HOSTNAME_MAX_LENGTH]


def run_cli(
    *,
    env_path: Path = DEFAULT_ENV_PATH,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Callable = subprocess.run,
    opener: Callable = urllib.request.urlopen,
    clock: Callable[[], float] = time.time,
    hostname_factory: Callable[[], str] = socket.gethostname,
    cleanup: Callable[[Path], None] = shutil.rmtree,
) -> int:
    temp_dir: Path | None = None
    success_output: str | None = None
    error_code: str | None = None
    try:
        config = load_config(Path(env_path))
        generated_at = clock()
        hostname = _normalize_hostname(hostname_factory())
        opaque_name = (
            f"photo_{hostname}_"
            f"{datetime.fromtimestamp(generated_at, _PHOTO_TIMEZONE):%y%m%d%H%M%S}.jpg"
        )
        object_key = f"{config['OSS_BUCKET_PATH']}/{opaque_name}"
        temp_dir = Path(tempfile.mkdtemp(prefix="n-agent-photo-"))
        temp_dir.chmod(0o700)
        image_path = temp_dir / "capture.jpg"
        size = _capture(image_path, ffmpeg=ffmpeg, runner=runner)
        image = image_path.read_bytes()
        sts_now = clock()
        credentials = _assume_role(config, opener=opener, now=sts_now)
        signing_now = clock()
        status_code, signed_url = _upload_and_sign(
            config,
            credentials,
            image,
            object_key,
            opener=opener,
            now=signing_now,
        )
        success_output = (
            f"CAPTURED:{opaque_name}:{size}\n"
            f"UPLOAD_HTTP:{status_code}\n"
            f"URL:{signed_url}\n"
        )
    except PhotoUploadError as exc:
        error_code = exc.code
    except Exception:
        error_code = "internal_error"

    if temp_dir is not None:
        try:
            cleanup(temp_dir)
            if temp_dir.exists():
                raise OSError("temporary_directory_still_exists")
        except Exception:
            error_code = "cleanup_failed"

    if error_code is not None or success_output is None:
        sys.stderr.write(f"ERROR:{error_code or 'internal_error'}\n")
        return 1
    sys.stdout.write(success_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
