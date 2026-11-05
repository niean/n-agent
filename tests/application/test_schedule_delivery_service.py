from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.schedule_delivery_service import ScheduleDeliveryService, ScheduleDeliveryError
from app.domain.gateway import GatewayConversation, GatewayHomeTarget
from app.domain.platform import Platform
from app.domain.schedule import DeliveryTarget


class Gateway:
    def __init__(self, rows, home=None):
        self.rows, self.home = rows, home

    async def get_conversation(self, identifier):
        return next((row for row in self.rows if row.id == identifier), None)

    async def find_conversations(self, platform, receive_id):
        return [row for row in self.rows if row.platform == platform and row.platform_session_id == receive_id]

    async def get_home_target(self, platform):
        return self.home


class Tasks:
    async def list(self):
        return []


def row(identifier='one', kind='chat_id', **changes):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return replace(GatewayConversation(identifier, Platform.FEISHU, 'secret-address', '', '', None, now, now, kind), **changes)


def service(rows, status='connected', home=None):
    return ScheduleDeliveryService(Gateway(rows, home), Tasks(), lambda platform: status)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,platform,status,identifier,allowed', [
    ('chat_id', Platform.FEISHU, 'connected', 'one', True),
    (None, Platform.FEISHU, 'connected', 'one', False),
    ('open_id', Platform.FEISHU, 'connected', 'one', False),
    ('chat_id', Platform.DINGTALK, 'connected', 'one', False),
    ('chat_id', Platform.FEISHU, 'disconnected', 'one', False),
    ('chat_id', Platform.FEISHU, 'connected', 'missing', False),
])
async def test_selection_requires_current_trusted_chat(kind, platform, status, identifier, allowed):
    resolver = service([row(kind=kind, platform=platform)], status)
    if allowed:
        target = await resolver.resolve_selection(identifier)
        assert target.context == dict(conversation_id='one', platform='feishu', receive_id='secret-address', receive_id_type='chat_id')
    else:
        with pytest.raises(ScheduleDeliveryError) as exc:
            await resolver.resolve_selection(identifier)
        assert 'secret-address' not in str(exc.value)


@pytest.mark.asyncio
async def test_home_evidence_conflicts_and_import_is_offline_read_only():
    home = GatewayHomeTarget(Platform.FEISHU, 'secret-address', 'chat_id')
    resolver = service([row(kind=None)], 'disconnected', home)
    context = dict(platform='feishu', receive_id='secret-address', receive_id_type='chat_id', conversation_id='foreign')
    assert (await resolver.rebind_import(context)).context['conversation_id'] == 'one'
    with pytest.raises(ScheduleDeliveryError):
        await service([row(kind='open_id')], home=home).resolve_selection('one')
    with pytest.raises(ScheduleDeliveryError):
        await service([row(kind=None)]).validate_snapshot(DeliveryTarget.feishu(context))


@pytest.mark.asyncio
async def test_representative_is_stable_but_snapshot_keeps_original_thread():
    rows = [row('z', thread_id='thread-z'), row('a', thread_id='thread-a')]
    resolver = service(rows)
    with pytest.raises(ScheduleDeliveryError, match='same_chat_representative'):
        await resolver.resolve_selection('z')
    target = DeliveryTarget.feishu(dict(conversation_id='z', platform='feishu', receive_id='secret-address', receive_id_type='chat_id'))
    assert await resolver.validate_snapshot(target) == target
    candidate = await resolver.project_conversation(rows[0])
    assert candidate['is_representative'] is False
    assert candidate['unavailable_reason'] == 'same_chat_representative'
    assert 'secret-address' not in str(candidate)


@pytest.mark.asyncio
async def test_legacy_local_origin_evidence_is_exact_and_feishu_snapshot_cannot_self_attest():
    resolver = service([row(kind=None)])
    context = dict(platform='feishu', receive_id='secret-address', receive_id_type='chat_id', conversation_id='one')
    tasks = [SimpleNamespace(origin=context, delivery_target=DeliveryTarget.feishu(context))]
    async def list_tasks():
        return tasks
    resolver.schedule_registry.list = list_tasks
    with pytest.raises(ScheduleDeliveryError, match='unsupported_address_type'):
        await resolver.validate_snapshot(DeliveryTarget.feishu(context))
    tasks[:] = [SimpleNamespace(origin=context, delivery_target=DeliveryTarget.origin({}))]
    assert (await resolver.resolve_selection('one')).context['receive_id_type'] == 'chat_id'
    tasks[:] = [SimpleNamespace(origin={**context, 'platform': 'dingtalk'}, delivery_target=DeliveryTarget.origin({}))]
    with pytest.raises(ScheduleDeliveryError):
        await resolver.resolve_selection('one')


@pytest.mark.asyncio
async def test_snapshot_drift_rejected_and_unknown_candidates_remain_visible():
    resolver = service([row()])
    target = DeliveryTarget.feishu(dict(conversation_id='one', platform='feishu', receive_id='changed', receive_id_type='chat_id'))
    with pytest.raises(ScheduleDeliveryError, match='snapshot_mismatch'):
        await resolver.validate_snapshot(target)
    candidate = await service([row(kind=None)]).project_conversation(row(kind=None))
    assert candidate['delivery_available'] is False
    assert candidate['unavailable_reason'] == 'unsupported_address_type'


