from datetime import datetime, timezone

import pytest

from app.domain.knowledge import (
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseRegistry,
    KnowledgeBaseType,
    KnowledgeBaseValidationError,
    KnowledgeProbeStatus,
    KnowledgeSearchRequest,
    validate_kb_id,
)


def test_knowledge_base_type_values_exist():
    assert KnowledgeBaseType("n_kb") is KnowledgeBaseType.N_KB
    assert KnowledgeBaseType("ragflow") is KnowledgeBaseType.RAGFLOW


def test_knowledge_probe_status_values_exist():
    assert KnowledgeProbeStatus.UNKNOWN.value == "unknown"
    assert KnowledgeProbeStatus.SUCCESS.value == "success"
    assert KnowledgeProbeStatus.FAILED.value == "failed"


def test_knowledge_base_exposes_api_key_present_without_api_key_field():
    now = datetime.now(timezone.utc)
    base = KnowledgeBase(
        id="engineering-docs_1",
        name="Engineering Docs",
        description="Engineering documentation",
        base_type=KnowledgeBaseType.N_KB,
        base_url="https://kb.example.test",
        dataset_id="engineering",
        api_key_present=True,
        enabled=True,
        default_top_k=5,
        default_min_score=0.2,
        last_probe_status=KnowledgeProbeStatus.UNKNOWN,
        last_probe_error=None,
        last_probed_at=None,
        created_at=now,
        updated_at=now,
    )

    assert base.api_key_present is True
    assert not hasattr(base, "api_key")


def test_validate_kb_id_accepts_lowercase_slug_with_dash_and_underscore():
    assert validate_kb_id("engineering-docs_1") == "engineering-docs_1"


def test_validate_kb_id_rejects_bad_id():
    with pytest.raises(KnowledgeBaseValidationError):
        validate_kb_id("Bad ID")


def test_search_request_separates_llm_kb_selection_from_backend_request():
    llm_request = KnowledgeSearchRequest(kb_id="engineering-docs", query="python", top_k=3, min_score=0.4)
    backend_request = KnowledgeBackendSearchRequest(
        query=llm_request.query,
        top_k=llm_request.top_k,
        min_score=llm_request.min_score,
    )

    assert llm_request.kb_id == "engineering-docs"
    assert not hasattr(backend_request, "kb_id")


def test_registry_update_base_can_clear_nullable_defaults():
    annotations = KnowledgeBaseRegistry.update_base.__annotations__

    assert annotations["clear_default_top_k"] == "bool"
    assert annotations["clear_default_min_score"] == "bool"
