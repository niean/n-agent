from __future__ import annotations

import asyncio
import json
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

import yaml

from app.domain.skill import (
    Skill,
    SkillBackupError,
    SkillFrontmatter,
    SkillManageRequest,
    SkillManageResult,
    SkillNotFoundError,
    SkillPatchConflictError,
    SkillPendingWrite,
    SkillReadiness,
    SkillRegistry,
    SkillSource,
    SkillUsage,
    SkillValidationError,
    SkillWriteAction,
    SkillWriteError,
    SkillWriteOrigin,
)
from app.domain.skill_format import (
    SkillFormatError,
    SkillFormatRequest,
    SkillFormatValidator,
    deserialize_metadata_list,
    normalize_frontmatter,
    skill_frontmatter_from_dict,
)
from app.domain.policy import PolicyOutcome
from app.domain.skill_policy import SkillPolicyRequest
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
class SkillScriptBytes:
    """Immutable, path-free facts for a verified linked Skill script."""

    skill_name: str
    script_relative_path: str
    content: bytes
    sha256: str


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
    async def read_script_bytes(self, skill: Skill, script_relative_path: str) -> bytes: ...
    async def read_skill_file(self, skill: Skill) -> str: ...
    async def write_skill_file(self, skill: Skill, content: str) -> None: ...
    async def patch_skill_file(self, skill: Skill, old_string: str, new_string: str) -> None: ...
    async def delete_skill(self, skill: Skill) -> None: ...
    async def write_linked_file(self, skill: Skill, file_path: str, content: str) -> None: ...
    async def remove_linked_file(self, skill: Skill, file_path: str) -> None: ...


# Redefined locally to avoid Application -> Infrastructure import leak.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore (all )?previous instructions", re.I),
    re.compile(r"system\s*:\s*you are", re.I),
)


class SkillManageRequestBuilder:
    """Build SkillManageRequest instances for each write action type."""

    @classmethod
    def create(
        cls,
        name: str,
        content: str,
        origin: SkillWriteOrigin,
        category: str = "",
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.CREATE,
            name=name,
            origin=origin,
            content=content,
            category=category,
        )

    @classmethod
    def patch(
        cls,
        name: str,
        old_string: str,
        new_string: str,
        origin: SkillWriteOrigin,
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.PATCH,
            name=name,
            origin=origin,
            old_string=old_string,
            new_string=new_string,
        )

    @classmethod
    def edit(
        cls,
        name: str,
        content: str,
        origin: SkillWriteOrigin,
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.EDIT,
            name=name,
            origin=origin,
            content=content,
        )

    @classmethod
    def delete(
        cls,
        name: str,
        origin: SkillWriteOrigin,
        absorbed_into: str = "",
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.DELETE,
            name=name,
            origin=origin,
            absorbed_into=absorbed_into,
        )

    @classmethod
    def write_file(
        cls,
        name: str,
        file_path: str,
        file_content: str,
        origin: SkillWriteOrigin,
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.WRITE_FILE,
            name=name,
            origin=origin,
            file_path=file_path,
            file_content=file_content,
        )

    @classmethod
    def remove_file(
        cls,
        name: str,
        file_path: str,
        origin: SkillWriteOrigin,
    ) -> SkillManageRequest:
        return SkillManageRequest(
            action=SkillWriteAction.REMOVE_FILE,
            name=name,
            origin=origin,
            file_path=file_path,
        )


