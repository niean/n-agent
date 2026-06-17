from __future__ import annotations

from app.domain.platform import Platform, PlatformDescriptor, PlatformKind
from app.infrastructure.registry.in_memory_platform_registry import InMemoryPlatformRegistry


class _StubLifecycle:
    def __init__(self, connected: bool, fatal: tuple[str, str] | None = None):
        self._connected = connected
        self._fatal = fatal

    def is_connected(self) -> bool:
        return self._connected

    def fatal_error(self) -> tuple[str, str] | None:
        return self._fatal


def _feishu_descriptor() -> PlatformDescriptor:
    return PlatformDescriptor(Platform.FEISHU, "飞书", PlatformKind.IM, {"app_id_suffix": "abcd****"})


def _cli_descriptor() -> PlatformDescriptor:
    return PlatformDescriptor(Platform.CLI, "CLI", PlatformKind.LOCAL, {})


def test_list_returns_registered_descriptors():
    registry = InMemoryPlatformRegistry([_feishu_descriptor(), _cli_descriptor()])

    platforms = {d.platform for d in registry.list()}

    assert platforms == {Platform.FEISHU, Platform.CLI}


def test_get_returns_specific_descriptor():
    feishu = _feishu_descriptor()
    registry = InMemoryPlatformRegistry([feishu])

    assert registry.get(Platform.FEISHU) is feishu
    assert registry.get(Platform.DINGTALK) is None


def test_get_lifecycle_returns_registered_reference():
    feishu = _feishu_descriptor()
    lifecycle = _StubLifecycle(connected=True)
    registry = InMemoryPlatformRegistry([feishu], {Platform.FEISHU: lifecycle})

    assert registry.get_lifecycle(Platform.FEISHU) is lifecycle


def test_get_lifecycle_returns_none_when_descriptor_only():
    registry = InMemoryPlatformRegistry([_feishu_descriptor()])

    assert registry.get_lifecycle(Platform.FEISHU) is None
