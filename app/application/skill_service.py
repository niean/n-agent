from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillNotFoundError,
    SkillReadiness,
    SkillRegistry,
    SkillValidationError,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


@dataclass(frozen=True)
class SkillScanWarning:
    relative_path: str
    reason: str
    detail: str | None = None
    first_path: str | None = None


@dataclass(frozen=True)
class SkillScanReport:
    skills_count: int
    warnings: list[SkillScanWarning] = field(default_factory=list)


@dataclass(frozen=True)
class SkillInput:
    name: str
    relative_path: str
    description: str = ""
    platforms: list[str] = field(default_factory=list)
    enabled: bool = True
    readiness: SkillReadiness = SkillReadiness.AVAILABLE
    frontmatter: dict[str, Any] = field(default_factory=dict)


class SkillFileLoaderProtocol(Protocol):
    async def scan(self) -> tuple[list[Skill], list[Any]]: ...
    async def render(self, skill: Skill, session_id: str = "") -> str: ...
    async def read_linked_file(self, skill: Skill, file_path: str) -> str: ...
    async def list_linked_files(self, skill: Skill) -> dict[str, list[str]]: ...


class SkillService:
    def __init__(self, registry: SkillRegistry, loader: SkillFileLoaderProtocol):
        self.registry = registry
        self.loader = loader

    async def list_skills(self, include_disabled: bool = True) -> list[Skill]:
        return await self.registry.list_skills(include_disabled=include_disabled)

    async def list_for_llm(self) -> list[Skill]:
        return [
            s for s in await self.registry.list_skills(include_disabled=False)
            if s.readiness is SkillReadiness.AVAILABLE
        ]

    async def get(self, name: str) -> Skill:
        skill = await self.registry.get_skill(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return skill

    async def create_skill(self, payload: SkillInput) -> Skill:
        existing = await self.registry.get_skill(payload.name.strip())
        if existing is not None:
            raise SkillValidationError("skill name already exists")
        skill = _skill_from_input(payload)
        return await self.registry.upsert_skill(skill)

    async def update_skill(self, name: str, payload: SkillInput) -> Skill:
        current = await self.registry.get_skill(name)
        if current is None:
            raise SkillNotFoundError(name)
        if payload.name.strip() != name:
            raise SkillValidationError("skill name cannot be changed")
        updated = _skill_from_input(payload, current=current)
        return await self.registry.upsert_skill(updated)

    async def delete_skill(self, name: str) -> None:
        if not await self.registry.delete_skill(name):
            raise SkillNotFoundError(name)

    async def set_enabled(self, name: str, enabled: bool) -> Skill:
        skill = await self.registry.get_skill(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return await self.registry.set_enabled(name, enabled)

    async def scan_now(self) -> SkillScanReport:
        skills, warnings = await self.loader.scan()
        await self.registry.replace_all_skills(skills)
        normalized = [_normalize_warning(w) for w in warnings]
        return SkillScanReport(skills_count=len(skills), warnings=normalized)

    async def render_view(self, name: str, session_id: str = "") -> dict[str, Any]:
        skill = await self.registry.get_skill(name)
        if skill is None:
            available = [
                s.name for s in (await self.registry.list_skills(include_disabled=False))[:20]
            ]
            return {"success": False, "error": "skill not found", "available": available}
        if not skill.enabled:
            return {
                "success": False,
                "error": f"Skill '{name}' is disabled.",
                "readiness": skill.readiness.value,
            }
        if skill.readiness is SkillReadiness.UNSUPPORTED:
            return {
                "success": False,
                "error": f"Skill '{name}' is not supported on this platform.",
                "readiness": "unsupported",
            }
        content = await self.loader.render(skill, session_id=session_id)
        linked = await self.loader.list_linked_files(skill)
        return {
            "success": True,
            "name": skill.name,
            "content": content,
            "description": skill.description,
            "readiness": skill.readiness.value,
            "linked_files": linked,
        }

    async def render_linked_file(self, name: str, file_path: str) -> dict[str, Any]:
        skill = await self.registry.get_skill(name)
        if skill is None:
            return {"success": False, "error": "skill not found"}
        if not skill.enabled:
            return {
                "success": False,
                "error": f"Skill '{name}' is disabled.",
                "readiness": skill.readiness.value,
            }
        if skill.readiness is SkillReadiness.UNSUPPORTED:
            return {
                "success": False,
                "error": f"Skill '{name}' is not supported on this platform.",
                "readiness": "unsupported",
            }
        try:
            content = await self.loader.read_linked_file(skill, file_path)
        except SkillValidationError as exc:
            return {"success": False, "error": str(exc)}
        except FileNotFoundError:
            available = await self.loader.list_linked_files(skill)
            return {"success": False, "error": "file not found", "available_files": available}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "name": skill.name, "file": file_path, "content": content}


def _normalize_warning(w: Any) -> SkillScanWarning:
    if isinstance(w, SkillScanWarning):
        return w
    return SkillScanWarning(
        relative_path=getattr(w, "relative_path", ""),
        reason=getattr(w, "reason", "unknown"),
        detail=getattr(w, "detail", None),
        first_path=getattr(w, "first_path", None),
    )


def _skill_from_input(payload: SkillInput, current: Skill | None = None) -> Skill:
    name = payload.name.strip()
    relative_path = payload.relative_path.strip()
    _validate_skill_input(name, relative_path, payload.platforms, payload.frontmatter)
    raw = dict(payload.frontmatter or {})
    raw["name"] = name
    raw["description"] = payload.description
    raw["platforms"] = list(payload.platforms)
    fm = SkillFrontmatter(
        name=name,
        description=payload.description,
        version=str(raw.get("version") or ""),
        platforms=list(payload.platforms),
        tags=list(raw.get("tags") or []),
        related_skills=list(raw.get("related_skills") or []),
        author=str(raw.get("author") or ""),
        license=str(raw.get("license") or ""),
        setup_help=raw.get("setup_help"),
        required_env_vars=list(raw.get("required_env_vars") or []),
        raw=raw,
    )
    return Skill(
        id=current.id if current else str(uuid4()),
        name=name,
        relative_path=relative_path,
        description=payload.description,
        platforms=list(payload.platforms),
        frontmatter=fm,
        enabled=payload.enabled,
        readiness=payload.readiness,
        last_scan_status=current.last_scan_status if current else "manual",
        last_scan_error=current.last_scan_error if current else None,
        last_seen_at=current.last_seen_at if current else None,
        created_at=current.created_at if current else None,
        updated_at=current.updated_at if current else None,
    )


def _validate_skill_input(
    name: str,
    relative_path: str,
    platforms: list[str],
    frontmatter: dict[str, Any],
) -> None:
    if not name:
        raise SkillValidationError("name required")
    if "/" in name or "\\" in name:
        raise SkillValidationError("name must not contain path separators")
    if not relative_path:
        raise SkillValidationError("relative_path required")
    normalized = relative_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if normalized.startswith("/") or normalized.startswith("../") or ".." in parts:
        raise SkillValidationError("relative_path must stay under skills_root")
    if not normalized.endswith("SKILL.md"):
        raise SkillValidationError("relative_path must point to SKILL.md")
    if not isinstance(platforms, list) or any(not isinstance(item, str) for item in platforms):
        raise SkillValidationError("platforms must be a string list")
    if not isinstance(frontmatter, dict):
        raise SkillValidationError("frontmatter must be an object")


def _skill_category(skill: Skill) -> str:
    """Hermes 用 skill 目录的第一段（如 mlops/evaluation/wandb -> mlops）作为 category；
    若 frontmatter 显式提供 category 则优先使用之。"""
    raw_cat = skill.frontmatter.raw.get("category") if skill.frontmatter.raw else None
    if isinstance(raw_cat, str) and raw_cat.strip():
        return raw_cat.strip()
    parts = (skill.relative_path or "").split("/")
    if len(parts) >= 2:
        return parts[0]
    return ""


def skill_tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="skills_list",
            description="List available skills (only enabled and platform-supported).",
            input_schema={
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.BUILTIN,
            toolset="skills",
        ),
        ToolDefinition(
            name="skill_view",
            description="View a skill's SKILL.md content (with macro substitution) or a linked file under it.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "file_path": {"type": "string"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.BUILTIN,
            toolset="skills",
        ),
    ]


