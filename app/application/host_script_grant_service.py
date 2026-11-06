"""Synchronize only durable intents from successful, controlled script writes."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict
from typing import Protocol

from app.domain.host_script_grant import (
    HostScriptGrantClient,
    HostScriptIntent,
    HostScriptIntentStore,
    merge_script_grants,
)
from app.domain.host_terminal_policy import HostTerminalPolicySnapshot


class ScriptResolver(Protocol):
    async def resolve_script_bytes(
        self, skill_name: str, script_relative_path: str
    ): ...


class HostScriptGrantService:
    def __init__(
        self,
        store: HostScriptIntentStore,
        client: HostScriptGrantClient,
        skill_service: ScriptResolver,
    ):
        self.store = store
        self.client = client
        self.skill_service = skill_service
        self._lock = asyncio.Lock()

    async def prepare_write(
        self, skill_name: str, script_relative_path: str, file_content: str, origin: str
    ) -> HostScriptIntent:
        async with self._lock:
            floor = 0
            try:
                remote = await self.client.script_grants("list")
                floor = max(
                    (
                        g.revision
                        for g in remote
                        if g.identity == (skill_name, script_relative_path)
                    ),
                    default=0,
                )
            except Exception:  # noqa: BLE001 - boundary failures must fail closed
                floor = 0  # Offline writes are never rebased during retries.
            return await self.store.prepare(
                skill_name,
                script_relative_path,
                hashlib.sha256(file_content.encode("utf-8")).hexdigest(),
                origin,
                floor=floor,
            )

    async def commit_write(self, revision: int) -> None:
        await self.store.transition(revision, "prepared", "pending")

    async def effective_snapshot(
        self, base: HostTerminalPolicySnapshot
    ) -> HostTerminalPolicySnapshot:
        try:
            return merge_script_grants(base, await self.client.script_grants("list"))
        except Exception:  # noqa: BLE001 - boundary failures must fail closed
            return base

    async def _source_error(self, intent: HostScriptIntent) -> str | None:
        if (
            getattr(self.skill_service, "last_auto_refresh_error", None)
            == "scan_failed"
        ):
            return "scan_blocked"
        try:
            source = await self.skill_service.resolve_script_bytes(*intent.identity)
        except Exception:  # noqa: BLE001 - boundary failures must fail closed
            return "scan_blocked"
        return None if source.sha256 == intent.sha256 else "source_mismatch"

    async def list_grants(self) -> list[dict]:
        rows = []
        for intent in await self.store.list_current():
            row = asdict(intent)
            if intent.state == "active":
                error = await self._source_error(intent)
                if error:
                    row.update(state="stale", error=error)
            rows.append(row)
        return rows

    async def sync(self) -> None:
        async with self._lock:
            for intent in await self.store.list_current():
                if intent.state == "pending_revoke":
                    await self._send(intent, "revoke", "revoked")
                elif intent.state == "pending":
                    error = await self._source_error(intent)
                    if error:
                        await self.store.transition(
                            intent.revision, intent.state, intent.state, error
                        )
                    else:
                        await self._send(intent, "grant", "active")

    async def _send(self, intent: HostScriptIntent, operation: str, state: str) -> bool:
        try:
            response = await self.client.script_grants(operation, intent.grant(state))
            confirmed = next(
                (g for g in response if g.identity == intent.identity), None
            )
            if (
                confirmed is None
                or confirmed.revision != intent.revision
                or confirmed.state != state
                or confirmed.sha256 != intent.sha256
                or confirmed.origin != intent.origin
            ):
                raise ValueError("invalid_confirmation")
        except Exception as exc:  # noqa: BLE001 - sanitize all bridge failures
            code = str(exc)
            if code not in {
                "host_script_grants_stale_revision",
                "host_script_grants_source_mismatch",
            }:
                code = "host_script_grants_unavailable"
            await self.store.transition(
                intent.revision, intent.state, intent.state, code
            )
            return False
        return await self.store.transition(intent.revision, intent.state, state)

    async def revoke(self, revision: int) -> dict:
        async with self._lock:
            intent = next(
                (i for i in await self.store.list_current() if i.revision == revision),
                None,
            )
            if intent is None or intent.state not in {
                "active",
                "pending",
                "pending_revoke",
            }:
                raise ValueError("host_script_grants_stale_revision")
            await self.store.transition(revision, intent.state, "pending_revoke")
            pending = next(
                i for i in await self.store.list_current() if i.revision == revision
            )
            success = await self._send(pending, "revoke", "revoked")
            return {
                "success": success,
                "revision": revision,
                "state": "revoked" if success else "pending_revoke",
                "error": None if success else "host_script_grants_unavailable",
            }

    async def revoke_skill(
        self, skill_name: str, script_relative_path: str | None = None
    ) -> bool:
        success = True
        for intent in await self.store.list_current():
            if (
                intent.skill_name == skill_name
                and (
                    script_relative_path is None
                    or intent.script_relative_path == script_relative_path
                )
                and intent.state in {"active", "pending", "pending_revoke"}
            ):
                result = await self.revoke(intent.revision)
                success = result["success"] and success
        return success
