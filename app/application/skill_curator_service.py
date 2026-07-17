from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.domain.curator_policy import CuratorPolicy, CuratorPolicyRequest
from app.domain.policy import PolicyOutcome
from app.domain.skill import (
    CuratorConfig,
    CuratorRunResult,
    CuratorState,
    CuratorTransitions,
    SkillSource,
)

logger = logging.getLogger(__name__)

DEFAULT_PROTECTED_SEEDS = frozenset({"n-agent", "skill-creator"})

DEFAULT_REPORT_ROOT = Path("locals/curator")


def _parse_iso(ts: str | None) -> datetime | None:
    """解析 ISO 字符串为 datetime；失败返回 None；无 tzinfo 视为 UTC。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


CURATOR_DRY_RUN_BANNER = (
    "═══════════════════════════════════════════════════════════════\n"
    "DRY-RUN - REPORT ONLY. DO NOT MUTATE THE SKILL LIBRARY.\n"
    "═══════════════════════════════════════════════════════════════\n"
    "\n"
    "This is a PREVIEW pass. Follow every instruction below EXCEPT:\n"
    "  - DO NOT call skill_manage with action=patch/create/delete/write_file/remove_file.\n"
    "  - skills_list and skill_view are FINE - read as much as you need.\n"
    "Produce the same summary you would on a live run, but describe actions you WOULD take.\n"
    "═══════════════════════════════════════════════════════════════"
)


CURATOR_CONSOLIDATION_PROMPT = """\
你是一个 Skill Curator 后台 consolidation 审查 Agent。这是 umbrella-building 合并审查，不是被动审计。

目标: 维护一个 class-level 的 Skill 库，而非大量窄粒度 skill。一个 broad umbrella skill 带 labeled 子章节，胜过五个窄兄弟 skill。

硬规则:
1. 只处理候选列表中的 agent-created skill；不触碰 seed/pinned skill。
2. 不删除任何 skill，只归档（archive 可恢复）。
3. pinned=yes 的 skill 完全跳过。
4. 不以 use_count 为由跳过合并；按内容判断重叠。
5. 合并方式: (a) patch 进已有 umbrella；(b) create 新 umbrella SKILL.md；(c) demote 到 references/templates/scripts 子文件。归档被吸收的兄弟 skill 时，skill_manage delete 必须声明 absorbed_into=<umbrella>（空串表示真 prune 无合并目标）。

工具:
  - skills_list, skill_view: 读取当前 landscape
  - skill_manage action=patch: 给 umbrella 加章节
  - skill_manage action=create: 创建新 umbrella
  - skill_manage action=delete: 归档 skill，必须传 absorbed_into

