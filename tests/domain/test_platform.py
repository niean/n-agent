from __future__ import annotations

import pytest

from app.domain.platform import Platform, PlatformDescriptor, PlatformKind


def test_platform_enum_values():
    assert Platform.CLI.value == "cli"
    assert Platform.FEISHU.value == "feishu"
    assert Platform.DINGTALK.value == "dingtalk"
    assert Platform.WECOM.value == "wecom"


def test_platform_kind_values():
    assert PlatformKind.IM.value == "im"
    assert PlatformKind.LOCAL.value == "local"


def test_platform_descriptor_immutable():
    descriptor = PlatformDescriptor(
        platform=Platform.FEISHU,
        display_name="飞书",
        kind=PlatformKind.IM,
        config_summary={"app_id_suffix": "1234****"},
    )
    assert descriptor.platform is Platform.FEISHU
    assert descriptor.display_name == "飞书"
    assert descriptor.kind is PlatformKind.IM
    assert descriptor.config_summary == {"app_id_suffix": "1234****"}
    with pytest.raises(Exception):
        descriptor.display_name = "x"  # type: ignore[misc]


def test_platform_descriptor_default_config_summary():
    descriptor = PlatformDescriptor(Platform.CLI, "CLI", PlatformKind.LOCAL)
    assert descriptor.config_summary == {}
