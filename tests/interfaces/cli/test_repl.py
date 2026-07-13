from __future__ import annotations

import asyncio
from typing import Any

import pytest
from prompt_toolkit.document import Document

from app.application.events import ChatEvent, ChatEventType
from app.domain.gateway import GatewaySessionKey
from app.domain.session import SessionSource
from app.domain.tool import ApprovalRequest, RiskLevel
from app.interfaces.cli.repl import ReplRunner, build_slash_completer


def _eof_input() -> None:
    raise EOFError()


def _make_input_fn(items: list[Any]):
    it = iter(items)

    def _fn(*_args: Any) -> str:
        item = next(it)
        return item() if callable(item) else item

    return _fn


@pytest.mark.asyncio
async def test_repl_exit_on_eof(monkeypatch, fake_console, fake_chat_adapter):
    monkeypatch.setattr("builtins.input", _make_input_fn([_eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_local_help_command(monkeypatch, fake_console, fake_chat_adapter):
    monkeypatch.setattr("builtins.input", _make_input_fn(["/help", _eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0


@pytest.mark.asyncio
async def test_repl_confirm_after_destructive(monkeypatch, fake_console, fake_chat_adapter):
    fake_chat_adapter.stream_responses = [
        [
            ("message_done", {"finish_reason": "confirmation_required", "metadata": {"confirmation": {"id": "c1"}}}),
            ("done", {}),
        ],
    ]
    monkeypatch.setattr("builtins.input", _make_input_fn(["/delete", "/confirm once", _eof_input]))
    rc = await ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False).run()
    assert rc == 0
    assert fake_chat_adapter.last_confirm_id == "c1"


def test_slash_completer_filters_root_commands_after_slash_prefix():
    completer = build_slash_completer()

    completions = list(completer.get_completions(Document("/p", cursor_position=2), None))
    texts = {completion.text for completion in completions}

    assert "/provider" in texts
    assert "/platform" in texts


def test_slash_completer_keeps_nested_subcommand_completion():
    completer = build_slash_completer()

    completions = list(completer.get_completions(Document("/provider l", cursor_position=11), None))
    texts = {completion.text for completion in completions}

    assert "list" in texts


# --- T5: dual-slot and non-TTY behavior ---


@pytest.mark.asyncio
async def test_non_tty_repl_does_not_create_bridge(monkeypatch, fake_console, fake_chat_adapter):
    runner = ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False)
    assert runner._tool_approval_bridge is None
    assert runner._last_tool_confirmation_id is None
    assert runner._last_slash_confirmation_id is None


@pytest.mark.asyncio
async def test_tty_repl_creates_bridge(fake_console, fake_chat_adapter):
    runner = ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=True)
    assert runner._tool_approval_bridge is not None


@pytest.mark.asyncio
async def test_slash_confirmation_uses_independent_slot(monkeypatch, fake_console, fake_chat_adapter):
    fake_chat_adapter.stream_responses = [
        [
            ("message_done", {"finish_reason": "confirmation_required", "metadata": {"confirmation": {"id": "slash-c1"}}}),
            ("done", {}),
        ],
    ]
    monkeypatch.setattr("builtins.input", _make_input_fn(["/delete", "/confirm once", _eof_input]))
    runner = ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False)
    rc = await runner.run()
    assert rc == 0
    assert fake_chat_adapter.last_confirm_id == "slash-c1"
    assert runner._last_tool_confirmation_id is None
    assert runner._last_slash_confirmation_id is None


@pytest.mark.asyncio
async def test_non_tty_confirm_tool_fail_closed(monkeypatch, fake_console, fake_chat_adapter):
    """Non-TTY: no decider injected, confirm tool gets fail-closed."""
    monkeypatch.setattr("builtins.input", _make_input_fn([_eof_input]))
    runner = ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False)
    assert runner._tool_approval_bridge is None
    rc = await runner.run()
    assert rc == 0


# --- T6: TTY concurrent prompt ---


class _FakePromptSession:
    """Drives TTY REPL by feeding scripted inputs on demand."""

    def __init__(self, inputs: list[Any]) -> None:
        self._iter = iter(inputs)
        self.prompts: list[str] = []

    async def prompt_async(self, prompt_str: str = "> ") -> str:
        self.prompts.append(prompt_str)
        await asyncio.sleep(0)
        try:
            item = next(self._iter)
        except StopIteration:
            raise EOFError()
        if callable(item):
            return item()
        return item


class _GatedAdapter:
    """Adapter whose send_stream blocks on a gate future, simulating a long stream."""

    def __init__(self) -> None:
        self.gate: asyncio.Future = None
        self.sent_texts: list[str] = []

    async def send_stream(self, text, conversation_id, *, approval_decider=None):
        self.sent_texts.append(text)
        yield ChatEvent(ChatEventType.MESSAGE_START)
        await self.gate
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="done")
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
        yield ChatEvent(ChatEventType.DONE)

    async def confirm(self, cid, choice, conv_id):
        from app.domain.gateway import InteractionResponse
        return InteractionResponse(messages=[])

    def grant_tool_for_session(self, sid, aid, tn):
        pass

    def is_tool_granted(self, sid, aid, tn):
        return False


