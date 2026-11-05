"""Read-only resolution of local, trusted schedule delivery addresses."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
from typing import Any, Awaitable, Callable
from collections.abc import Mapping
from types import MappingProxyType

from app.domain.gateway import GatewayConversation, GatewaySessionRegistry
from app.domain.platform import Platform
from app.domain.schedule import DeliveryTarget, DeliveryTargetType, ScheduledTask, ScheduledTaskRegistry


class ScheduleDeliveryError(ValueError):
    """Stable reason code; never includes addresses or caller supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ScheduleDeliveryService:
    def __init__(
        self,
        gateway_registry: GatewaySessionRegistry,
        schedule_registry: ScheduledTaskRegistry,
        platform_status: Callable[[Platform], str | Awaitable[str]],
        chat_name_resolver: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        self.gateway_registry = gateway_registry
        self.schedule_registry = schedule_registry
        self.platform_status = platform_status
        self.chat_name_resolver = chat_name_resolver

    async def _status(self, platform: Platform) -> str:
        value = self.platform_status(platform)
        return await value if inspect.isawaitable(value) else value

    async def _local_types(self) -> dict[str, set[str]]:
        """Read local address evidence once per operation, never retain task JSON."""
        types: dict[str, set[str]] = {}
        home = await self.gateway_registry.get_home_target(Platform.FEISHU)
        if home and home.platform == Platform.FEISHU:
            types.setdefault(home.receive_id, set()).add(home.receive_id_type)
        # Only persisted legacy ORIGIN records are local evidence. FEISHU
        # snapshots and import input cannot establish their own trust.
        for task in await self.schedule_registry.list():
            if task.delivery_target.target_type != DeliveryTargetType.ORIGIN:
                continue
            for context in (task.origin, task.delivery_target.context):
                address, kind = context.get('receive_id'), context.get('receive_id_type')
                if context.get('platform') == 'feishu' and isinstance(address, str) and isinstance(kind, str) and kind:
                    types.setdefault(address, set()).add(kind)
        return types

    async def capture_local_evidence(self) -> Mapping[str, frozenset[str]]:
        """Freeze fallback evidence before any migration writes."""
        return MappingProxyType({key: frozenset(values) for key, values in (await self._local_types()).items()})

    async def _rows_and_type(
        self, row: GatewayConversation,
        local_types: Mapping[str, frozenset[str] | set[str]] | None = None,
        chats: dict[tuple[Platform, str], list[GatewayConversation]] | None = None,
    ) -> list[GatewayConversation]:
        if row.platform != Platform.FEISHU:
            raise ScheduleDeliveryError('unsupported_platform')
        if not row.platform_session_id.strip():
            raise ScheduleDeliveryError('unsupported_address_type')
        key = (row.platform, row.platform_session_id)
        if chats is not None and key in chats:
            rows = chats[key]
        else:
            rows = await self.gateway_registry.find_conversations(*key)
            if chats is not None:
                chats[key] = rows
        if local_types is None:
            local_types = await self._local_types()
        evidence = {item.receive_id_type for item in rows if item.receive_id_type}
        evidence.update(local_types.get(row.platform_session_id, set()))
        if len(evidence) > 1:
            raise ScheduleDeliveryError('address_type_conflict')
        if evidence != {'chat_id'}:
            raise ScheduleDeliveryError('unsupported_address_type')
        if not rows:
            raise ScheduleDeliveryError('conversation_not_found')
        return rows

    @staticmethod
    def _representative(rows: list[GatewayConversation]) -> GatewayConversation:
        return min(rows, key=lambda row: (row.created_at, row.id))

    @staticmethod
    def _target(row: GatewayConversation) -> DeliveryTarget:
        return DeliveryTarget.feishu(dict(conversation_id=row.id, platform='feishu',
                                          receive_id=row.platform_session_id, receive_id_type='chat_id'))

    async def resolve_selection(self, conversation_id: str) -> DeliveryTarget:
        row = await self.gateway_registry.get_conversation(conversation_id)
        if row is None:
            raise ScheduleDeliveryError('conversation_not_found')
        rows = await self._rows_and_type(row)
        if self._representative(rows).id != row.id:
            raise ScheduleDeliveryError('same_chat_representative')
        if await self._status(row.platform) != 'connected':
            raise ScheduleDeliveryError('platform_unavailable')
        return self._target(row)

    async def validate_snapshot(self, target: DeliveryTarget) -> DeliveryTarget:
        """Return canonical verified target for later domain authorization, never reselect."""
        context = target.context
        if target.target_type != DeliveryTargetType.FEISHU:
            raise ScheduleDeliveryError('unsupported_target')
        row = await self.gateway_registry.get_conversation(context.get('conversation_id', ''))
        if row is None:
            raise ScheduleDeliveryError('conversation_not_found')
        await self._rows_and_type(row)
        canonical = self._target(row)
        if any(context.get(key) != value for key, value in canonical.context.items()):
            raise ScheduleDeliveryError('snapshot_mismatch')
        if await self._status(row.platform) != 'connected':
            raise ScheduleDeliveryError('platform_unavailable')
        return canonical

    async def rebind_import(
        self, context: dict[str, Any], *,
        local_types: Mapping[str, frozenset[str] | set[str]] | None = None,
    ) -> DeliveryTarget:
        """Resolve imported address against local evidence, ignoring foreign internal IDs."""
        if (context.get('platform') != 'feishu' or context.get('receive_id_type') != 'chat_id'
                or not isinstance(context.get('receive_id'), str) or not context['receive_id'].strip()):
            raise ScheduleDeliveryError('invalid_import_target')
        rows = await self.gateway_registry.find_conversations(Platform.FEISHU, context['receive_id'])
        if not rows:
            raise ScheduleDeliveryError('conversation_not_found')
        representative = self._representative(rows)
        await self._rows_and_type(representative, local_types)
        return self._target(representative)

    async def project_conversations(self, rows: list[GatewayConversation]) -> dict[str, dict[str, Any]]:
        """Project a page using one local evidence read and one lookup per chat."""
        if not rows:
            return {}
        local_types = await self._local_types()
        chats: dict[tuple[Platform, str], list[GatewayConversation]] = {}
        statuses = {platform: await self._status(platform) for platform in {row.platform for row in rows}}
        projected = {row.id: await self._project_conversation(row, local_types, chats, statuses[row.platform]) for row in rows}
        # Only local, verified chat_id evidence may leave the application boundary.
        addresses = {row.platform_session_id for row in rows
                     if row.platform == Platform.FEISHU and statuses[row.platform] == 'connected'
                     and projected[row.id]['unavailable_reason'] in (None, 'same_chat_representative')}
        names: dict[str, str | None] = {}
        semaphore = asyncio.Semaphore(4)
        async def lookup(address: str) -> None:
            try:
                # Queue time is included so a page has a fixed total display budget.
                async with asyncio.timeout(2):
                    async with semaphore:
                        value = await self.chat_name_resolver(address)
                        names[address] = value.strip() if isinstance(value, str) and value.strip() else None
            except Exception:
                names[address] = None
        if self.chat_name_resolver:
            await asyncio.gather(*(lookup(address) for address in addresses))
        for row in rows:
            projected[row.id]['delivery_chat_name'] = names.get(row.platform_session_id) if row.platform == Platform.FEISHU else None
        return projected

    async def project_conversation(self, row: GatewayConversation) -> dict[str, Any]:
        return (await self.project_conversations([row]))[row.id]

    async def _project_conversation(
        self, row: GatewayConversation, local_types: dict[str, set[str]],
        chats: dict[tuple[Platform, str], list[GatewayConversation]], status: str,
    ) -> dict[str, Any]:
        reason = None
        representative = False
        try:
            rows = await self._rows_and_type(row, local_types, chats)
            representative = self._representative(rows).id == row.id
            if not representative:
                reason = 'same_chat_representative'
            elif status != 'connected':
                reason = 'platform_unavailable'
        except ScheduleDeliveryError as exc:
            reason = exc.code
        opaque = hashlib.sha256(f'{row.platform.value}:{row.platform_session_id}'.encode()).hexdigest()[:16]
        label = '飞书聊天（发送到父聊天）' if row.thread_id else '飞书聊天'
        parent_chat = f'chat-{opaque}'
        return dict(delivery_available=reason is None, unavailable_reason=reason,
                    label=label, parent_chat=parent_chat, is_representative=representative,
                    delivery_label=label, delivery_parent_chat=parent_chat,
                    delivery_unavailable_reason=reason, delivery_is_representative=representative,
                    platform_status=status)

    @staticmethod
    def _legacy_restore_available(task: ScheduledTask) -> bool:
        origin = task.origin
        return origin.get('platform') == 'feishu' and bool(
            origin.get('target') == 'home' or
            (isinstance(origin.get('receive_id'), str) and origin['receive_id'].strip()
             and isinstance(origin.get('receive_id_type'), str) and origin['receive_id_type'].strip())
        )

    async def project_task(self, task: ScheduledTask) -> dict[str, Any]:
        """Delivery UI contract. Never include addresses or raw target context.

        ``kind`` is dashboard/silent/feishu, or the real legacy platform;
        ``conversation_id`` identifies a verified local chat, including legacy echoes.
        ``home_configured`` is null when home does not apply. ``available``
        describes this saved target; legacy restoration is separate eligibility.
        """
        target = task.delivery_target
        legacy = target.target_type == DeliveryTargetType.ORIGIN
        result = dict(kind=target.target_type.value, conversation_id=None,
                      label='Dashboard' if target.target_type == DeliveryTargetType.DASHBOARD else '不投递',
                      available=True, reason=None, legacy=legacy, home=False,
                      home_configured=None,
                      legacy_restore_available=self._legacy_restore_available(task))
        if target.target_type == DeliveryTargetType.FEISHU:
            result.update(conversation_id=target.context.get('conversation_id'), label='飞书聊天')
            try:
                await self.validate_snapshot(target)
            except ScheduleDeliveryError as exc:
                result.update(available=False, reason=exc.code, label='历史飞书目标（需重新选择）')
        elif legacy:
            context = target.context
            platform = context.get('platform') or task.origin.get('platform')
            # Platform labels use a fixed vocabulary, never raw saved values.
            platform_label = {'dingtalk': '钉钉', 'wecom': '企业微信'}.get(platform) if isinstance(platform, str) else None
            label = (f'历史{platform_label}投递目标（需重新连接）' if platform_label
                     else '历史投递目标（未知平台，需重新连接）')
            result.update(kind=platform if isinstance(platform, str) and platform else 'origin',
                          label=label, available=False, reason='unsupported_platform')
            if platform == 'feishu':
                # Legacy outbound always prefers the current home, including
                # records that originally saved a concrete source address.
                saved_home = await self.gateway_registry.get_home_target(Platform.FEISHU)
                configured = bool(saved_home and saved_home.platform == Platform.FEISHU
                                  and saved_home.receive_id and saved_home.receive_id_type)
                home = saved_home is not None or context.get('target') == 'home'
                result.update(home=home, label='默认通知会话' if home else '历史飞书聊天')
                if home:
                    result.update(home_configured=configured)
                    valid = configured
                    reason = 'home_target_not_configured'
                else:
                    valid = bool(context.get('receive_id') and context.get('receive_id_type'))
                    reason = 'invalid_legacy_target'
                if valid:
                    connected = await self._status(Platform.FEISHU) == 'connected'
                    result.update(available=connected, reason=None if connected else 'platform_unavailable')
                    # Echo the actual legacy outbound address without changing its
                    # persisted target or querying network display metadata.
                    address = saved_home.receive_id if home and configured else context.get('receive_id')
                    address_type = saved_home.receive_id_type if home and configured else context.get('receive_id_type')
                    if connected and address_type == 'chat_id':
                        rows = await self.gateway_registry.find_conversations(Platform.FEISHU, address)
                        if rows:
                            representative = self._representative(rows)
                            try:
                                await self.resolve_selection(representative.id)
                            except ScheduleDeliveryError:
                                pass
                            else:
                                result['conversation_id'] = representative.id
                else:
                    result.update(available=False, reason=reason)
        return result
