"""Path-safety helpers shared by skill / future file tools.

参考 hermes-agent/tools/path_security.py，故意保留同名 API 便于读者交叉对照。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def has_traversal_component(path_str: str) -> bool:
    if not path_str:
        return True
    if os.path.isabs(path_str):
        return True
    raw_parts = path_str.replace("\\", "/").split("/")
    return any(p in ("..", ".") for p in raw_parts)


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError as exc:
        return f"path resolve error: {exc}"
    if resolved == resolved_root:
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return f"path is not within {resolved_root}: {resolved}"
    return None