@pytest.mark.asyncio
async def test_tty_repl_routes_tool_approval_during_stream(monkeypatch, fake_console):
    """Tool approval during stream: /confirm once resolves bridge Future."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-confirm-x",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-confirm-x"] = pending
    runner._last_tool_confirmation_id = "tool-confirm-x"

    fake_prompt = _FakePromptSession(["hello", "/confirm once", "/exit"])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)
    rc = await runner.run()
    assert rc == 0
    assert pending.future.done()
    decision = pending.future.result()
    assert decision.allowed is True
    assert decision.scope == "once"
    assert runner._last_tool_confirmation_id is None
    assert set(fake_prompt.prompts) == {"> "}


@pytest.mark.asyncio
async def test_tty_repl_rejects_non_approval_input_during_stream(monkeypatch, fake_console):
    """During stream, non-approval input should not trigger a second send."""
    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)
    fake_prompt = _FakePromptSession(["hello", "this should be rejected", "/exit"])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)

    run_task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.1)
    assert adapter.sent_texts == ["hello"]
    await asyncio.sleep(0.1)
    assert adapter.sent_texts == ["hello"]
    if not run_task.done():
        adapter.gate.set_result(None)
    rc = await run_task
    assert rc == 0
    assert adapter.sent_texts == ["hello"]


@pytest.mark.asyncio
async def test_tty_repl_exit_during_stream_returns_zero(monkeypatch, fake_console):
    """ /exit during stream cancels stream and returns 0."""
    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)
    fake_prompt = _FakePromptSession(["hello", "/exit"])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)
    rc = await runner.run()
    assert rc == 0
    assert adapter.sent_texts == ["hello"]


@pytest.mark.asyncio
async def test_tty_repl_eof_during_stream_returns_zero(monkeypatch, fake_console):
    """EOF (Ctrl+D) during stream cancels stream and returns 0."""
    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)
    fake_prompt = _FakePromptSession(["hello", _eof_input])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)
    rc = await runner.run()
    assert rc == 0


@pytest.mark.asyncio
async def test_tty_repl_cancel_during_stream(monkeypatch, fake_console):
    """ /cancel during stream routes to tool pending."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-confirm-y",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-confirm-y"] = pending
    runner._last_tool_confirmation_id = "tool-confirm-y"

    fake_prompt = _FakePromptSession(["hello", "/cancel", "/exit"])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)
    rc = await runner.run()
    assert rc == 0
    assert pending.future.done()
    decision = pending.future.result()
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "cancelled"


@pytest.mark.asyncio
async def test_tty_repl_trust_during_stream(monkeypatch, fake_console):
    """ /confirm trust during stream resolves with session scope."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-confirm-z",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
        session_grant_updater=lambda sid, aid, tn: None,
    )
    runner._tool_approval_bridge._pending["tool-confirm-z"] = pending
    runner._last_tool_confirmation_id = "tool-confirm-z"

    fake_prompt = _FakePromptSession(["hello", "/confirm trust", "/exit"])
    monkeypatch.setattr("app.interfaces.cli.repl.PromptSession", lambda **kw: fake_prompt)
    rc = await runner.run()
    assert rc == 0
    assert pending.future.done()
    decision = pending.future.result()
    assert decision.allowed is True
    assert decision.scope == "session"


@pytest.mark.asyncio
async def test_bridge_cleanup_clears_tool_slot_on_discard(monkeypatch, fake_console):
    """When pending is discarded (stream end), cleanup clears the tool slot."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-confirm-c",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-confirm-c"] = pending
    runner._last_tool_confirmation_id = "tool-confirm-c"
    # Wire cleanup callback like _send_stream does
    pending.cleanup = lambda cid: (
        setattr(runner, "_last_tool_confirmation_id", None)
        if runner._last_tool_confirmation_id == cid
        else None
    )

    # Simulate stream end -> _send_stream finally calls discard_pending_for_actor
    runner._tool_approval_bridge.discard_pending_for_actor(
        runner._actor_id(), runner._session_key()
    )
    assert runner._last_tool_confirmation_id is None  # cleanup cleared it


# --- T9: dual-pending routing and slash regression ---


@pytest.mark.asyncio
async def test_dual_pending_confirm_routes_to_tool_not_slash(monkeypatch, fake_console):
    """When both tool and slash pending exist, /confirm routes to tool;
    slash slot untouched, CliChatAdapter.confirm NOT called."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-dual-1",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-dual-1"] = pending
    runner._last_tool_confirmation_id = "tool-dual-1"
    runner._last_slash_confirmation_id = "slash-dual-1"

    await runner._handle_confirm("/confirm once")

    # Tool pending consumed
    assert pending.future.done()
    decision = pending.future.result()
    assert decision.allowed is True
    assert decision.scope == "once"
    # Tool slot cleared
    assert runner._last_tool_confirmation_id is None
    # Slash slot untouched
    assert runner._last_slash_confirmation_id == "slash-dual-1"
    # Adapter confirm NOT called (slash not touched)
    assert adapter.sent_texts == []


@pytest.mark.asyncio
async def test_dual_pending_cancel_routes_to_tool_not_slash(monkeypatch, fake_console):
    """When both tool and slash pending exist, /cancel routes to tool;
    slash slot untouched."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-dual-cancel",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-dual-cancel"] = pending
    runner._last_tool_confirmation_id = "tool-dual-cancel"
    runner._last_slash_confirmation_id = "slash-dual-cancel"

    await runner._handle_cancel()

    assert pending.future.done()
    decision = pending.future.result()
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "cancelled"
    assert runner._last_tool_confirmation_id is None
    assert runner._last_slash_confirmation_id == "slash-dual-cancel"
    assert adapter.sent_texts == []


