from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.domain.gateway import GatewayHomeTarget
from app.domain.platform import Platform
from app.domain.schedule import DeliveryResult, DeliveryTarget, DeliveryTargetType

HomeTargetResolver = Callable[[Platform], Awaitable[GatewayHomeTarget | None]]


class ScheduleOutboundDelivery:
    def __init__(self, feishu_client=None, home_target_resolver: HomeTargetResolver | None = None):
        self.feishu_client = feishu_client
        self.home_target_resolver = home_target_resolver

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
            receive_id_type = context.get("receive_id_type", "chat_id")
            if self.home_target_resolver is not None:
                home = await self.home_target_resolver(platform)
                if home is not None:
                    receive_id = home.receive_id
                    receive_id_type = home.receive_id_type
                elif context.get("target") == "home":
                    return DeliveryResult("failed", "feishu home target is not configured")
            elif context.get("target") == "home":
                return DeliveryResult("failed", "feishu home target resolver is not configured")
            if not receive_id:
                return DeliveryResult("failed", "feishu origin missing receive_id")
            try:
                await self.feishu_client.send_markdown_reply(
                    receive_id,
                    content,
                    receive_id_type,
                )
            except Exception as exc:
                return DeliveryResult("failed", str(exc))
            return DeliveryResult("success")
        return DeliveryResult("failed", f"unsupported platform: {platform.value}")
