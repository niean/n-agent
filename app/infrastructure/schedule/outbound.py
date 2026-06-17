from __future__ import annotations

from app.domain.schedule import DeliveryResult, DeliveryTarget, DeliveryTargetType


class ScheduleOutboundDelivery:
    def __init__(self, feishu_client=None):
        self.feishu_client = feishu_client

    async def deliver(self, target: DeliveryTarget, content: str) -> DeliveryResult:
        if target.target_type is DeliveryTargetType.SILENT:
            return DeliveryResult("skipped")
        if target.target_type is DeliveryTargetType.DASHBOARD:
            return DeliveryResult("success")
        context = target.context
        source_type = context.get("source_type")
        if not source_type and context.get("receive_id") and context.get("receive_id_type"):
            source_type = "feishu"
        if source_type == "feishu":
            if self.feishu_client is None:
                return DeliveryResult("failed", "feishu client is not configured")
            receive_id = context.get("receive_id", "")
            if not receive_id:
                return DeliveryResult("failed", "feishu origin missing receive_id")
            try:
                await self.feishu_client.send_text(
                    receive_id,
                    content,
                    context.get("receive_id_type", "chat_id"),
                )
            except Exception as exc:
                return DeliveryResult("failed", str(exc))
            return DeliveryResult("success")
        return DeliveryResult("failed", "unsupported origin source")
