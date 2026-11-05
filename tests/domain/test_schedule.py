from app.domain.schedule import DeliveryTarget, DeliveryTargetType


def test_delivery_target_factories_copy_context_and_preserve_origin():
    context = {"platform": "feishu", "receive_id": "oc_1", "receive_id_type": "chat_id"}
    target = DeliveryTarget.feishu(context)
    assert target.target_type is DeliveryTargetType.FEISHU
    assert target.target_type.value == "feishu"
    assert target.context == context
    assert target.context is not context
    origin = DeliveryTarget.origin(context)
    context["receive_id"] = "changed"
    assert target.context["receive_id"] == origin.context["receive_id"] == "oc_1"
    assert origin.target_type is DeliveryTargetType.ORIGIN
    assert DeliveryTarget.dashboard().context == DeliveryTarget.silent().context == {}


def test_gateway_conversation_old_constructor_defaults_address_type_to_none():
    from datetime import datetime, timezone
    from app.domain.gateway import GatewayConversation
    from app.domain.platform import Platform

    now = datetime.now(timezone.utc)
    conversation = GatewayConversation("id", Platform.FEISHU, "oc_1", "", "Chat", None, now, now)
    assert conversation.receive_id_type is None


def test_scheduled_delivery_authorization_requires_every_verified_fact():
    from itertools import product
    from app.domain.policy import PolicyOutcome
    from app.domain.schedule import ScheduledDeliveryAuthorizationPolicy, ScheduledDeliveryAuthorizationRequest

    policy = ScheduledDeliveryAuthorizationPolicy()
    for platform, address_type, verified, matches, available in product(
        ('feishu', 'other'), ('chat_id', 'open_id'), (True, False), (True, False), (True, False)
    ):
        result = policy.evaluate(ScheduledDeliveryAuthorizationRequest(platform, address_type, verified, matches, available))
        allowed = platform == 'feishu' and address_type == 'chat_id' and verified and matches and available
        assert result.outcome is (PolicyOutcome.ALLOW if allowed else PolicyOutcome.DENY)
        assert result.reason == ('scheduled_delivery_authorized' if allowed else 'scheduled_delivery_denied')
