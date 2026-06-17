from pathlib import Path

from app.config import Settings
from app.domain.platform import Platform, PlatformKind
from app.main import build_application_services


def _settings(tmp_path: Path, *, feishu_enabled: bool) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=feishu_enabled,
        feishu_app_id="cli_a123456",
        feishu_app_secret="secret",
        feishu_tenant_key="tenant-1",
        feishu_allowed_open_ids=["ou_a", "ou_b"],
        feishu_allowed_chat_ids=["oc_a"],
    )


def test_build_application_services_wires_cli_only_platform_when_feishu_disabled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path, feishu_enabled=False))

    assert services.feishu_long_connection_gateway is None
    platforms = services.platform_registry.list()
    assert [descriptor.platform for descriptor in platforms] == [Platform.CLI]
    assert platforms[0].kind is PlatformKind.LOCAL
    assert services.platform_registry.get_lifecycle(Platform.FEISHU) is None


def test_build_application_services_wires_feishu_lifecycle_singleton(tmp_path: Path):
    services = build_application_services(_settings(tmp_path, feishu_enabled=True))

    feishu = services.platform_registry.get(Platform.FEISHU)
    assert feishu is not None
    assert feishu.kind is PlatformKind.IM
    assert feishu.config_summary == {
        "app_id_suffix": "cli_****",
        "tenant_key": "tenant-1",
        "allowed_open_id_count": 2,
        "allowed_chat_id_count": 1,
    }
    assert services.feishu_long_connection_gateway is not None
    assert services.platform_registry.get_lifecycle(Platform.FEISHU) is services.feishu_long_connection_gateway
