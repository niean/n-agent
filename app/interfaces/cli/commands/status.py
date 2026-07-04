from __future__ import annotations

import json
from typing import Any


def _build_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    services = _build_services()
    payload = services.health_snapshot()
    print(json.dumps(payload, ensure_ascii=False))
    return 0
