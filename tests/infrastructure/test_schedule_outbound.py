import pytest

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
        {"source_type": "feishu", "receive_id": "ou_1", "receive_id_type": "open_id"}
    )

    result = await ScheduleOutboundDelivery(feishu).deliver(target, "done")

    assert result.status == "success"
    assert feishu.calls == [("ou_1", "done", "open_id")]


@pytest.mark.asyncio
async def test_feishu_origin_without_source_type_falls_back_to_feishu():
    feishu = FakeFeishuClient()
    target = DeliveryTarget.origin(
        {"receive_id": "ou_1", "receive_id_type": "open_id"}
    )

    result = await ScheduleOutboundDelivery(feishu).deliver(target, "done")

    assert result.status == "success"
    assert feishu.calls == [("ou_1", "done", "open_id")]


@pytest.mark.asyncio
async def test_origin_without_receive_id_fails():
    result = await ScheduleOutboundDelivery(FakeFeishuClient()).deliver(
        DeliveryTarget.origin({"source_type": "feishu", "receive_id_type": "open_id"}),
        "done",
    )

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_feishu_send_exception_returns_failed_result():
    result = await ScheduleOutboundDelivery(FakeFeishuClient(fail=True)).deliver(
        DeliveryTarget.origin(
            {"source_type": "feishu", "receive_id": "ou_1", "receive_id_type": "open_id"}
        ),
        "done",
    )

    assert result.status == "failed"
    assert "send failed" in result.error
