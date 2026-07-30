"""Thin CLI adapter for the Browser Host Bridge runtime."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from app import browser_host_runtime


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token-path", required=True)
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--profile-root", required=True)
    parser.add_argument("--chrome-executable", default=None)
    parser.add_argument("--port", type=_port, default=8766)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration without starting Chrome or listening",
    )


def run(args: argparse.Namespace) -> int:
    try:
        config = browser_host_runtime.BrowserHostConfig(
            token_path=Path(args.token_path),
            sqlite_path=Path(args.sqlite_path),
            profile_root=Path(args.profile_root),
            chrome_executable=(
                Path(args.chrome_executable)
                if args.chrome_executable is not None
                else None
            ),
            port=args.port,
        )
        if args.check:
            browser_host_runtime.validate_browser_host_config(config)
            print("ok")
            return 0
        return browser_host_runtime.serve_browser_host(config)
    except browser_host_runtime.BrowserHostRuntimeError as exc:
        print(f"error: {exc.error_code}", file=sys.stderr)
        return 2 if exc.error_code == "browser_host_address_in_use" else 1
    except Exception:
        print("error: browser_host_internal_error", file=sys.stderr)
        return 1


def _port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be 1..65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    return port


__all__ = ["register_cli", "run"]
