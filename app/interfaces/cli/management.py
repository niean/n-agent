from __future__ import annotations

import argparse
import io
import sys
from typing import Any


def is_management_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    name = stripped[1:].split(maxsplit=1)[0]
    if not name:
        return False
    return name in _get_dispatch()


def _strip_leading_slash(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("/"):
        return stripped[1:]
    return stripped


def run_management_command(text: str) -> int:
    """Parse and run a management command like 'provider list --json'.

    Sync entry point; calls command module run() which may asyncio.run() internally.
    Must be called from a thread when inside a running event loop (REPL).
    """
    cmd_text = _strip_leading_slash(text)
    tokens = _tokenize(cmd_text)
    if not tokens:
        return 0
    parser = _build_management_parser()
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = stdout_buf
    sys.stderr = stderr_buf
    try:
        try:
            args = parser.parse_args(tokens)
        except SystemExit as exc:
            code = int(exc.code or 0)
            _flush_buffers(stdout_buf, stderr_buf, old_stdout)
            return code
        if args.command is None:
            _flush_buffers(stdout_buf, stderr_buf, old_stdout)
            return 0
        handler = _get_dispatch().get(args.command)
        if handler is None:
            print(f"unknown command: {args.command}", file=old_stdout, flush=True)
            return 2
        if _requires_tty_passthrough(args, old_stdout):
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            return handler(args)
        rc = handler(args)
        _flush_buffers(stdout_buf, stderr_buf, old_stdout)
        return rc
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=old_stdout, flush=True)
        _flush_buffers(stdout_buf, stderr_buf, old_stdout)
        return 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def _requires_tty_passthrough(args: Any, stdout: Any) -> bool:
    if args.command != "sessions":
        return False
    if not getattr(args, "browse", False):
        return False
    if getattr(args, "pick", None):
        return False
    if getattr(args, "no_interactive", False):
        return False
    isatty = getattr(stdout, "isatty", None)
    return bool(isatty and isatty())


def _tokenize(text: str) -> list[str]:
    import shlex

    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _build_management_parser() -> argparse.ArgumentParser:
    from app.interfaces.cli.main import build_parser

    return build_parser()


def _get_dispatch() -> dict[str, Any]:
    from app.interfaces.cli.main import _DISPATCH

    return _DISPATCH


def _flush_buffers(stdout_buf: io.StringIO, stderr_buf: io.StringIO, real_stdout: Any) -> None:
    out = stdout_buf.getvalue()
    err = stderr_buf.getvalue()
    if out:
        print(out, end="", file=real_stdout, flush=True)
    if err:
        print(err, end="", file=sys.__stderr__, flush=True)


def get_management_completions() -> dict[str, Any]:
    """Return nested dict for NestedCompleter."""
    from app.interfaces.cli.main import build_parser

    parser = build_parser()
    result: dict[str, Any] = {}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for cmd_name, sub_parser in action.choices.items():
            sub_completions: dict[str, Any] = {}
            for sub_action in sub_parser._actions:
                if isinstance(sub_action, argparse._SubParsersAction):
                    for sub_name in sub_action.choices:
                        sub_completions[sub_name] = None
            result[f"/{cmd_name}"] = sub_completions if sub_completions else None
    return result
