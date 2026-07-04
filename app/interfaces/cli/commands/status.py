from __future__ import annotations

from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)


def _build_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    services = _build_services()
    payload = services.health_snapshot()
    render_data(payload, make_console(), fmt=resolve_format(args))
    return 0
