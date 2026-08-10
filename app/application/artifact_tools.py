"""T8: Artifact tool definitions (Application Layer).

8 Agent-native artifact tools exposed to普通 Chat (realtime). Each tool:
  - source_type=AGENT (conversational agent callable; hidden in unattended)
  - managed=False (no server-side permitted_managed_tools gating)
  - toolset="artifact"
  - realtime_only=True (ToolPolicy hides in SAFE_ONLY even when granted,
    spec: SAFE_ONLY grant 不得放开 artifact_*, 防递归/绕过审批)
  - input_schema additionalProperties=false; no session/run/actor/source_*
    provenance params (those come from trusted context, never arguments)

风险等级 (spec tool contract):
  - artifact_create / artifact_update / artifact_rollback: CONFIRM
  - artifact_publish: DANGEROUS (externally visible side effect; approvable
    in realtime via the approval card, never in unattended)
  - artifact_read / artifact_list / artifact_list_revisions / artifact_diff: SAFE

工具契约 (spec lines 37-44):
  - artifact_create:         name, kind, inline_content 或受控 workspace_ref,
                             summary, classification, labels
  - artifact_list:           当前会话隐式作用域, cursor, limit, 可选 kind/status
  - artifact_read:           artifact_id, 可选 revision_id, 可选文本范围
  - artifact_update:         artifact_id, expected_revision_id, 完整新内容或
                             text_patch, change_summary
  - artifact_list_revisions: artifact_id, cursor, limit
  - artifact_diff:           artifact_id, from_revision_id, to_revision_id,
                             context_lines
  - artifact_rollback:       artifact_id, target_revision_id, expected_revision_id,
                             change_summary
  - artifact_publish:        artifact_id, revision_id, expected_current_revision_id
"""
from __future__ import annotations

from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType


# 工具名常量，供 ArtifactToolExecutor 与测试引用
ARTIFACT_TOOL_CREATE = "artifact_create"
ARTIFACT_TOOL_LIST = "artifact_list"
ARTIFACT_TOOL_READ = "artifact_read"
ARTIFACT_TOOL_UPDATE = "artifact_update"
ARTIFACT_TOOL_LIST_REVISIONS = "artifact_list_revisions"
ARTIFACT_TOOL_DIFF = "artifact_diff"
ARTIFACT_TOOL_ROLLBACK = "artifact_rollback"
ARTIFACT_TOOL_PUBLISH = "artifact_publish"

ARTIFACT_TOOL_NAMES: frozenset[str] = frozenset({
    ARTIFACT_TOOL_CREATE,
    ARTIFACT_TOOL_LIST,
    ARTIFACT_TOOL_READ,
    ARTIFACT_TOOL_UPDATE,
    ARTIFACT_TOOL_LIST_REVISIONS,
    ARTIFACT_TOOL_DIFF,
    ARTIFACT_TOOL_ROLLBACK,
    ARTIFACT_TOOL_PUBLISH,
})

# ArtifactKind 枚举值 (app/domain/artifact.py)
_KIND_VALUES = [
    "document", "markdown", "code", "html", "data",
    "csv", "json", "image", "pdf", "text", "other",
]
# ArtifactStatus 枚举值
_STATUS_VALUES = ["draft", "published", "archived"]