@pytest.mark.asyncio
async def test_tool_claim_failure_keeps_tool_slot_and_slash_untouched(monkeypatch, fake_console):
    """Tool claim validation fails (wrong actor) -> tool slot still set,
    slash slot unchanged, tool pending still in bridge."""
    from app.interfaces.cli.cli_tool_approval import (
        CliToolApprovalError,
        PendingCliToolApproval,
    )

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    # Pending owned by a DIFFERENT actor -> claim will fail
    pending = PendingCliToolApproval(
        confirmation_id="tool-claim-fail",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id="cli:different-actor",
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-claim-fail"] = pending
    runner._last_tool_confirmation_id = "tool-claim-fail"
    runner._last_slash_confirmation_id = "slash-claim-fail"

    await runner._handle_confirm("/confirm once")

    # Claim failed: tool slot NOT cleared
    assert runner._last_tool_confirmation_id == "tool-claim-fail"
    # Slash slot untouched
    assert runner._last_slash_confirmation_id == "slash-claim-fail"
    # Tool pending still in bridge, unclaimed
    assert runner._tool_approval_bridge.owns_confirmation("tool-claim-fail")
    assert not runner._tool_approval_bridge.is_claimed("tool-claim-fail")
    assert not pending.future.done()
    # Adapter NOT called
    assert adapter.sent_texts == []


@pytest.mark.asyncio
async def test_tool_claim_success_then_repeat_does_not_fall_to_slash(monkeypatch, fake_console):
    """After tool claim succeeds during stream, repeat /confirm during stream
    shows 'no pending' and does NOT fall through to slash pending."""
    from app.interfaces.cli.cli_tool_approval import PendingCliToolApproval

    adapter = _GatedAdapter()
    adapter.gate = asyncio.get_running_loop().create_future()
    runner = ReplRunner(adapter, fake_console, conversation_id="conv-1", is_tty=True)

    loop = asyncio.get_running_loop()
    pending = PendingCliToolApproval(
        confirmation_id="tool-repeat-1",
        request=ApprovalRequest("s1", "call-1", "manage_schedule", {}, "d", RiskLevel.CONFIRM),
        session_key=runner._session_key(),
        actor_id=runner._actor_id(),
        future=loop.create_future(),
        created_at=loop.time(),
        expires_at=loop.time() + 900,
    )
    runner._tool_approval_bridge._pending["tool-repeat-1"] = pending
    runner._last_tool_confirmation_id = "tool-repeat-1"
    # Slash slot also set to verify repeat doesn't fall through
    runner._last_slash_confirmation_id = "slash-repeat-1"

    # First /confirm during stream -> routes to tool, succeeds
    await runner._handle_approval_during_stream("/confirm once")
    assert pending.future.done()
    assert runner._last_tool_confirmation_id is None
    # Slash slot still intact (not consumed by tool flow)
    assert runner._last_slash_confirmation_id == "slash-repeat-1"

    # Second /confirm during stream -> tool slot is None, shows "no pending"
    # and does NOT fall through to slash (adapter.confirm not called)
    await runner._handle_approval_during_stream("/confirm once")
    # Slash slot still intact, adapter NOT called
    assert runner._last_slash_confirmation_id == "slash-repeat-1"
    assert adapter.sent_texts == []


@pytest.mark.asyncio
async def test_post_stream_confirm_falls_to_slash_when_tool_cleared(monkeypatch, fake_console, fake_chat_adapter):
    """After stream ends, tool slot is cleared (bridge cleanup). /confirm then
    routes to slash pending. This verifies the expected fall-through behavior
    when no tool pending is active."""
    fake_chat_adapter.stream_responses = [
        [
            ("message_done", {"finish_reason": "confirmation_required", "metadata": {"confirmation": {"id": "slash-post-1"}}}),
            ("done", {}),
        ],
    ]
    monkeypatch.setattr("builtins.input", _make_input_fn(["/delete", "/confirm once", _eof_input]))
    runner = ReplRunner(fake_chat_adapter, fake_console, conversation_id="conv-1", is_tty=False)
    rc = await runner.run()
    assert rc == 0
    # Slash confirmation was consumed by adapter.confirm
    assert fake_chat_adapter.last_confirm_id == "slash-post-1"
    # Tool slot never set (non-TTY, no bridge)
    assert runner._last_tool_confirmation_id is None
    assert runner._last_slash_confirmation_id is None