class SkillToolExecutor(ToolExecutor):
    def __init__(self, service: SkillService):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            payload: Any
            if request.name == "skills_list":
                category = request.arguments.get("category") if isinstance(request.arguments, dict) else None
                category = str(category).strip() if category else None
                skills = await self.service.list_for_llm()
                items = [
                    {
                        "name": s.name,
                        "description": s.description,
                        "category": _skill_category(s),
                    }
                    for s in skills
                ]
                if category:
                    items = [it for it in items if it["category"] == category]
                categories = sorted({it["category"] for it in items if it["category"]})
                payload = {
                    "success": True,
                    "skills": items,
                    "categories": categories,
                    "count": len(items),
                    "hint": "Use skill_view(name) to see full content, tags, and linked files",
                }
            elif request.name == "skill_view":
                name = str(request.arguments.get("name") or "")
                if not name:
                    payload = {"success": False, "error": "name required"}
                else:
                    file_path = request.arguments.get("file_path")
                    session_id = str((context and getattr(context, "session_id", "")) or "")
                    if file_path:
                        payload = await self.service.render_linked_file(name, str(file_path))
                    else:
                        payload = await self.service.render_view(name, session_id=session_id)
            else:
                payload = {"success": False, "error": f"unknown skill tool: {request.name}"}
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.SUCCESS,
                content=json.dumps(payload),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.ERROR,
                content=json.dumps({"success": False, "error": str(exc)}),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
