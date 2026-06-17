from __future__ import annotations

from app.domain.platform import Platform
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
        platform_value = context.get("platform")
        if not platform_value:
            return DeliveryResult("failed", "origin missing platform")
        try:
            platform = Platform(platform_value)
        except ValueError:
            return DeliveryResult("failed", f"unsupported platform: {platform_value}")
        if platform is Platform.FEISHU:
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
        return DeliveryResult("failed", f"unsupported platform: {platform.value}")
