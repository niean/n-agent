from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


_KB_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class KnowledgeBaseType(str, Enum):
    N_KB = "n_kb"
    RAGFLOW = "ragflow"


class KnowledgeProbeStatus(str, Enum):
    UNKNOWN = "unknown"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    name: str
    description: str
    base_type: KnowledgeBaseType
    base_url: str
    dataset_id: str
    api_key_present: bool
    enabled: bool
    default_top_k: int | None
    default_min_score: float | None
    last_probe_status: KnowledgeProbeStatus
    last_probe_error: str | None
    last_probed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class KnowledgeBaseSecret:
    kb_id: str
    api_key: str


@dataclass(frozen=True)
class KnowledgeSearchRequest:
    kb_id: str
    query: str
    top_k: int | None = None
    min_score: float | None = None


@dataclass(frozen=True)
class KnowledgeBackendSearchRequest:
    query: str
    top_k: int | None = None
    min_score: float | None = None


@dataclass(frozen=True)
class KnowledgeSnippet:
    id: str | None
    title: str | None
    content: str
    score: float | None
    source: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeSearchResult:
    kb_id: str
    kb_name: str
    base_type: KnowledgeBaseType
    query: str
    results: list[KnowledgeSnippet]


class KnowledgeBaseNotFoundError(Exception):
    """Knowledge base 不存在"""


class DuplicateKnowledgeBaseError(Exception):
    """Knowledge base 唯一约束冲突"""


class KnowledgeBaseValidationError(Exception):
    """Knowledge base 输入校验失败"""


class KnowledgeProbeError(Exception):
    """Knowledge base 探测失败"""


class KnowledgeSearchError(Exception):
    """Knowledge base 检索失败"""


def validate_kb_id(kb_id: str) -> str:
    if not _KB_ID_PATTERN.fullmatch(kb_id):
        raise KnowledgeBaseValidationError("kb_id must contain only lowercase letters, digits, underscores, and hyphens")
    return kb_id


class KnowledgeBaseRegistry(Protocol):
    async def list_bases(self) -> list[KnowledgeBase]:
        ...

    async def get_base(self, kb_id: str) -> KnowledgeBase | None:
        ...

    async def create_base(self, base: KnowledgeBase, api_key: str | None = None) -> KnowledgeBase:
        ...

    async def update_base(
        self,
        kb_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        base_type: KnowledgeBaseType | None = None,
        base_url: str | None = None,
        dataset_id: str | None = None,
        enabled: bool | None = None,
        default_top_k: int | None = None,
        default_min_score: float | None = None,
        clear_default_top_k: bool = False,
        clear_default_min_score: bool = False,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> KnowledgeBase:
        ...

    async def delete_base(self, kb_id: str) -> None:
        ...

    async def get_secret(self, kb_id: str) -> str | None:
        ...

    async def update_probe_status(
        self,
        kb_id: str,
        status: KnowledgeProbeStatus,
        error: str | None = None,
        probed_at: datetime | None = None,
    ) -> None:
        ...


class KnowledgeRetriever(Protocol):
    async def probe(self, base: KnowledgeBase, secret: KnowledgeBaseSecret | None = None) -> None:
        ...

    async def search(
        self,
        base: KnowledgeBase,
        request: KnowledgeBackendSearchRequest,
        secret: KnowledgeBaseSecret | None = None,
    ) -> KnowledgeSearchResult:
        ...


class KnowledgeRetrieverFactory(Protocol):
    def get(self, base_type: KnowledgeBaseType) -> KnowledgeRetriever:
        ...