class SkillService:
    def __init__(
        self,
        registry: SkillRegistry,
        loader: SkillFileLoaderProtocol,
        usage: Any | None = None,
        pending: Any | None = None,
        backup: Any | None = None,
        policy: Any | None = None,
        write_approval: bool = False,
        guard_agent_created: bool = True,
        backup_enabled: bool = True,
        format_validator: SkillFormatValidator | None = None,
    ):
        self.registry = registry
        self.loader = loader
        self.usage = usage
        self.pending = pending
        self.backup = backup
        self.policy = policy
        self.write_approval = write_approval
        self.guard_agent_created = guard_agent_created
        self.backup_enabled = backup_enabled
        self.format_validator = format_validator
        self._bg_read_targets: set[str] = set()

    async def list_skills(self, include_disabled: bool = True) -> list[Skill]:
        return await self.registry.list_skills(include_disabled=include_disabled)

    async def list_for_llm(self) -> list[Skill]:
        return [
            s for s in await self.registry.list_skills(include_disabled=False)
            if s.readiness is SkillReadiness.AVAILABLE
        ]

    async def list_chat_selectable(self) -> list[Skill]:
        """Skills exposed in the chat input skill popover.

        Filters list_for_llm with the per-skill ``chat_selectable`` toggle so
        admins can hide individual Skills from the dialog without disabling
        them globally or removing them from the LLM-facing index.
        """
        return [
            s for s in await self.list_for_llm()
            if s.chat_selectable
        ]

    async def build_skills_index(self) -> str:
        """Build a compact skill index section for the system prompt.

        Returns a ``## Available Skills`` markdown section grouping skills by category
        (name + description), matching the titled-section convention used by every other
        system-prompt block. Returns empty string when no skills are available, so the
        caller can skip injection.
        """
        skills = await self.list_for_llm()
        if not skills:
            return ""
        by_category: dict[str, list[tuple[str, str]]] = {}
        for s in skills:
            cat = _skill_category(s) or "general"
            by_category.setdefault(cat, []).append((s.name, s.description or ""))
        lines: list[str] = []
        for category in sorted(by_category.keys()):
            lines.append(f"- {category}:")
            for name, desc in sorted(by_category[category], key=lambda x: x[0]):
                if desc:
                    lines.append(f"  - {name}: {desc}")
                else:
                    lines.append(f"  - {name}")
        return "## Available Skills\n\n" + "\n".join(lines)

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

    async def set_chat_selectable(self, name: str, value: bool) -> Skill:
        skill = await self.registry.get_skill(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return await self.registry.set_chat_selectable(name, value)

    async def scan_now(self) -> SkillScanReport:
        skills, warnings = await self.loader.scan()
        await self.registry.replace_all_skills(skills)
        normalized = [_normalize_warning(w) for w in warnings]
        return SkillScanReport(skills_count=len(skills), warnings=normalized)

    async def resolve_script_bytes(
        self, skill_name: str, script_relative_path: str
    ) -> SkillScriptBytes:
        """Resolve bytes only for a ready, enabled, successfully scanned Skill."""
        skill = await self.registry.get_skill(skill_name)
        if (
            skill is None
            or skill.name != skill_name
            or not skill.enabled
            or skill.readiness is not SkillReadiness.AVAILABLE
            or skill.last_scan_status != "ok"
            or skill.last_scan_error is not None
        ):
            raise SkillNotFoundError("skill_script_not_found")
        try:
            content = await self.loader.read_script_bytes(skill, script_relative_path)
        except FileNotFoundError as exc:
            raise SkillNotFoundError("skill_script_not_found") from exc
        digest = hashlib.sha256(content).hexdigest()
        return SkillScriptBytes(
            skill_name=skill.name,
            script_relative_path=script_relative_path,
            content=bytes(content),
            sha256=digest,
        )

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

    # ------------------------------------------------------------------
    # manage_skill: unified write orchestration
    # ------------------------------------------------------------------

    def mark_bg_read(self, skill_name: str, file_path: str | None = None) -> None:
        """Record that a background-review fork has loaded the exact target
        via skill_view.  T10/T11 will wire this into the fork's skill_view path.
        """
        self._bg_read_targets.add(skill_name)

    async def manage_skill(
        self, request: SkillManageRequest
    ) -> SkillManageResult:
        """Unified write entry-point with policy, guard, backup, and staging."""
        if (
            self.usage is None
            or self.pending is None
            or self.backup is None
            or self.policy is None
        ):
            raise RuntimeError(
                "manage_skill requires usage, pending, backup, and policy"
            )

        action = request.action
        name = request.name
        origin = request.origin

        # -- Step 1: resolve target skill ----------------------------------
        existing = await self.registry.get_skill(name)
        name_exists = existing is not None
        target_source = existing.source if existing is not None else None

        pinned = False
        if self.usage is not None:
            maybe = self.usage.get(name)
            if asyncio.iscoroutine(maybe):
                usage_rec = await maybe
                if usage_rec is not None:
                    pinned = usage_rec.pinned

        if origin == SkillWriteOrigin.BACKGROUND_REVIEW:
            exact_target_loaded = name in self._bg_read_targets
        else:
            exact_target_loaded = True

        # create requires name absent; other actions require name present
        if action == SkillWriteAction.CREATE and name_exists:
            return SkillManageResult(
                success=False, staged=False, pending_id=None,
                skill_name=name, action=action,
                summary="", diff=None, error="skill name already exists",
            )
        if action != SkillWriteAction.CREATE and not name_exists:
            return SkillManageResult(
                success=False, staged=False, pending_id=None,
                skill_name=name, action=action,
                summary="", diff=None, error="skill not found",
            )

        # -- Step 1.5: validate action payload (reject empty/destructive writes)
        # EDIT/CREATE replace the whole SKILL.md with ``content``; an empty
        # content would wipe the file (e.g. an LLM confusing EDIT's ``content``
        # with PATCH's ``old_string`` and omitting ``content`` entirely). PATCH
        # with an empty ``old_string`` cannot match anything. Reject these
        # before any backup/write so the existing skill is never destroyed.
        if action in {SkillWriteAction.CREATE, SkillWriteAction.EDIT}:
            if not (request.content or "").strip():
                return SkillManageResult(
                    success=False, staged=False, pending_id=None,
                    skill_name=name, action=action,
                    summary="", diff=None,
                    error="content_required",
                )
        elif action == SkillWriteAction.PATCH:
            if not (request.old_string or "").strip():
                return SkillManageResult(
                    success=False, staged=False, pending_id=None,
                    skill_name=name, action=action,
                    summary="", diff=None,
                    error="old_string_required",
                )

        # -- Step 2: guard scan (pre-write injection rejection) ------------
        if self.guard_agent_created:
            should_scan = (
                target_source == SkillSource.AGENT
                or origin
                in {SkillWriteOrigin.FOREGROUND, SkillWriteOrigin.BACKGROUND_REVIEW}
            )
            if should_scan:
                scan_text = self._scan_text_for_action(request)
                if scan_text and self._detect_injection(scan_text):
                    return SkillManageResult(
                        success=False, staged=False, pending_id=None,
                        skill_name=name, action=action,
                        summary="", diff=None, error="injection_detected",
                    )

        # -- Step 2.5: format validation (pre-write frontmatter check) -----
        format_error = await self._validate_format_for_request(request, existing)
        if format_error is not None:
            return SkillManageResult(
                success=False, staged=False, pending_id=None,
                skill_name=name, action=action,
                summary="", diff=None, error=format_error,
            )

        # -- Step 3: policy evaluate ---------------------------------------
        policy_request = SkillPolicyRequest(
            target_source=target_source,
            action=action,
            origin=origin,
            pinned=pinned,
            name_exists=name_exists,
            write_approval_enabled=self.write_approval,
            approved_replay=request.approved_replay,
            exact_target_loaded=exact_target_loaded,
        )
        outcome = self.policy.evaluate(policy_request)

        # -- Step 4: dispatch on outcome -----------------------------------
        if outcome == PolicyOutcome.DENY:
            return SkillManageResult(
                success=False, staged=False, pending_id=None,
                skill_name=name, action=action,
                summary="", diff=None, error="policy_denied",
            )

        if (
            outcome == PolicyOutcome.REQUIRE_APPROVAL
            and not request.approved_replay
        ):
            summary = f"{action.value} {name}"
            diff = request.new_string or request.content or ""
            pw = SkillPendingWrite(
                pending_id="",
                action=action,
                skill_name=name,
                origin=origin,
                summary=summary,
                diff=diff,
                payload={
                    "action": action.value,
                    "name": name,
                    "content": request.content,
                    "old_string": request.old_string,
                    "new_string": request.new_string,
                    "file_path": request.file_path,
                    "file_content": request.file_content,
                    "category": request.category,
                },
                state="pending",
                error=None,
                created_at=None,
                updated_at=None,
            )
            staged = self.pending.stage(pw)
            if asyncio.iscoroutine(staged):
                pending_id = await staged
            else:
                pending_id = str(staged)
            return SkillManageResult(
                success=False, staged=True, pending_id=pending_id,
                skill_name=name, action=action,
                summary=summary, diff=diff, error=None,
            )

        # ALLOW or approved_replay -> execute write
        # -- Step 5: backup (fail-closed) ----------------------------------
        if self.backup_enabled:
            snap = self.backup.snapshot()
            if asyncio.iscoroutine(snap):
                try:
                    await snap
                except SkillBackupError as exc:
                    return SkillManageResult(
                        success=False, staged=False, pending_id=None,
                        skill_name=name, action=action,
                        summary="", diff=None,
                        error=f"backup_failed: {exc}",
                    )

        # -- Step 6: dispatch to loader ------------------------------------
        try:
            if action in {SkillWriteAction.CREATE, SkillWriteAction.EDIT}:
                skill_placeholder = self._build_skill_for_write(name, existing, origin)
                w = self.loader.write_skill_file(skill_placeholder, request.content)
                if asyncio.iscoroutine(w):
                    await w
                # Build authoritative Skill from normalized content so the
                # registry stays consistent with the disk-normalized
                # frontmatter (description/platforms/frontmatter.raw).
                upsert_skill = self._build_skill_from_content(
                    request.content, name, existing, origin
                )
                up = self.registry.upsert_skill(upsert_skill)
                if asyncio.iscoroutine(up):
                    await up
            elif action == SkillWriteAction.PATCH:
                # SkillPatchConflictError propagates to caller
                p = self.loader.patch_skill_file(
                    existing, request.old_string, request.new_string
                )
                if asyncio.iscoroutine(p):
                    await p
                # Re-read on-disk content and build updated Skill so the
                # registry reflects the patched frontmatter.
                rr = self.loader.read_skill_file(existing)
                if asyncio.iscoroutine(rr):
                    re_read = await rr
                else:
                    re_read = rr
                upsert_skill = self._build_skill_from_content(
                    re_read, existing.name, existing, origin,
                    preserve_source=True,
                )
                up = self.registry.upsert_skill(upsert_skill)
                if asyncio.iscoroutine(up):
                    await up
            elif action == SkillWriteAction.DELETE:
                d = self.loader.delete_skill(existing)
                if asyncio.iscoroutine(d):
                    await d
                rd = self.registry.delete_skill(name)
                if asyncio.iscoroutine(rd):
                    await rd
            elif action == SkillWriteAction.WRITE_FILE:
                wf = self.loader.write_linked_file(
                    existing, request.file_path, request.file_content
                )
                if asyncio.iscoroutine(wf):
                    await wf
                up = self.registry.upsert_skill(existing)
                if asyncio.iscoroutine(up):
                    await up
            elif action == SkillWriteAction.REMOVE_FILE:
                rf = self.loader.remove_linked_file(existing, request.file_path)
                if asyncio.iscoroutine(rf):
                    await rf
                rd = self.registry.delete_skill(name)
                if asyncio.iscoroutine(rd):
                    await rd
        except SkillPatchConflictError:
            raise
        except SkillWriteError as exc:
            return SkillManageResult(
                success=False, staged=False, pending_id=None,
                skill_name=name, action=action,
                summary="", diff=None, error=f"write_failed: {exc}",
            )

        # -- Step 7: update usage ------------------------------------------
        if action == SkillWriteAction.CREATE:
            new_usage = SkillUsage(
                created_by=origin.value,
                use_count=0,
                view_count=0,
                patch_count=0,
                created_at=None,
                last_used_at=None,
                last_viewed=None,
                last_patched_at=None,
                state="active",
                pinned=False,
                archived_at=None,
            )
            up_maybe = self.usage.upsert(name, new_usage)
            if asyncio.iscoroutine(up_maybe):
                await up_maybe
        elif action in {
            SkillWriteAction.PATCH,
            SkillWriteAction.EDIT,
            SkillWriteAction.WRITE_FILE,
            SkillWriteAction.REMOVE_FILE,
        }:
            ip = self.usage.increment_patch(name)
            if asyncio.iscoroutine(ip):
                await ip

        if action in {SkillWriteAction.DELETE, SkillWriteAction.REMOVE_FILE}:
            ss = self.usage.set_state(name, "archived")
            if asyncio.iscoroutine(ss):
                await ss

        # -- Step 8: return success ----------------------------------------
        summary = f"{action.value} {name}"
        diff = request.new_string or request.content or ""
        return SkillManageResult(
            success=True, staged=False, pending_id=None,
            skill_name=name, action=action,
            summary=summary, diff=diff, error=None,
        )

    async def approve_pending(self, pending_id: str) -> SkillManageResult:
        """Atomically take a pending write and replay it through manage_skill."""
        if (
            self.usage is None
            or self.pending is None
            or self.backup is None
            or self.policy is None
        ):
            raise RuntimeError(
                "approve_pending requires usage, pending, backup, and policy"
            )

        take = self.pending.approve_take(pending_id)
        if asyncio.iscoroutine(take):
            pw = await take
        else:
            pw = take

        if pw is None:
            return SkillManageResult(
                success=False, staged=False, pending_id=pending_id,
                skill_name="", action=SkillWriteAction.CREATE,
                summary="", diff=None,
                error="pending_not_found_or_taken",
            )

        payload = pw.payload or {}
        request = SkillManageRequest(
            action=SkillWriteAction(payload.get("action", "create")),
            name=payload.get("name", ""),
            origin=pw.origin,
            content=payload.get("content", ""),
            old_string=payload.get("old_string", ""),
            new_string=payload.get("new_string", ""),
            file_path=payload.get("file_path", ""),
            file_content=payload.get("file_content", ""),
            category=payload.get("category", ""),
            approved_replay=True,
        )

        result = await self.manage_skill(request)

        if result.success:
            clr = self.pending.clear(pending_id)
            if asyncio.iscoroutine(clr):
                await clr
        # On failure, leave the pending record in place.

        return result

    # ------------------------------------------------------------------
    # convenience methods for pending / usage (delegate to injected stores)
    # ------------------------------------------------------------------

    async def list_pending(self) -> list[SkillPendingWrite]:
        if self.pending is None:
            raise RuntimeError("list_pending requires pending store")
        return await self.pending.list()

    async def get_pending(self, pending_id: str) -> SkillPendingWrite | None:
        if self.pending is None:
            raise RuntimeError("get_pending requires pending store")
        return await self.pending.get(pending_id)

    async def reject_pending(self, pending_id: str) -> bool:
        if self.pending is None:
            raise RuntimeError("reject_pending requires pending store")
        return await self.pending.reject(pending_id)

    async def approve_all_pending(self) -> int:
        if self.pending is None:
            raise RuntimeError("approve_all_pending requires pending store")
        items = await self.pending.list()
        approved = 0
        for pw in items:
            result = await self.approve_pending(pw.pending_id)
            if result.success:
                approved += 1
        return approved

    async def reject_all_pending(self) -> int:
        if self.pending is None:
            raise RuntimeError("reject_all_pending requires pending store")
        items = await self.pending.list()
        rejected = 0
        for pw in items:
            ok = await self.pending.reject(pw.pending_id)
            if ok:
                rejected += 1
        return rejected

    async def list_usage(self) -> list[tuple[str, SkillUsage]]:
        if self.usage is None:
            raise RuntimeError("list_usage requires usage store")
        skills = await self.list_skills(include_disabled=True)
        result: list[tuple[str, SkillUsage]] = []
        for s in skills:
            usage = await self.usage.get(s.name)
            if usage is not None:
                result.append((s.name, usage))
        return result

    async def set_pinned(self, name: str, pinned: bool) -> None:
        if self.usage is None:
            raise RuntimeError("set_pinned requires usage store")
        await self.get(name)  # raises SkillNotFoundError if not found
        await self.usage.set_pinned(name, pinned)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _scan_text_for_action(self, request: SkillManageRequest) -> str:
        if request.action in {SkillWriteAction.CREATE, SkillWriteAction.EDIT}:
            return request.content
        if request.action == SkillWriteAction.PATCH:
            return request.new_string
        if request.action == SkillWriteAction.WRITE_FILE:
            return request.file_content
        return ""

    @staticmethod
    def _detect_injection(text: str) -> bool:
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                return True
        return False

    @staticmethod
    def _build_skill_for_write(
        name: str,
        existing: Skill | None,
        origin: SkillWriteOrigin,
    ) -> Skill:
        source = (
            SkillSource.AGENT
            if origin == SkillWriteOrigin.BACKGROUND_REVIEW
            else SkillSource.USER
        )
        relative_path = (
            existing.relative_path if existing else f"{name}/SKILL.md"
        )
        fm = existing.frontmatter if existing else SkillFrontmatter(
            name=name,
            description="",
            version="",
            platforms=[],
            tags=[],
            related_skills=[],
            author="",
            license="",
            setup_help=None,
            required_env_vars=[],
            raw={"name": name},
        )
        return Skill(
            id=existing.id if existing else str(uuid4()),
            name=name,
            relative_path=relative_path,
            description=existing.description if existing else "",
            platforms=list(existing.platforms) if existing else [],
            frontmatter=fm,
            enabled=existing.enabled if existing else True,
            readiness=(
                existing.readiness if existing else SkillReadiness.AVAILABLE
            ),
            last_scan_status="manual",
            last_scan_error=None,
            last_seen_at=None,
            created_at=existing.created_at if existing else None,
            updated_at=existing.updated_at if existing else None,
            source=source,
        )

    # ------------------------------------------------------------------
    # format validation + registry consistency helpers
    # ------------------------------------------------------------------

    async def _validate_format_for_request(
        self, request: SkillManageRequest, existing: Skill | None
    ) -> str | None:
        """Validate frontmatter format for create/edit/patch.

        Returns an error string (``format_invalid:<reason>``) or None when
        valid.  Skipped entirely when ``self.format_validator is None``
        (backward compat with callers that don't inject a validator).
        """
        if self.format_validator is None:
            return None

        action = request.action

        if action in {SkillWriteAction.CREATE, SkillWriteAction.EDIT}:
            return self._validate_create_edit(request, existing)

        if action == SkillWriteAction.PATCH:
            return await self._validate_patch(request, existing)

        return None

    def _validate_create_edit(
        self, request: SkillManageRequest, existing: Skill | None
    ) -> str | None:
        action = request.action
        parsed = _parse_frontmatter(request.content)
        if parsed is None:
            return "format_invalid:frontmatter is not a mapping"
        fm_dict, body = parsed

        dir_name = (
            request.name
            if action == SkillWriteAction.CREATE
            else (existing.name if existing else request.name)
        )
        result = self.format_validator.validate(
            SkillFormatRequest(
                frontmatter=fm_dict,
                dir_name=dir_name,
                body_line_count=len(body.splitlines()),
            )
        )
        if result.errors:
            return f"format_invalid:{result.errors[0]}"

        # Explicit name checks (redundant with validator but clearer).
        if action == SkillWriteAction.CREATE:
            if fm_dict.get("name") != request.name:
                return (
                    f"format_invalid:name mismatch: "
                    f"frontmatter.name={fm_dict.get('name')!r} "
                    f"request.name={request.name!r}"
                )
        elif action == SkillWriteAction.EDIT:
            if existing and fm_dict.get("name") != existing.name:
                return (
                    f"format_invalid:edit cannot change name: "
                    f"frontmatter.name={fm_dict.get('name')!r} "
                    f"existing.name={existing.name!r}"
                )

        return None

    async def _validate_patch(
        self, request: SkillManageRequest, existing: Skill | None
    ) -> str | None:
        if existing is None:
            return None
        # Read current on-disk content for candidate construction.
        rr = self.loader.read_skill_file(existing)
        if asyncio.iscoroutine(rr):
            current = await rr
        else:
            current = rr
        candidate = current.replace(request.old_string, request.new_string)

        current_parsed = _parse_frontmatter(current)
        candidate_parsed = _parse_frontmatter(candidate)

        # If candidate frontmatter is non-mapping, that's invalid.
        if candidate_parsed is None:
            return "format_invalid:patched frontmatter is not a mapping"

        # If current is non-mapping, we can't compare; skip (loader handles).
        if current_parsed is None:
            return None

        current_fm, _ = current_parsed
        candidate_fm, candidate_body = candidate_parsed

        # Body-only patch: frontmatter dict unchanged -> skip validation.
        if current_fm == candidate_fm:
            return None

        # Frontmatter changed; validate candidate.
        result = self.format_validator.validate(
            SkillFormatRequest(
                frontmatter=candidate_fm,
                dir_name=existing.name,
                body_line_count=len(candidate_body.splitlines()),
            )
        )
        if result.errors:
            return f"format_invalid:{result.errors[0]}"

        return None

    def _build_skill_from_content(
        self,
        content: str,
        name: str,
        existing: Skill | None,
        origin: SkillWriteOrigin,
        *,
        preserve_source: bool = False,
    ) -> Skill:
        """Build an authoritative Skill from (normalized) content.

        Used after write/patch so registry.upsert_skill receives a Skill
        whose description/platforms/frontmatter.raw are consistent with
        the disk-normalized frontmatter -- not the empty-description
        placeholder from _build_skill_for_write.
        """
        parsed = _parse_frontmatter(content)
        if parsed is None:
            # Non-mapping frontmatter; shouldn't happen after successful
            # write (write_skill_file normalizes). Fall back to placeholder.
            return self._build_skill_for_write(name, existing, origin)
        fm_dict, _ = parsed
        try:
            normalized = normalize_frontmatter(fm_dict)
        except SkillFormatError:
            normalized = fm_dict

        # Compute platforms from normalized metadata (string->list) with
        # top-level fallback.
        md = normalized.get("metadata")
        if isinstance(md, dict) and "platforms" in md:
            platforms = deserialize_metadata_list(md["platforms"])
        else:
            platforms = deserialize_metadata_list(normalized.get("platforms"))

        fm = skill_frontmatter_from_dict(normalized, name, platforms)
        if preserve_source and existing is not None:
            source = existing.source
        else:
            source = (
                SkillSource.AGENT
                if origin == SkillWriteOrigin.BACKGROUND_REVIEW
                else SkillSource.USER
            )
        relative_path = (
            existing.relative_path if existing else f"{name}/SKILL.md"
        )
        return Skill(
            id=existing.id if existing else str(uuid4()),
            name=name,
            relative_path=relative_path,
            description=fm.description,
            platforms=platforms,
            frontmatter=fm,
            enabled=existing.enabled if existing else True,
            readiness=(
                existing.readiness if existing else SkillReadiness.AVAILABLE
            ),
            last_scan_status="manual",
            last_scan_error=None,
            last_seen_at=None,
            created_at=existing.created_at if existing else None,
            updated_at=existing.updated_at if existing else None,
            source=source,
        )


def _normalize_warning(w: Any) -> SkillScanWarning:
    if isinstance(w, SkillScanWarning):
        return w
    return SkillScanWarning(
        relative_path=getattr(w, "relative_path", ""),
        reason=getattr(w, "reason", "unknown"),
        detail=getattr(w, "detail", None),
        first_path=getattr(w, "first_path", None),
    )


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str] | None:
    """Split content into (frontmatter_dict, body) using yaml.safe_load.

    Returns ``None`` if the frontmatter YAML is a non-mapping (invalid).
    Returns ``({}, content)`` when there is no frontmatter block.
    This is a local duplicate of file_loader's _split_frontmatter to
    respect the DDD boundary (Application must not import Infrastructure).
    """
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    raw = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw, dict):
        return None
    return raw, parts[2].lstrip("\n")


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
        # 保留既有 source，避免 SEED/AGENT Skill 在编辑后被降级为 USER
        # （SkillInput 不暴露 source 字段，因此构造时必须显式从 current 派生）
        source=current.source if current else SkillSource.USER,
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
            description=(
                "Discover available procedural skills by name, description, category, and tags. "
                "Use this before saying an installed capability is unavailable, especially for tasks "
                "such as weather, forecasts, travel checks, operations, or other capability requests "
                "that are not covered by direct tools."
            ),
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
                        "tags": list(s.frontmatter.tags),
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
                    "hint": (
                        "Choose the relevant skill by name/description/tags, then call "
                        "skill_view(name) before answering or using other tools."
                    ),
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
                content=json.dumps(payload, ensure_ascii=False),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.ERROR,
                content=json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False),
                duration_ms=int((time.monotonic() - start) * 1000),
            )


