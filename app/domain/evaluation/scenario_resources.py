"""场景资源领域模型（plan T4，spec 6.1/6.2 资源生命周期）。

资源账目的封闭词表与不可变 VO：intent -> owned/unknown ->
released/retained/cleanup_failed 的独立资源状态（不与执行 phase 共用
词表）；释放顺序、幂等业务键与精确 refs 在构造期校验。

资源记录只存引用和有界摘要（spec 6.2）：不保存答案正文/永久副本，
ref 携带定位信息（路径/外部 ID），文件资源必须携带期望 hash 供删前
复核，进程资源必须携带创建身份供防 PID 复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_RESOURCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_:.\-]{0,199}$")
_PROCESS_IDENTITY_RE = re.compile(r"^pid=\d+:start=\d+(\.\d+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _RESOURCE_KEY_RE.match(value):
        raise ScenarioResourceError(
            f"{field} 必须匹配 {_RESOURCE_KEY_RE.pattern}: {value!r}"
        )


def contract_identifier(value: str) -> str:
    """自由文本 external_id 进入 business_key 前的确定性编码。

    真实外部 ID 形状不受标识符契约约束（如制品 ID 是 base64url，
    含大写字母）；满足契约的原样返回（business_key 保持可读），
    否则返回 sha256 十六进制（必满足契约，跨重启稳定，幂等回查
    不受影响——回查按记录携带的 external_id 进行）。"""
    if isinstance(value, str) and _RESOURCE_KEY_RE.match(value):
        return value
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class ScenarioResourceError(ValueError):
    """场景资源 VO 构造期校验错误。"""


RESOURCE_KINDS: tuple[str, ...] = (
    "session",
    "cli_conversation",
    "acp_process",
    "sandbox",
    "browser",
    "task",
    "artifact",
    "workspace_file",
    "memory_binding",
)

RESOURCE_STATES: tuple[str, ...] = (
    "intent",
    "owned",
    "unknown",
    "released",
    "retained",
    "cleanup_failed",
)

# 释放顺序分层（spec 6.1）：先停止/取消仍在写入的 task/子进程并回读
# 终态，再关闭浏览器/释放 sandbox，最后处理存储数据。数值小者先释放。
RELEASE_ORDER: dict[str, int] = {
    "task": 0,
    "acp_process": 0,
    "browser": 1,
    "sandbox": 1,
    "session": 2,
    "cli_conversation": 2,
    "artifact": 2,
    "workspace_file": 2,
    "memory_binding": 2,
}

# 保留期适用的存储类资源（活跃资源必须及时停止释放，不受保留期保护）
RETENTION_ELIGIBLE_KINDS: frozenset[str] = frozenset({
    "session",
    "cli_conversation",
    "artifact",
    # workspace_file 不保留：settle 时经删除端口 hash 复核删除
    # （清理时 worker 仍写入 -> hash_changed 拒删，T10）
    "memory_binding",
})


@dataclass(frozen=True)
class ResourceIntent:
    """副作用前登记的资源意图（唯一业务幂等键 + 精确归属）。

    business_key 形如 ``{execution_id}:{case_id}:{resource_kind}:{label}``，
    是断网/崩溃后回查副作用的唯一依据——存在 intent 就假定副作用可能
    已发生，禁止以 side_effects=[] 判定"无副作用"后重试创建。
    """

    business_key: str
    execution_id: str
    case_id: str
    resource_kind: str
    release_rule: str
    retention_rule: str
    external_id: str | None = None
    ref: str | None = None
    expected_sha256: str | None = None
    process_identity: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.business_key, "business_key")
        _require_identifier(self.execution_id, "execution_id")
        _require_identifier(self.case_id, "case_id")
        if self.resource_kind not in RESOURCE_KINDS:
            raise ScenarioResourceError(
                f"resource_kind 必须在封闭集合内: {self.resource_kind!r}"
            )
        _require_identifier(self.release_rule, "release_rule")
        _require_identifier(self.retention_rule, "retention_rule")
        if self.external_id is not None and not isinstance(self.external_id, str):
            raise ScenarioResourceError("external_id 必须是字符串或 None")
        if self.ref is not None and (
            not isinstance(self.ref, str) or len(self.ref) > 512
        ):
            raise ScenarioResourceError(
                "ref 必须是 <=512 字符的定位引用（有界摘要，不存正文）"
            )
        if self.expected_sha256 is not None and not _SHA256_RE.match(
            self.expected_sha256
        ):
            raise ScenarioResourceError(
                f"expected_sha256 必须是 64 位十六进制: {self.expected_sha256!r}"
            )
        if self.process_identity is not None and not _PROCESS_IDENTITY_RE.match(
            self.process_identity
        ):
            raise ScenarioResourceError(
                f"process_identity 必须是 pid=<n>:start=<ts> 形状: "
                f"{self.process_identity!r}"
            )
        # 类别专属不变量：文件必须携带期望 hash（删前复核依据）；
        # 进程必须携带创建身份（防 PID 复用误杀）
        if self.resource_kind == "workspace_file" and (
            not self.expected_sha256 or not self.ref
        ):
            raise ScenarioResourceError(
                "workspace_file intent 必须携带 ref 与 expected_sha256"
                "（删除前复核根/类型/hash）"
            )
        if self.resource_kind == "acp_process" and not self.process_identity:
            raise ScenarioResourceError(
                "acp_process intent 必须携带 process_identity（防 PID 复用）"
            )


@dataclass(frozen=True)
class ResourceRecord:
    """资源账目行（store 投影）：intent + 当前状态与归属信息。"""

    resource_id: str
    business_key: str
    execution_id: str
    case_id: str
    resource_kind: str
    release_order: int
    release_rule: str
    retention_rule: str
    state: str
    external_id: str | None
    ref: str | None
    expected_sha256: str | None
    process_identity: str | None
    retained_until: float | None
    error_code: str | None
    owner_token: str | None
    lease_until: float | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise ScenarioResourceError("resource_id 不能为空")
        if self.state not in RESOURCE_STATES:
            raise ScenarioResourceError(
                f"state 必须在封闭集合内: {self.state!r}"
            )
        if self.resource_kind not in RESOURCE_KINDS:
            raise ScenarioResourceError(
                f"resource_kind 必须在封闭集合内: {self.resource_kind!r}"
            )
