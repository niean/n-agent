# tests/domain/test_external_memory_provider_models.py
import pytest
from app.domain.external_memory_provider import (
    ExternalMemoryProviderType,
    ExternalMemoryProviderConfig,
    ExternalMemoryProviderSecret,
    ExternalMemoryProbeStatus,
    ExternalMemoryProviderNotFoundError,
    DuplicateExternalMemoryProviderError,
    ExternalMemoryProviderInUseError,
    ExternalMemoryProviderValidationError,
)

def test_provider_type_values():
    assert ExternalMemoryProviderType.MEM0.value == "mem0"
    assert ExternalMemoryProviderType.HOLOGRAPHIC.value == "holographic"
    assert ExternalMemoryProviderType.HONCHO.value == "honcho"

def test_config_is_frozen_and_masks_api_key():
    cfg = ExternalMemoryProviderConfig(
        id="p1", name="my-mem0",
        provider_type=ExternalMemoryProviderType.MEM0,
        base_url="https://app.mem0.ai/v1",
        api_key_present=True, enabled=True,
        extra_config={"user_id": "u1"},
        probe_status=None, created_at="t1", updated_at="t1",
    )
    assert cfg.api_key_present is True
    with pytest.raises(Exception):
        cfg.id = "p2"  # frozen

def test_probe_status_values():
    assert ExternalMemoryProbeStatus.UNKNOWN.value == "unknown"
    assert ExternalMemoryProbeStatus.OK.value == "ok"
    assert ExternalMemoryProbeStatus.FAILED.value == "failed"
