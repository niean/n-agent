from __future__ import annotations

import logging
import shutil
from pathlib import Path

_SEEDS_ROOT = Path(__file__).resolve().parent / "seeds"

logger = logging.getLogger(__name__)


def seed_default_plugins(plugins_root: Path) -> None:
    plugins_root = Path(plugins_root)
    if not _SEEDS_ROOT.exists():
        return
    try:
        plugins_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("seed_default_plugins: plugins_root unwritable %s: %s", plugins_root, exc)
        return
    for source in _SEEDS_ROOT.iterdir():
        if not source.is_dir():
            continue
        if source.name.startswith(".") or source.name == "__pycache__":
            continue
        target = plugins_root / source.name
        for src_file in source.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(source)
            dst_file = target / rel
            if dst_file.exists():
                continue
            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_file, dst_file)
            except OSError as exc:
                logger.warning("seed_default_plugins: copy failed %s -> %s: %s", src_file, dst_file, exc)
