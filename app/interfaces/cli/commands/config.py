from __future__ import annotations

import json
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)


_SECRET_MARKERS = ("api_key", "secret", "password", "token")


def _load_settings() -> Any:
    from app.main import build_application_services

    return build_application_services().settings


def _is_secret_field(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in _SECRET_MARKERS) or lower.endswith("_key")


def _to_dict(settings: Any) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for name in dir(settings):
        if name.startswith("_"):
            continue
        value = getattr(settings, name)
        if callable(value):
            continue
        if _is_secret_field(name):
            obj[f"{name}_present"] = bool(value)
        else:
            obj[name] = _coerce(value)
    return obj


def _coerce(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


def run(args) -> int:
    settings = _load_settings()
    obj = _to_dict(settings)
    if args.section:
        prefix = args.section.lower()
        obj = {k: v for k, v in obj.items() if k.lower().startswith(prefix)}
    render_data(obj, make_console(), fmt=resolve_format(args))
    return 0