@pytest.mark.asyncio
async def test_batch_projection_reads_local_evidence_once_and_each_chat_once():
    rows = [row('one'), row('two', thread_id='thread'), row('three', platform_session_id='another-address')]
    resolver = service(rows)
    calls = dict(home=0, tasks=0, chats=0)
    original_home = resolver.gateway_registry.get_home_target
    original_find = resolver.gateway_registry.find_conversations
    async def home(platform):
        calls['home'] += 1
        return await original_home(platform)
    async def find(platform, address):
        calls['chats'] += 1
        return await original_find(platform, address)
    async def tasks():
        calls['tasks'] += 1
        return []
    resolver.gateway_registry.get_home_target = home
    resolver.gateway_registry.find_conversations = find
    resolver.schedule_registry.list = tasks
    projected = await resolver.project_conversations(rows)
    assert list(projected) == ['one', 'two', 'three']
    assert projected['one']['delivery_available'] is True
    assert projected['two']['unavailable_reason'] == 'same_chat_representative'
    assert calls == dict(home=1, tasks=1, chats=2)


@pytest.mark.asyncio
async def test_import_evidence_snapshot_is_immutable_and_empty_does_not_refresh():
    resolver = service([row(kind=None)], 'disconnected')
    frozen = await resolver.capture_local_evidence()
    with pytest.raises(TypeError):
        frozen['secret-address'] = frozenset({'chat_id'})
    resolver.gateway_registry.home = GatewayHomeTarget(Platform.FEISHU, 'secret-address', 'chat_id')
    context = dict(platform='feishu', receive_id='secret-address', receive_id_type='chat_id')
    with pytest.raises(ScheduleDeliveryError):
        await resolver.rebind_import(context, local_types=frozen)
    assert (await resolver.rebind_import(context)).context['conversation_id'] == 'one'


@pytest.mark.asyncio
async def test_chat_name_is_display_only_deduplicated_and_failure_is_optional():
    rows = [row('one'), row('two', thread_id='thread'), row('other', platform=Platform.DINGTALK)]
    calls = []
    async def name(address):
        calls.append(address)
        return '每日报告群'
    resolver = ScheduleDeliveryService(Gateway(rows), Tasks(), lambda _: 'connected', name)
    projected = await resolver.project_conversations(rows)
    assert calls == ['secret-address']
    assert projected['one']['delivery_chat_name'] == '每日报告群'
    assert projected['one']['delivery_label'] == '飞书聊天'
    assert 'secret-address' not in str(projected)
    target = await resolver.resolve_selection('one')
    await resolver.validate_snapshot(target)
    await resolver.rebind_import(dict(target.context))
    assert calls == ['secret-address']
    async def broken(_):
        raise RuntimeError('secret error')
    resolver.chat_name_resolver = broken
    candidate = await resolver.project_conversation(rows[0])
    assert candidate['delivery_available'] is True
    assert candidate['delivery_chat_name'] is None
    assert 'secret error' not in str(candidate)
    resolver.platform_status = lambda _: 'disconnected'
    resolver.chat_name_resolver = name
    await resolver.project_conversations(rows)
    assert calls == ['secret-address']


@pytest.mark.asyncio
async def test_chat_name_page_budget_bounds_concurrency_and_preserves_availability(monkeypatch):
    import asyncio
    from app.application import schedule_delivery_service as module
    original_timeout = asyncio.timeout
    monkeypatch.setattr(module.asyncio, 'timeout', lambda _: original_timeout(0.01))
    rows = [row(str(i), platform_session_id=f'address-{i}') for i in range(20)]
    active = maximum = 0
    async def slow(_):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    resolver = ScheduleDeliveryService(Gateway(rows), Tasks(), lambda _: 'connected', slow)
    projected = await resolver.project_conversations(rows)
    assert maximum == 4
    assert active == 0
    assert all(item['delivery_available'] and item['delivery_chat_name'] is None for item in projected.values())


@pytest.mark.asyncio
async def test_legacy_projection_maps_actual_home_to_stable_chat_without_mutation():
    origin = dict(platform='feishu', receive_id='origin-address', receive_id_type='chat_id')
    target = DeliveryTarget.origin(origin)
    task = SimpleNamespace(origin=origin, delivery_target=target)
    resolver = service([row('home'), row('source', platform_session_id='origin-address')],
                       home=GatewayHomeTarget(Platform.FEISHU, 'secret-address', 'chat_id'))
    result = await resolver.project_task(task)
    assert result['conversation_id'] == 'home'
    assert task.delivery_target == target and task.origin == origin
    resolver.gateway_registry.rows = [row('source', platform_session_id='origin-address')]
    assert (await resolver.project_task(task))['conversation_id'] is None


@pytest.mark.asyncio
async def test_legacy_projection_does_not_map_untrusted_or_disconnected_chat():
    context = dict(platform='feishu', receive_id='secret-address', receive_id_type='chat_id')
    task = SimpleNamespace(origin=context, delivery_target=DeliveryTarget.origin(context))
    assert (await service([row(kind='open_id')]).project_task(task))['conversation_id'] is None
    assert (await service([row()], status='disconnected').project_task(task))['conversation_id'] is None