完成后输出人类可读摘要 + 结构化 YAML 块:
## Structured summary (required)
```yaml
consolidations:
  - from: <old-skill>
    into: <umbrella>
    reason: <一句话>
prunings:
  - name: <skill>
    reason: <一句话>
```
每个归档的 skill 必须出现在 consolidations 或 prunings 之一。\
"""


class SkillCuratorService:
    """Curator 周期维护编排器。

    周期触发 + 确定性状态机迁移（active/stale/archived）+ 可选 LLM consolidation。
    对齐 HermesAgent agent/curator.py，融入 N-Agent DDD 分层。所有文件操作委托
    file_loader/backup_store，状态迁移委托 CuratorPolicy。never raises on the
    auto-trigger path。
    """

    def __init__(
        self,
        *,
        skill_registry: Any,
        skill_usage_store: Any,
        skill_service: Any,
        file_loader: Any,
        backup_store: Any,
        evolution_service: Any,
        curator_state_store: Any,
        curator_policy: CuratorPolicy,
        settings: Any,
        protected_seeds: set[str] | None = None,
        report_root: Path | str | None = None,
    ):
        self.skill_registry = skill_registry
        self.skill_usage_store = skill_usage_store
        self.skill_service = skill_service
        self.file_loader = file_loader
        self.backup_store = backup_store
        self.evolution_service = evolution_service
        self.curator_state_store = curator_state_store
        self.curator_policy = curator_policy
        self.settings = settings
        self._protected_seeds = (
            set(protected_seeds) if protected_seeds is not None else set(DEFAULT_PROTECTED_SEEDS)
        )
        self._report_root = Path(report_root) if report_root else DEFAULT_REPORT_ROOT
        self._in_flight = False

    # ------------------------------------------------------------------
    # Config + gating
    # ------------------------------------------------------------------

    def get_config(self) -> CuratorConfig:
        s = self.settings
        return CuratorConfig(
            enabled=s.skills_curator_enabled,
            interval_hours=s.skills_curator_interval_hours,
            min_idle_hours=s.skills_curator_min_idle_hours,
            stale_after_days=s.skills_curator_stale_after_days,
            archive_after_days=s.skills_curator_archive_after_days,
            prune_seeds=s.skills_curator_prune_seeds,
            consolidate=s.skills_curator_consolidate,
            consolidate_max_iterations=s.skills_curator_consolidate_max_iterations,
        )

    async def should_run_now(self, now: datetime | None = None) -> bool:
        cfg = self.get_config()
        if not cfg.enabled:
            return False
        try:
            state = await self.curator_state_store.load()
        except Exception:
            logger.warning("curator state load failed", exc_info=True)
            return False
        if state.paused:
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        if state.last_run_at is None:
            seeded = CuratorState(
                last_run_at=now.isoformat(),
                last_run_summary="deferred first run - curator seeded",
            )
            try:
                await self.curator_state_store.save(seeded)
            except Exception:
                logger.warning("curator state seed save failed", exc_info=True)
            return False
        last = _parse_iso(state.last_run_at)
        if last is None:
            return False
        return (now - last) >= timedelta(hours=cfg.interval_hours)

    async def maybe_run_curator(
        self,
        idle_for_seconds: float | None = None,
        on_summary: Callable[[str], None] | None = None,
    ) -> CuratorRunResult | None:
        """自动触发入口。门禁通过才跑；never raises。

        idle_for_seconds is None 时跳过自动触发（无法证明空闲，避免 min_idle_hours
        形同虚设）。CLI 手动 run 直接调 run_curator_review，不走此门禁。
        """
        try:
            if not await self.should_run_now():
                return None
            if self._in_flight:
                logger.debug("curator already in flight, skipping auto trigger")
                return None
            if idle_for_seconds is None:
                return None
            cfg = self.get_config()
            if idle_for_seconds < cfg.min_idle_hours * 3600:
                return None
            return await self.run_curator_review(on_summary=on_summary)
        except Exception as e:
            logger.warning("maybe_run_curator failed: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Deterministic state machine (no LLM)
    # ------------------------------------------------------------------

    async def apply_automatic_transitions(
        self, now: datetime | None = None, dry_run: bool = False
    ) -> tuple[CuratorTransitions, list[dict]]:
        """确定性状态机迁移。返回 (counts, errors)。

        archive 判定优先于 stale；delete_skill 移目录成功后才 usage.archive_skill；
        单 skill 异常捕获记录到 errors 并继续。
        """
        cfg = self.get_config()
        if now is None:
            now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=cfg.stale_after_days)
        archive_cutoff = now - timedelta(days=cfg.archive_after_days)
        rows = await self.skill_usage_store.list_curator_managed(
            prune_seeds=cfg.prune_seeds, protected_names=self._protected_seeds
        )
        counts = CuratorTransitions()
        errors: list[dict] = []
        for row in rows:
            counts = replace(counts, checked=counts.checked + 1)
            try:
                outcome = self.curator_policy.evaluate(
                    CuratorPolicyRequest(
                        name=row.name,
                        source=SkillSource(row.source) if row.source else None,
                        state=row.state,
                        pinned=row.pinned,
                        is_protected_seed=row.name in self._protected_seeds,
                        prune_seeds=cfg.prune_seeds,
                        action="transition",
                    )
                )
                if outcome == PolicyOutcome.DENY:
                    continue
                if not row._persisted:
                    if not dry_run:
                        await self.skill_usage_store.seed_record_if_missing(row.name)
                    counts = replace(counts, seeded=counts.seeded + 1)
                    continue
                anchor = _parse_iso(row.last_activity_at) or row.created_at or now
                never_used = row.use_count == 0
                if never_used and anchor > stale_cutoff:
                    if row.state == "stale" and not dry_run:
                        await self.skill_usage_store.set_state(row.name, "active")
                        counts = replace(counts, reactivated=counts.reactivated + 1)
                    continue
                if anchor <= archive_cutoff and row.state != "archived":
                    if not dry_run:
                        ok, err = await self._archive_skill(row.name)
                        if not ok:
                            errors.append({"name": row.name, "error": err})
                            continue
                    counts = replace(counts, archived=counts.archived + 1)
                elif anchor <= stale_cutoff and row.state == "active":
                    if not dry_run:
                        await self.skill_usage_store.set_state(row.name, "stale")
                    counts = replace(counts, marked_stale=counts.marked_stale + 1)
                elif anchor > stale_cutoff and row.state == "stale":
                    if not dry_run:
                        await self.skill_usage_store.set_state(row.name, "active")
                    counts = replace(counts, reactivated=counts.reactivated + 1)
            except Exception as e:
                logger.warning(
                    "curator transition failed for %s: %s", row.name, e, exc_info=True
                )
                errors.append({"name": row.name, "error": str(e)})
        return counts, errors

    async def _archive_skill(self, name: str) -> tuple[bool, str | None]:
        """归档单个 skill：delete_skill 移目录成功后 usage.archive_skill。

        返回 (ok, error)。delete_skill 失败不调 usage.archive_skill；
        usage.archive_skill 失败尝试补偿（restore 目录），失败报告 partial_failure。
        """
        try:
            skill = await self.skill_registry.get_skill(name)
            if skill is None:
                return False, f"skill not found in registry: {name}"
            await self.file_loader.delete_skill(skill)
        except Exception as e:
            return False, f"delete_skill failed: {e}"
        try:
            await self.skill_usage_store.archive_skill(name)
            return True, None
        except Exception as e:
            # partial_failure: 目录已移走但 usage 未更新。尝试 restore 目录补偿。
            try:
                await self.file_loader.restore_skill(name)
                return False, f"usage.archive_skill failed, directory restored: {e}"
            except Exception as re_err:
                return False, (
                    f"partial_failure: directory archived but usage not updated; "
                    f"restore also failed: {e} / {re_err}"
                )

    # ------------------------------------------------------------------
    # Manual operations (CLI entry points)
    # ------------------------------------------------------------------

    async def manual_archive(self, name: str) -> tuple[bool, str]:
        """手动归档：经 CuratorPolicy 校验后归档。拒绝 pinned/protected/user/seed。"""
        cfg = self.get_config()
        skill = await self.skill_registry.get_skill(name)
        if skill is None:
            return False, f"skill not found: {name}"
        outcome = self.curator_policy.evaluate(
            CuratorPolicyRequest(
                name=name,
                source=skill.source,
                state="active",
                pinned=False,  # pinned 单独检查
                is_protected_seed=name in self._protected_seeds,
                prune_seeds=cfg.prune_seeds,
                action="archive",
            )
        )
        # pinned 检查（manual archive 拒绝任何 pinned）
        usage = await self.skill_usage_store.get(name)
        if usage is not None and usage.pinned:
            return False, f"skill '{name}' is pinned - unpin first"
        if outcome == PolicyOutcome.DENY:
            return False, f"skill '{name}' not archivable (protected/seed/user)"
        ok, err = await self._archive_skill(name)
        if ok:
            return True, f"archived '{name}'"
        return False, err or "archive failed"

    async def manual_restore(self, name: str) -> tuple[bool, str]:
        """手动恢复：file_loader.restore_skill + registry scan + usage.restore_skill。

        scan_now 让恢复的 skill 目录重新入 registry；若该 skill 之前已被 delete
        从 registry 移除，重扫按目录名识别 seed、其余默认 user——source 保留依赖
        registry 未被删除，caller 需自行确认 source 归属。
        """
        try:
            await self.file_loader.restore_skill(name)
        except FileNotFoundError as e:
            return False, str(e)
        except FileExistsError as e:
            return False, str(e)
        except Exception as e:
            return False, f"restore_skill failed: {e}"
        # registry scan/upsert：让恢复的 skill 目录重新入 registry（plan T7 S5）。
        try:
            await self.skill_service.scan_now()
        except Exception as e:
            logger.warning("curator restore scan_now failed: %s", e, exc_info=True)
        try:
            await self.skill_usage_store.restore_skill(name)
        except Exception as e:
            return False, f"partial_failure: directory restored but usage not updated: {e}"
        return True, f"restored '{name}'"

    async def manual_pin(self, name: str, pinned: bool) -> tuple[bool, str]:
        """pin/unpin：仅 agent-created skill 生效。"""
        skill = await self.skill_registry.get_skill(name)
        if skill is None:
            return False, f"skill not found: {name}"
        if skill.source != SkillSource.AGENT:
            return False, (
                f"skill '{name}' is {skill.source.value}, not agent-created - "
                "only agent-created skills participate in curation"
            )
        await self.skill_usage_store.set_pinned(name, pinned)
        action = "pinned" if pinned else "unpinned"
        return True, f"{action} '{name}'"

    async def list_archived_skills(self) -> list[dict]:
        """列出 .archive 中的 skill（委托 file_loader）。"""
        return await self.file_loader.list_archived()

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def run_curator_review(
        self,
        synchronous: bool = True,
        dry_run: bool = False,
        consolidate: bool | None = None,
        on_summary: Callable[[str], None] | None = None,
    ) -> CuratorRunResult:
        """执行一次 curator review。

        1. in-flight guard
        2. 非 dry_run 且有候选 -> backup_store.snapshot（fail-closed）
        3. apply_automatic_transitions
        4. consolidate（None 读配置）
        5. 写报告
        6. 非 dry_run 更新 state
        7. on_summary
        """
        if self._in_flight:
            return CuratorRunResult(
                started_at=_now_iso(),
                auto_transitions=CuratorTransitions(),
                summary_so_far="skipped: curator already in flight",
            )
        self._in_flight = True
        start = datetime.now(timezone.utc)
        cfg = self.get_config()
        if consolidate is None:
            consolidate = cfg.consolidate
        errors: list[dict] = []
        backup_error: str | None = None
        try:
            rows = await self.skill_usage_store.list_curator_managed(
                prune_seeds=cfg.prune_seeds, protected_names=self._protected_seeds
            )
            has_candidates = len(rows) > 0

            # backup fail-closed（非 dry_run 且有候选）
            if not dry_run and has_candidates:
                try:
                    await self.backup_store.snapshot()
                except Exception as e:
                    backup_error = f"backup failed: {e}"
                    errors.append({"phase": "backup", "error": backup_error})
                    counts = CuratorTransitions()
                    auto_summary = f"auto: skipped (backup failed)"
                    await self._finalize_run(
                        start, counts, auto_summary, dry_run, errors=errors,
                        backup_error=backup_error, llm_meta=None,
                    )
                    if on_summary:
                        try:
                            on_summary(f"curator: {auto_summary}")
                        except Exception:
                            pass
                    return CuratorRunResult(
                        started_at=start.isoformat(),
                        auto_transitions=counts,
                        summary_so_far=auto_summary,
                    )

            counts, trans_errors = await self.apply_automatic_transitions(
                now=start, dry_run=dry_run
            )
            errors.extend(trans_errors)

            auto_parts = []
            if counts.marked_stale:
                auto_parts.append(f"{counts.marked_stale} marked stale")
            if counts.archived:
                auto_parts.append(f"{counts.archived} archived")
            if counts.reactivated:
                auto_parts.append(f"{counts.reactivated} reactivated")
            auto_summary = ", ".join(auto_parts) if auto_parts else "no changes"

            # consolidation
            llm_meta: dict[str, Any] = {
                "final": "", "summary": "", "model": "", "provider": "",
                "tool_calls": [], "error": None,
            }
            if consolidate and has_candidates and self.evolution_service is not None:
                llm_meta = await self._run_consolidation(
                    dry_run=dry_run, session_id=f"curator-{start.strftime('%Y%m%d%H%M%S')}"
                )
            elif consolidate and not has_candidates:
                llm_meta["summary"] = "skipped (no candidates)"
            elif consolidate and self.evolution_service is None:
                llm_meta["summary"] = "skipped (evolution_service not configured)"
                errors.append({"phase": "consolidation", "error": "evolution_service missing"})
            else:
                llm_meta["summary"] = "skipped (consolidation off)"

            prefix = "dry-run auto: " if dry_run else "auto: "
            final_summary = f"{prefix}{auto_summary}; llm: {llm_meta.get('summary', 'no change')}"

            await self._finalize_run(
                start, counts, final_summary, dry_run, errors=errors,
                backup_error=backup_error, llm_meta=llm_meta,
            )

            if on_summary:
                try:
                    on_summary(f"curator: {final_summary}")
                except Exception:
                    pass
            return CuratorRunResult(
                started_at=start.isoformat(),
                auto_transitions=counts,
                summary_so_far=final_summary,
            )
        except Exception as e:
            logger.warning("run_curator_review failed: %s", e, exc_info=True)
            errors.append({"phase": "orchestrator", "error": str(e)})
            try:
                await self._finalize_run(
                    start, CuratorTransitions(), f"error: {e}", dry_run,
                    errors=errors, backup_error=backup_error, llm_meta=None,
                )
            except Exception:
                pass
            return CuratorRunResult(
                started_at=start.isoformat(),
                auto_transitions=CuratorTransitions(),
                summary_so_far=f"error: {e}",
            )
        finally:
            self._in_flight = False

    async def _run_consolidation(self, *, dry_run: bool, session_id: str) -> dict[str, Any]:
        """fork consolidation review。dry_run 时只读 preview。"""
        try:
            candidate_list = await self.render_candidate_list()
            if not candidate_list or "No agent-created skills" in candidate_list:
                return {
                    "final": "", "summary": "skipped (no candidates)",
                    "model": "", "provider": "", "tool_calls": [], "error": None,
                }
            prompt = CURATOR_CONSOLIDATION_PROMPT
            if dry_run:
                prompt = f"{CURATOR_DRY_RUN_BANNER}\n\n{prompt}"
            cfg = self.get_config()
            result = await self.evolution_service.run_background_review(
                session_id=session_id,
                digest=candidate_list,
                prompt=prompt,
                max_iterations=cfg.consolidate_max_iterations,
                allow_toolsets={"skills"},
            )
            summary = result.final_text[:240] if result.final_text else (
                result.error or "no change"
            )
            return {
                "final": result.final_text,
                "summary": summary,
                "model": "",
                "provider": "",
                "tool_calls": result.tool_calls,
                "error": result.error,
                "preview_only": dry_run,
            }
        except Exception as e:
            return {
                "final": "", "summary": f"error ({e})", "model": "", "provider": "",
                "tool_calls": [], "error": str(e),
            }

    async def render_candidate_list(self) -> str:
        cfg = self.get_config()
        rows = await self.skill_usage_store.list_curator_managed(
            prune_seeds=cfg.prune_seeds, protected_names=self._protected_seeds
        )
        if not rows:
            return "No agent-created skills to review."
        lines = [f"Agent-created skills ({len(rows)}):\n"]
        for r in rows:
            lines.append(
                f"- {r.name}  state={r.state}  pinned={'yes' if r.pinned else 'no'}  "
                f"activity={r.activity_count}  use={r.use_count}  view={r.view_count}  "
                f"patches={r.patch_count}  last_activity={r.last_activity_at or 'never'}"
            )
        return "\n".join(lines)

    async def _finalize_run(
        self,
        start: datetime,
        counts: CuratorTransitions,
        final_summary: str,
        dry_run: bool,
        *,
        errors: list[dict] | None = None,
        backup_error: str | None = None,
        llm_meta: dict[str, Any] | None,
    ) -> None:
        """写报告 + 更新 curator_state。"""
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        report_path = await self._write_run_report(
            started_at=start,
            elapsed_seconds=elapsed,
            counts=counts,
            final_summary=final_summary,
            dry_run=dry_run,
            errors=errors or [],
            backup_error=backup_error,
            llm_meta=llm_meta or {},
        )
        if dry_run:
            return
        try:
            state = await self.curator_state_store.load()
            new_state = CuratorState(
                last_run_at=start.isoformat(),
                last_run_duration_seconds=elapsed,
                last_run_summary=final_summary,
                last_report_path=str(report_path) if report_path else None,
                paused=state.paused,
                run_count=state.run_count + 1,
            )
            await self.curator_state_store.save(new_state)
        except Exception:
            logger.warning("curator state save failed", exc_info=True)

    # ------------------------------------------------------------------
    # Report writing (stdlib, no Infrastructure import)
    # ------------------------------------------------------------------

    async def _write_run_report(
        self,
        *,
        started_at: datetime,
        elapsed_seconds: float,
        counts: CuratorTransitions,
        final_summary: str,
        dry_run: bool,
        errors: list[dict],
        backup_error: str | None,
        llm_meta: dict[str, Any],
    ) -> str | None:
        try:
            root = Path(self._report_root)
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.warning("curator report root create failed", exc_info=True)
            return None
        stamp = started_at.strftime("%Y%m%d-%H%M%S")
        run_dir = root / stamp
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = root / f"{stamp}-{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except Exception:
            logger.warning("curator run dir create failed", exc_info=True)
            return None

        skipped_reasons: list[str] = []
        if llm_meta.get("summary", "").startswith("skipped"):
            skipped_reasons.append(llm_meta["summary"])

        payload = {
            "started_at": started_at.isoformat(),
            "duration_seconds": round(elapsed_seconds, 2),
            "dry_run": dry_run,
            "auto_transitions": {
                "checked": counts.checked,
                "marked_stale": counts.marked_stale,
                "archived": counts.archived,
                "reactivated": counts.reactivated,
                "seeded": counts.seeded,
            },
            "summary": final_summary,
            "skipped_reasons": skipped_reasons,
            "errors": errors,
            "backup_error": backup_error,
            "llm_final": llm_meta.get("final", ""),
            "llm_summary": llm_meta.get("summary", ""),
            "llm_error": llm_meta.get("error"),
            "tool_calls": llm_meta.get("tool_calls", []),
            "preview_only": llm_meta.get("preview_only", False),
        }
        try:
            (run_dir / "run.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            logger.warning("curator run.json write failed", exc_info=True)
        try:
            (run_dir / "REPORT.md").write_text(
                _render_report_markdown(payload), encoding="utf-8"
            )
        except Exception:
            logger.warning("curator REPORT.md write failed", exc_info=True)
        return str(run_dir)

    # ------------------------------------------------------------------
    # Classification (consolidated vs pruned)
    # ------------------------------------------------------------------

    def _reconcile_classification(
        self,
        removed: list[str],
        tool_calls: list[dict],
        model_final: str,
        after_names: set[str],
        added: list[str],
    ) -> dict[str, list[dict]]:
        """分类 removed skill 为 consolidated/pruned。

        优先级: absorbed_into tool-call 参数 > 结构化 YAML block > heuristic。
        解析异常保守归 pruned。
        """
        absorbed = _extract_absorbed_into_declarations(tool_calls)
        model_block = _parse_structured_summary(model_final)
        heuristic = _classify_removed_skills(
            removed=removed, added=added, after_names=after_names,
            tool_calls=tool_calls,
        )
        heur_cons = {e["name"]: e for e in heuristic.get("consolidated", [])}
        destinations = after_names | set(added)
        consolidated: list[dict] = []
        pruned: list[dict] = []
        model_cons = {e["from"]: e for e in model_block.get("consolidations", [])}
        for name in removed:
            dec = absorbed.get(name)
            if dec is not None:
                into = dec.get("into", "")
                if into and into in destinations:
                    consolidated.append({"name": name, "into": into, "source": "absorbed_into"})
                    continue
                if into == "":
                    pruned.append({"name": name, "source": "absorbed_into prune"})
                    continue
            mc = model_cons.get(name)
            if mc and mc.get("into") in destinations:
                consolidated.append({"name": name, "into": mc["into"], "source": "model"})
                continue
            hc = heur_cons.get(name)
            if hc:
                consolidated.append(
                    {"name": name, "into": hc["into"], "source": "tool-call audit"}
                )
                continue
            pruned.append({"name": name, "source": "no-evidence fallback"})
        return {"consolidated": consolidated, "pruned": pruned}

    async def get_status_view(self) -> dict[str, Any]:
        cfg = self.get_config()
        try:
            state = await self.curator_state_store.load()
        except Exception:
            state = CuratorState()
        rows = await self.skill_usage_store.list_curator_managed(
            prune_seeds=cfg.prune_seeds, protected_names=self._protected_seeds
        )
        by_state = {"active": 0, "stale": 0, "archived": 0}
        pinned: list[str] = []
        for r in rows:
            by_state[r.state] = by_state.get(r.state, 0) + 1
            if r.pinned:
                pinned.append(r.name)
        return {
            "enabled": cfg.enabled,
            "paused": state.paused,
            "run_count": state.run_count,
            "last_run_at": state.last_run_at,
            "last_run_summary": state.last_run_summary,
            "last_report_path": state.last_report_path,
            "config": {
                "interval_hours": cfg.interval_hours,
                "min_idle_hours": cfg.min_idle_hours,
                "stale_after_days": cfg.stale_after_days,
                "archive_after_days": cfg.archive_after_days,
                "prune_seeds": cfg.prune_seeds,
                "consolidate": cfg.consolidate,
                "consolidate_max_iterations": cfg.consolidate_max_iterations,
            },
            "skills": {
                "total": len(rows),
                "by_state": by_state,
                "pinned": pinned,
            },
        }


# ---------------------------------------------------------------------------
# Module-level classification helpers
# ---------------------------------------------------------------------------


def _extract_absorbed_into_declarations(
    tool_calls: list[dict],
) -> dict[str, dict[str, Any]]:
    """从 skill_manage delete 调用提取 absorbed_into 声明（权威分类信号）。"""
    out: dict[str, dict[str, Any]] = {}
    for tc in tool_calls or []:
        if not isinstance(tc, dict) or tc.get("name") != "skill_manage":
            continue
        raw = tc.get("arguments") or ""
        args: dict[str, Any] = {}
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                args = json.loads(raw)
            except Exception:
                continue
        if args.get("action") != "delete":
            continue
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if "absorbed_into" not in args:
            continue
        target = args.get("absorbed_into")
        if not isinstance(target, str):
            continue
        out[name.strip()] = {"into": target.strip()}
    return out


def _parse_structured_summary(llm_final: str) -> dict[str, list[dict[str, str]]]:
    """从 LLM final 提取 ```yaml 结构化块。"""
    import re

    empty: dict[str, list[dict[str, str]]] = {"consolidations": [], "prunings": []}
    if not llm_final or not isinstance(llm_final, str):
        return empty
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", llm_final, re.DOTALL | re.IGNORECASE)
    if not match:
        return empty
    body = match.group(1)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(body)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    out: dict[str, list[dict[str, str]]] = {"consolidations": [], "prunings": []}
    for key, field in (("consolidations", "from"), ("prunings", "name")):
        raw = data.get(key) or []
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            main = entry.get(field)
            if not (isinstance(main, str) and main.strip()):
                continue
            out[key].append(
                {
                    field: main.strip(),
                    "into": str(entry.get("into", "")).strip(),
                    "reason": str(entry.get("reason", "")).strip(),
                }
            )
    return out


def _classify_removed_skills(
    *,
    removed: list[str],
    added: list[str],
    after_names: set[str],
    tool_calls: list[dict],
) -> dict[str, list[dict]]:
    """Heuristic: removed skill 被 referenced 于 surviving/added skill 的 tool call。"""
    import re

    destinations = after_names | set(added)
    consolidated: list[dict] = []
    pruned: list[dict] = []
    parsed: list[dict] = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict) or tc.get("name") != "skill_manage":
            continue
        raw = tc.get("arguments") or ""
        args: dict[str, Any] = {}
        if isinstance(raw, dict):
            args = raw
        elif isinstance(raw, str):
            try:
                args = json.loads(raw)
            except Exception:
                continue
        if isinstance(args, dict):
            parsed.append(args)
    for name in removed:
        into: str | None = None
        needles = {name, name.replace("-", "_"), name.replace("_", "-")}
        for args in parsed:
            target = args.get("name")
            if not isinstance(target, str) or target == name or target not in destinations:
                continue
            haystacks = [
                str(args.get(k, "")) for k in ("file_path", "file_content", "content", "new_string")
                if isinstance(args.get(k), str)
            ]
            hit = any(
                bool(re.search(rf"\b{re.escape(n)}\b", h))
                for h in haystacks for n in needles if n
            )
            if hit:
                into = target
                break
        if into:
            consolidated.append({"name": name, "into": into})
        else:
            pruned.append({"name": name})
    return {"consolidated": consolidated, "pruned": pruned}


def _render_report_markdown(p: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Curator run - {p.get('started_at', '')}\n")
    auto = p.get("auto_transitions") or {}
    lines.append("## Auto-transitions (deterministic, no LLM)\n")
    lines.append(f"- checked: {auto.get('checked', 0)}")
    lines.append(f"- marked stale: {auto.get('marked_stale', 0)}")
    lines.append(f"- archived: {auto.get('archived', 0)}")
    lines.append(f"- reactivated: {auto.get('reactivated', 0)}")
    lines.append(f"- seeded: {auto.get('seeded', 0)}")
    lines.append("")
    if p.get("dry_run"):
        lines.append("> DRY-RUN: no changes applied.\n")
    if p.get("backup_error"):
        lines.append(f"> backup error: `{p['backup_error']}`\n")
    llm = p.get("llm_summary") or ""
    if llm:
        lines.append(f"## LLM pass\n\n{llm}\n")
    errors = p.get("errors") or []
    if errors:
        lines.append(f"## Errors ({len(errors)})\n")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")
    final = p.get("llm_final") or ""
    if final:
        lines.append("## LLM final summary\n")
        lines.append(final)
        lines.append("")
    lines.append(f"\nsummary: {p.get('summary', '')}\n")
    return "\n".join(lines)
