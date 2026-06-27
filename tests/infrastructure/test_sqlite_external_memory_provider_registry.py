# tests/infrastructure/test_sqlite_external_memory_provider_registry.py
import pytest
from datetime import datetime
from app.domain.external_memory_provider import (
    ExternalMemoryProviderType, ExternalMemoryProbeStatus,
    ExternalMemoryProviderNotFoundError, DuplicateExternalMemoryProviderError,
)
from app.infrastructure.registry.sqlite_external_memory_provider_registry import (
    SQLiteExternalMemoryProviderRegistry,
)


@pytest.fixture
def registry(tmp_path):
    r = SQLiteExternalMemoryProviderRegistry(tmp_path / "test.db")
    r.create_tables()
    return r


def test_create_and_list(registry):
    cfg = registry.create_provider(
        id="p1", name="my-mem0",
        provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://app.mem0.ai/v1",
        api_key="sk-xxx", enabled=False, extra_config={"user_id": "u1"},
    )
    assert cfg.api_key_present is True
    assert cfg.enabled is False
    items = registry.list_providers()
    assert len(items) == 1
    assert items[0].name == "my-mem0"
    # api_key 不出现在 config 字段
    assert not hasattr(items[0], "api_key")


def test_get_secret(registry):
    registry.create_provider(
        id="p1", name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="u", api_key="sk-xxx", enabled=False, extra_config={},
    )
    secret = registry.get_secret("p1")
    assert secret.api_key == "sk-xxx"


def test_update_api_key_three_states(registry):
    registry.create_provider(
        id="p1", name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="u", api_key="sk-1", enabled=False, extra_config={},
    )
    # null 不变
    cfg = registry.update_provider("p1", base_url="u2")
    assert cfg.api_key_present is True
    assert registry.get_secret("p1").api_key == "sk-1"
    # 非空覆盖
    registry.update_provider("p1", api_key="sk-2")
    assert registry.get_secret("p1").api_key == "sk-2"
    # "" 清空
    registry.update_provider("p1", api_key="")
    assert registry.get_secret("p1").api_key is None
    assert registry.get_provider("p1").api_key_present is False


def test_duplicate_name_rejected(registry):
    registry.create_provider(
        id="p1", name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="u", api_key=None, enabled=False, extra_config={},
    )
    with pytest.raises(DuplicateExternalMemoryProviderError):
        registry.create_provider(
            id="p2", name="m", provider_type=ExternalMemoryProviderType.HONCHO,
            base_url="u", api_key=None, enabled=False, extra_config={},
        )


def test_at_most_one_enabled_on_create(registry):
    registry.create_provider(
        id="p1", name="m1", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="u", api_key="k", enabled=True, extra_config={},
    )
    with pytest.raises(Exception):  # ExternalMemoryProviderValidationError
        registry.create_provider(
            id="p2", name="m2", provider_type=ExternalMemoryProviderType.HONCHO,
            base_url="u", api_key="k", enabled=True, extra_config={},
        )


def test_delete_missing_raises(registry):
    with pytest.raises(ExternalMemoryProviderNotFoundError):
        registry.delete_provider("missing")


def test_save_probe_status(registry):
    registry.create_provider(
        id="p1", name="m", provider_type=ExternalMemoryProviderType.MEM0,
        base_url="u", api_key=None, enabled=False, extra_config={},
    )
    registry.save_probe_status("p1", ExternalMemoryProbeStatus.OK)
    cfg = registry.get_provider("p1")
    assert cfg.probe_status == ExternalMemoryProbeStatus.OK