def artifact_tool_definitions() -> list[ToolDefinition]:
    """返回 8 个 artifact ToolDefinition。

    source_type/toolset/managed/realtime_only 在所有工具上一致；差异在
    description、risk_level 与 input_schema。realtime_only=True 让 ToolPolicy
    在 SAFE_ONLY（unattended/cron）始终隐藏，即使误配 granted_tools 同名也不
    放开（spec 约束）。DANGEROUS（publish）在 DEFAULT 暴露但执行需审批通道。
    """
    common = dict(
        source_type=ToolSourceType.AGENT,
        toolset="artifact",
        managed=False,
        realtime_only=True,
    )
    return [
        ToolDefinition(
            name=ARTIFACT_TOOL_CREATE,
            description=(
                "Create a new artifact bound to the current session with an initial "
                "revision. Provide either inline_content (text) or a controlled "
                "workspace_ref (workspace: scheme). Returns the artifact_id, initial "
                "revision_id/revision_number, and the export capabilities actually "
                "supported by the initial content. Session/run/actor provenance is "
                "taken from trusted server context and cannot be set via arguments."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": _KIND_VALUES},
                    "inline_content": {"type": "string"},
                    "workspace_ref": {
                        "type": "string",
                        "description": "workspace:{relative_path} source descriptor",
                    },
                    "summary": {"type": "string"},
                    "classification": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "kind"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_LIST,
            description=(
                "List the current session's artifacts (implicit session scope). "
                "Returns lightweight references (no content or content_ref). Use to "
                "enumerate candidates when the user has multiple artifacts. Optional "
                "kind/status filters and cursor pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cursor": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "kind": {"type": "string", "enum": _KIND_VALUES},
                    "status": {"type": "string", "enum": _STATUS_VALUES},
                },
                "required": [],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_READ,
            description=(
                "Read a bounded slice of an artifact's content. Defaults to the "
                "current revision; specify revision_id to read a same-artifact "
                "historical revision (cross-artifact revision_id returns not_found). "
                "Text content is paginated by complete UTF-8 lines: line_offset (from "
                "0) and line_limit (1..500); response includes truncated and "
                "next_line_offset. The returned content is redacted (subject to "
                "InformationFlow cleaning before reaching the model context); do not "
                "construct text_patch from redacted fragments. Binary revisions return "
                "metadata only (size/mime/checksum), never content or content_ref."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "revision_id": {"type": "string"},
                    "line_offset": {"type": "integer", "minimum": 0},
                    "line_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_UPDATE,
            description=(
                "Create a new revision from updated content. expected_revision_id "
                "must equal the current revision (CAS); a stale value returns "
                "artifact_revision_conflict (retryable). Provide exactly one content "
                "input: content (full new text), workspace_ref (workspace: scheme), "
                "or text_patch (array of search/replace operations). kind/mime may "
                "change with full content; text_patch preserves the parent kind/mime. "
                "Returns new revision_id, revision_number, diff_summary, "
                "content_unchanged, and publish_sync_state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "expected_revision_id": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "workspace_ref": {"type": "string"},
                    "text_patch": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "1..100 search/replace operations",
                    },
                    "change_summary": {"type": "string"},
                    "kind": {"type": "string", "enum": _KIND_VALUES},
                    "mime": {"type": "string"},
                },
                "required": ["artifact_id", "expected_revision_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_LIST_REVISIONS,
            description=(
                "List revisions for an artifact (newest first). Returns revision "
                "summaries (id, number, checksum, size, change_summary, created_by, "
                "created_at, is_current, is_published). Cursor pagination; limit 1..100."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "cursor": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_DIFF,
            description=(
                "Compute a bounded unified diff between two revisions of the same "
                "artifact. context_lines 0..20 (default 3). Text pairs return a "
                "unified diff; binary pairs return a binary_changed summary; mixed "
                "text/binary returns artifact_diff_unsupported. The diff output is "
                "redacted (InformationFlow cleaned before model context)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "from_revision_id": {"type": "string", "minLength": 1},
                    "to_revision_id": {"type": "string", "minLength": 1},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
                },
                "required": ["artifact_id", "from_revision_id", "to_revision_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_ROLLBACK,
            description=(
                "Roll back to a target revision by creating a new revision whose "
                "content is copied from the target (the target is not mutated and "
                "intermediate revisions are not deleted). expected_revision_id must "
                "equal the current revision (CAS). Returns new revision_id, "
                "revision_number, diff_summary, content_unchanged, publish_sync_state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "target_revision_id": {"type": "string", "minLength": 1},
                    "expected_revision_id": {"type": "string", "minLength": 1},
                    "change_summary": {"type": "string"},
                },
                "required": ["artifact_id", "target_revision_id", "expected_revision_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            **common,
        ),
        ToolDefinition(
            name=ARTIFACT_TOOL_PUBLISH,
            description=(
                "Publish a specific revision to a public snapshot. revision_id and "
                "expected_current_revision_id must both equal the current revision; "
                "otherwise returns artifact_revision_conflict. This is a DANGEROUS, "
                "externally visible side effect and requires explicit approval. After "
                "approval, the active publish is atomically replaced. Returns "
                "publish_id, published_revision_id, share_url, reused."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "revision_id": {"type": "string", "minLength": 1},
                    "expected_current_revision_id": {"type": "string", "minLength": 1},
                },
                "required": ["artifact_id", "revision_id", "expected_current_revision_id"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.DANGEROUS,
            **common,
        ),
    ]
