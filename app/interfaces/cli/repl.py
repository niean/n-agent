from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.interfaces.cli.render import render_markdown, render_status
from app.interfaces.cli.slash import (
    GATEWAY_COMMANDS,
    LOCAL_COMMANDS,
    handle_local_command,
    is_local_command,
)
from app.interfaces.cli.streaming import consume_stream

HISTORY_DIR = Path.home() / ".n-agent"
HISTORY_FILE = HISTORY_DIR / "cli_history"


class ReplRunner:
    def __init__(self, gateway_client: Any, console: Any, conversation_id: str, is_tty: bool = True) -> None:
        self._client = gateway_client
        self._console = console
        self._conversation_id = conversation_id
        self._is_tty = is_tty
        self._last_confirmation_id: str | None = None
        self._history_file = str(HISTORY_FILE)
        self._prompt_session: Any = None

    async def run(self) -> int:
        self._ensure_history_file()
        if self._is_tty:
            return await self._run_tty()
        return await self._run_non_tty()

    def _ensure_history_file(self) -> None:
        HISTORY_DIR.mkdir(mode=0o700, exist_ok=True)
        if not HISTORY_FILE.exists():
            HISTORY_FILE.touch(mode=0o600)

    async def _run_non_tty(self) -> int:
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                return 0
            except KeyboardInterrupt:
                return 0
            if not text:
                continue
            should_exit = await self._handle_input(text)
            if should_exit:
                return 0

    async def _run_tty(self) -> int:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout

        completer = WordCompleter(GATEWAY_COMMANDS + LOCAL_COMMANDS, pattern=None)
        self._prompt_session = PromptSession(
            history=FileHistory(self._history_file),
            completer=completer,
        )
        while True:
            try:
                with patch_stdout():
                    text = await self._prompt_session.prompt_async("> ")
                text = text.strip()
                if not text:
                    continue
                should_exit = await self._handle_input(text)
            except (EOFError, KeyboardInterrupt):
                return 0
            if should_exit:
                return 0

    async def _handle_input(self, text: str) -> bool:
        """Return True if REPL should exit."""
        if is_local_command(text):
            if text.startswith("/confirm"):
                await self._handle_confirm(text)
                return False
            if text == "/cancel":
                await self._handle_cancel()
                return False
            if text == "/exit":
                return True
            handle_local_command(text, self._console, history_path=self._history_file)
            return False

        await self._send_stream(text)
        return False

    async def _send_stream(self, text: str) -> None:
        def on_confirmation(metadata: dict[str, Any]) -> None:
            confirmation = metadata.get("confirmation") or {}
            cid = confirmation.get("id")
            if cid:
                self._last_confirmation_id = cid
                render_status("destructive command requires confirmation", "warning", self._console)
                render_status("use /confirm once, /confirm trust, or /cancel", "info", self._console)

        stream = self._client.send_stream(text, self._conversation_id)
        task = asyncio.create_task(consume_stream(stream, self._console, on_confirmation=on_confirmation))
        try:
            await task
        except asyncio.CancelledError:
            task.cancel()
            aclose = getattr(stream, "aclose", None)
            if aclose:
                try:
                    await aclose()
                except Exception:
                    pass
            render_status("\n[interrupted]", "warning", self._console)

    async def _handle_confirm(self, text: str) -> None:
        if not self._last_confirmation_id:
            render_status("no pending confirmation", "warning", self._console)
            return
        parts = text.split()
        choice = parts[1] if len(parts) > 1 else "once"
        choice_map = {"once": "once", "trust": "trust_session"}
        if choice not in choice_map:
            render_status("usage: /confirm once|trust", "warning", self._console)
            return
        resp = await self._client.confirm(self._last_confirmation_id, choice_map[choice], self._conversation_id)
        for msg in resp.messages:
            render_markdown(msg.content, self._console)
        self._last_confirmation_id = None

    async def _handle_cancel(self) -> None:
        if not self._last_confirmation_id:
            render_status("no pending confirmation", "warning", self._console)
            return
        resp = await self._client.confirm(self._last_confirmation_id, "cancel", self._conversation_id)
        for msg in resp.messages:
            render_markdown(msg.content, self._console)
        self._last_confirmation_id = None
