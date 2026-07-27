"""Browser Domain architecture boundary tests.

Asserts app/domain/browser.py is pure: no Playwright/CDP SDK, no SQLite/FastAPI,
no Application/Infrastructure/Interfaces imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROWSER_DOMAIN = ROOT / "app" / "domain" / "browser.py"

FORBIDDEN = (
    "playwright",
    "sqlite3",
    "fastapi",
    "app.application",
    "app.infrastructure",
    "app.interfaces",
    "asyncio",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_browser_domain_pure():
    mods = _imported_modules(BROWSER_DOMAIN)
    bad = [
        m for m in mods
        if m in FORBIDDEN or any(m.startswith(f + ".") for f in FORBIDDEN)
    ]
    assert not bad, f"app/domain/browser.py imports forbidden modules: {bad}"
