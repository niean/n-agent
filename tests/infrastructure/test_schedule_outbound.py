import pytest

from app.domain.gateway import GatewayHomeTarget
from app.domain.platform import Platform
from app.domain.schedule import DeliveryTarget
from app.infrastructure.schedule.outbound import ScheduleOutboundDelivery


class FakeFeishuClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_text(self, receive_id, text, receive_id_type="chat_id"):
        self.calls.append((receive_id, text, receive_id_type))
        if self.fail:
            raise RuntimeError("send failed")


@pytest.mark.asyncio
async def test_silent_delivery_skips_sending():
    feishu = FakeFeishuClient()
    result = await ScheduleOutboundDelivery(feishu).deliver(DeliveryTarget.silent(), "done")

    assert result.status == "skipped"
    assert feishu.calls == []


@pytest.mark.asyncio
async def test_dashboard_delivery_records_success_without_platform_send():
    feishu = FakeFeishuClient()
    result = await ScheduleOutboundDelivery(feishu).deliver(DeliveryTarget.dashboard(), "done")

    assert result.status == "success"
    assert feishu.calls == []


@pytest.mark.asyncio
async def test_feishu_origin_delivery_sends_receive_id_type():
    feishu = FakeFeishuClient()
    target = DeliveryTarget.origin(
        {"platform": "feishu", "receive_id": "ou_1", "receive_id_type": "open_id"}
    )

    result = await ScheduleOutboundDelivery(feishu).deliver(target, "done")

    assert result.status == "success"
    assert feishu.calls == [("ou_1", "done", "open_id")]


@pytest.mark.asyncio
async def test_feishu_home_delivery_resolves_current_home_target():
    feishu = FakeFeishuClient()
    homes = {Platform.FEISHU: GatewayHomeTarget(Platform.FEISHU, "oc_new", "chat_id")}

    async def resolve_home(platform):
        return homes.get(platform)

    result = await ScheduleOutboundDelivery(feishu, resolve_home).deliver(
        DeliveryTarget.origin({"platform": "feishu", "target": "home"}),
        "done",
    )

    assert result.status == "success"
    assert feishu.calls == [("oc_new", "done", "chat_id")]


@pytest.mark.asyncio
async def test_feishu_home_delivery_fails_without_home_target():
    async def resolve_home(platform):
        return None

    result = await ScheduleOutboundDelivery(FakeFeishuClient(), resolve_home).deliver(
        DeliveryTarget.origin({"platform": "feishu", "target": "home"}),
        "done",
    )

    assert result.status == "failed"
    assert "home" in result.error


@pytest.mark.asyncio
async def test_feishu_origin_delivery_uses_current_home_when_resolver_has_target():
    feishu = FakeFeishuClient()

    async def resolve_home(platform):
        return GatewayHomeTarget(Platform.FEISHU, "oc_home", "chat_id")

    result = await ScheduleOutboundDelivery(feishu, resolve_home).deliver(
        DeliveryTarget.origin({"platform": "feishu", "receive_id": "oc_old", "receive_id_type": "chat_id"}),
        "done",
    )

    assert result.status == "success"
    assert feishu.calls == [("oc_home", "done", "chat_id")]


@pytest.mark.asyncio
async def test_origin_without_platform_fails_without_fallback():
    feishu = FakeFeishuClient()
    target = DeliveryTarget.origin({"receive_id": "ou_1", "receive_id_type": "open_id"})

    result = await ScheduleOutboundDelivery(feishu).deliver(target, "done")

    assert result.status == "failed"
    assert "platform" in result.error
    assert feishu.calls == []


@pytest.mark.asyncio
async def test_origin_with_unknown_platform_fails():
    result = await ScheduleOutboundDelivery(FakeFeishuClient()).deliver(
        DeliveryTarget.origin({"platform": "telegram", "receive_id": "u_1", "receive_id_type": "chat_id"}),
        "done",
    )

    assert result.status == "failed"
    assert "telegram" in result.error


@pytest.mark.asyncio
async def test_origin_without_receive_id_fails():
    result = await ScheduleOutboundDelivery(FakeFeishuClient()).deliver(
        DeliveryTarget.origin({"platform": "feishu", "receive_id_type": "open_id"}),
        "done",
    )

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_feishu_send_exception_returns_failed_result():
    result = await ScheduleOutboundDelivery(FakeFeishuClient(fail=True)).deliver(
        DeliveryTarget.origin(
            {"platform": "feishu", "receive_id": "ou_1", "receive_id_type": "open_id"}
        ),
        "done",
    )

    assert result.status == "failed"
    assert "send failed" in result.error
