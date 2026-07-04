"""ACP stdio server CLI entry point.

Wires ``NAgentACPAgent`` into the ``agent-client-protocol`` stdio JSON-RPC
loop. stdout carries ONLY ACP JSON-RPC frames -- all logs, diagnostics, and
provider setup hints go to stderr. Any stdout pollution corrupts the protocol.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_BENIGN_METHODS = {"_ping", "_health", "ping", "health"}


class _BenignMethodNotFoundFilter(logging.Filter):
    """Drop ERROR records whose exception is a benign method-not-found.

    ACP clients may probe the agent with ``ping``/``health`` methods that
    aren't in the agent's method table. The SDK raises ``RequestError`` with
    code -32601 and logs it via ``logging.exception`` in the connection loop.
    These tracebacks are noise on stderr; filter them out so operators see
    only actionable errors.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc_info = record.exc_info
        if not exc_info:
            return True
        exc = exc_info[1]
        if exc is None:
            return True
        code = getattr(exc, "code", None)
        if code != -32601:
            return True
        data = getattr(exc, "data", None)
        method = data.get("method") if isinstance(data, dict) else None
        if method in _BENIGN_METHODS:
            return False
        return True


def _configure_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(_BenignMethodNotFoundFilter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _load_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    if getattr(args, "check", False):
        return _run_check()
    if getattr(args, "setup", False):
        return _run_setup()
    return _run_server()


def _run_check() -> int:
    try:
        import acp  # noqa: F401
        from app.interfaces.cli.commands.acp.agent import NAgentACPAgent  # noqa: F401
    except Exception as exc:
        print(f"ACP check FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("ACP check OK: acp + NAgentACPAgent importable", file=sys.stderr)
    return 0


def _run_setup() -> int:
    print("ACP provider setup:", file=sys.stderr)
    print("  1. Create a provider:  n-agent provider create --name <n> --base-url <url> --model <m> --api-key <k>", file=sys.stderr)
    print("  2. Activate it:        n-agent provider activate <id>", file=sys.stderr)
    print("  3. (Optional) KB:      n-agent knowledge create --id <id> --name <n> --description <d> --base-type <t> --base-url <u> --dataset-id <d>", file=sys.stderr)
    print("  4. Start ACP server:   n-agent acp", file=sys.stderr)
    return 0


def _run_server() -> int:
    _configure_logging()
    try:
        import asyncio

        import acp
        from app.interfaces.cli.commands.acp.agent import NAgentACPAgent

        services = _load_services()
        agent = NAgentACPAgent(services, services.settings)
        asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("ACP server failed")
        print(f"ACP server error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