def skill_manage_tool_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_manage",
        description=(
            "Create, patch, edit, delete, write_file, or remove_file a Skill for "
            "Skill self-evolution. The server injects the write origin from the "
            "call context; clients cannot set it. Writes may be staged pending "
            "approval depending on policy; a staged result returns a pending_id. "
            "Naming guidance: use English kebab-case for name (e.g. my-skill); "
            "description should have English what/when text plus a parenthesized "
            "Chinese alias (e.g. Does X when Y (做某事)); put extension fields in "
            "metadata, not as unknown top-level fields; recommend calling "
            "skill_view(\"skill-creator\") before creating a new skill."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "create",
                        "patch",
                        "edit",
                        "delete",
                        "write_file",
                        "remove_file",
                    ],
                },
                "name": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "file_path": {"type": "string"},
                "file_content": {"type": "string"},
                "category": {"type": "string"},
                "absorbed_into": {
                    "type": "string",
                    "default": "",
                    "description": "delete only: declare absorption target umbrella "
                    "(empty=prune with no merge target, non-empty=merged into the "
                    "named umbrella); used by Curator classification, does not "
                    "change delete behavior.",
                },
            },
            "required": ["action", "name"],
            "additionalProperties": False,
        },
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.BUILTIN,
        toolset="skills",
    )


def _resolve_write_origin(context: ToolExecutionContext | None) -> SkillWriteOrigin:
    """Origin is read ONLY from server-injected trusted_metadata, never from
    client-controlled request.arguments, to prevent forgery."""
    origin_str = None
    if context is not None:
        trusted = getattr(context, "trusted_metadata", None)
        if isinstance(trusted, dict):
            origin_str = trusted.get("skill_write_origin")
    if origin_str == SkillWriteOrigin.BACKGROUND_REVIEW.value:
        return SkillWriteOrigin.BACKGROUND_REVIEW
    return SkillWriteOrigin.FOREGROUND


class SkillManageToolExecutor(ToolExecutor):
    def __init__(self, service: SkillService):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            args = request.arguments if isinstance(request.arguments, dict) else {}
            origin = _resolve_write_origin(context)

            action_str = str(args.get("action") or "")
            try:
                action = SkillWriteAction(action_str)
            except ValueError as exc:
                return ToolResult(
                    tool_call_id=request.id,
                    tool_name=request.name,
                    status=ToolResultStatus.ERROR,
                    content=json.dumps(
                        {"success": False, "error": f"invalid action: {action_str}"},
                        ensure_ascii=False,
                    ),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            manage_request = SkillManageRequest(
                action=action,
                name=str(args.get("name") or ""),
                origin=origin,
                content=str(args.get("content") or ""),
                old_string=str(args.get("old_string") or ""),
                new_string=str(args.get("new_string") or ""),
                file_path=str(args.get("file_path") or ""),
                file_content=str(args.get("file_content") or ""),
                category=str(args.get("category") or ""),
                absorbed_into=str(args.get("absorbed_into") or ""),
                approved_replay=False,
            )
            result = await self.service.manage_skill(manage_request)

            if result.success or result.staged:
                return ToolResult(
                    tool_call_id=request.id,
                    tool_name=request.name,
                    status=ToolResultStatus.SUCCESS,
                    content=json.dumps(
                        {
                            "success": result.success,
                            "staged": result.staged,
                            "pending_id": result.pending_id,
                            "skill_name": result.skill_name,
                            "action": result.action.value,
                            "summary": result.summary,
                        },
                        ensure_ascii=False,
                    ),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.ERROR,
                content=json.dumps(
                    {"success": False, "error": result.error},
                    ensure_ascii=False,
                ),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.ERROR,
                content=json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
