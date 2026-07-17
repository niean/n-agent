from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from app.domain.skill import (
    Skill,
    SkillPatchConflictError,
    SkillReadiness,
    SkillScanError,
    SkillSource,
    SkillValidationError,
)
from app.domain.skill_format import (
    SkillFormatError,
    SkillFormatRequest,
    SkillFormatValidator,
    metadata_get,
    normalize_frontmatter,
    skill_frontmatter_from_dict,
)


EXCLUDED_SKILL_DIRS = {".git", ".github", ".hub", ".archive", ".backups"}
INJECTION_PATTERNS = (
    re.compile(r"ignore (all )?previous instructions", re.I),
    re.compile(r"system\s*:\s*you are", re.I),
)
LINKED_DIRS = ("references", "templates", "scripts", "assets")

PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}


def _platform_matches(platforms: list[str], current: str) -> bool:
    if not platforms:
        return True
    cur = (current or "").lower()
    for p in platforms:
        normalized = str(p).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if cur.startswith(mapped):
            return True
    return False


@dataclass(frozen=True)
class SkillFileLoaderConfig:
    root: Path
    current_platform: str = "linux"
    inline_shell_enabled: bool = False
    inline_shell_timeout: int = 10
    max_view_bytes: int = 131072
    max_count: int = 200


@dataclass(frozen=True)
class SkillScanWarning:
    relative_path: str
    reason: str
    detail: str | None = None
    first_path: str | None = None


class SkillFileLoader:
    def __init__(self, config: SkillFileLoaderConfig):
        self.config = config

    @property
    def root(self) -> Path:
        return Path(self.config.root)

    async def scan(self) -> tuple[list[Skill], list[SkillScanWarning]]:
        return await asyncio.to_thread(self._scan_sync)

    def _scan_sync(self) -> tuple[list[Skill], list[SkillScanWarning]]:
        from app.infrastructure.path_security import validate_within_dir

        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        seed_names = _seed_dir_names()
        skills: list[Skill] = []
        warnings: list[SkillScanWarning] = []
        seen_names: dict[str, str] = {}
        count = 0
        validator = SkillFormatValidator()
        for file in _iter_skill_files(root):
            count += 1
            if count > self.config.max_count:
                break
            rel = str(file.relative_to(root))
            err = validate_within_dir(file, root)
            if err is not None:
                warnings.append(SkillScanWarning(rel, "path_violation"))
                continue
            try:
                fm_dict, body = _split_frontmatter(file.read_text(encoding="utf-8"))
            except Exception as exc:
                warnings.append(SkillScanWarning(rel, "yaml_error", detail=str(exc)[:200]))
                continue
            skill_dir = file.parent
            name = str(fm_dict.get("name") or skill_dir.name).strip() or skill_dir.name
            if name in seen_names:
                warnings.append(
                    SkillScanWarning(rel, "duplicate_name", first_path=seen_names[name])
                )
                continue
            seen_names[name] = rel
            # Platforms: metadata.platforms (string->list) first, then top-level fallback.
            # Keeps readiness semantics identical (macos-only on linux -> UNSUPPORTED).
            platforms = metadata_get(fm_dict, "platforms", is_list=True)
            unsupported = bool(platforms) and not _platform_matches(
                platforms, self.config.current_platform
            )
            readiness = (
                SkillReadiness.UNSUPPORTED if unsupported else SkillReadiness.AVAILABLE
            )
            last_scan_error: str | None = None
            for pattern in INJECTION_PATTERNS:
                if pattern.search(body):
                    last_scan_error = "injection_warning"
                    break
            # Format validation: non-blocking.  Records format_warning but
            # never prevents the skill from entering the registry.  Injection
            # takes priority for last_scan_error.
            fmt_result = validator.validate(
                SkillFormatRequest(
                    frontmatter=fm_dict,
                    dir_name=skill_dir.name,
                    body_line_count=len(body.splitlines()),
                )
            )
            if fmt_result.errors or fmt_result.warnings:
                detail = _format_detail(fmt_result.errors, fmt_result.warnings)
                warnings.append(SkillScanWarning(rel, "format_warning", detail=detail))
                if last_scan_error is None:
                    last_scan_error = "format_warning"
            fm = skill_frontmatter_from_dict(fm_dict, name, platforms)
            top_dir = Path(rel).parts[0] if Path(rel).parts else ""
            default_source = SkillSource.SEED if top_dir in seed_names else SkillSource.USER
            source = _resolve_source(fm_dict, default_source)
            skill = Skill(
                id=str(uuid4()), name=name, relative_path=rel,
                description=fm.description, platforms=platforms,
                frontmatter=fm, enabled=True, readiness=readiness,
                last_scan_status="ok" if last_scan_error is None else "warning",
                last_scan_error=last_scan_error,
                last_seen_at=None, created_at=None, updated_at=None,
                source=source,
            )
            skills.append(skill)
        return skills, warnings

    async def render(self, skill: Skill, session_id: str = "") -> str:
        return await asyncio.to_thread(self._render_sync, skill, session_id)

    def _render_sync(self, skill: Skill, session_id: str) -> str:
        skill_dir = (self.root / Path(skill.relative_path).parent).resolve()
        skill_file = self.root / skill.relative_path
        text = skill_file.read_text(encoding="utf-8")
        text = text.replace("${HERMES_SKILL_DIR}", str(skill_dir))
        text = text.replace("${HERMES_SESSION_ID}", session_id or "")
        if self.config.inline_shell_enabled:
            text = _expand_inline_shell(
                text, cwd=skill_dir, timeout=self.config.inline_shell_timeout
            )
        if len(text.encode("utf-8")) > self.config.max_view_bytes:
            data = text.encode("utf-8")
            cut = self.config.max_view_bytes
            text = (
                data[:cut].decode("utf-8", errors="ignore")
                + f"\n...[truncated {len(data) - cut} bytes]"
            )
        return text

    async def read_linked_file(self, skill: Skill, file_path: str) -> str:
        return await asyncio.to_thread(self._read_linked_file_sync, skill, file_path)

    async def read_script_bytes(self, skill: Skill, script_relative_path: str) -> bytes:
        return await asyncio.to_thread(
            self._read_script_bytes_sync, skill, script_relative_path
        )

    async def read_skill_file(self, skill: Skill) -> str:
        return await asyncio.to_thread(self._read_skill_file_sync, skill)

    def _read_skill_file_sync(self, skill: Skill) -> str:
        from app.infrastructure.path_security import validate_within_dir

        skill_file = self.root / skill.relative_path
        err = validate_within_dir(skill_file, self.root)
        if err is not None:
            raise SkillValidationError(err)
        if not skill_file.is_file():
            raise FileNotFoundError(f"skill file not found: {skill_file}")
        return skill_file.read_text(encoding="utf-8")

    def _read_script_bytes_sync(
        self, skill: Skill, script_relative_path: str
    ) -> bytes:
        """Open a linked script through dir fds without following any symlink."""
        if not isinstance(script_relative_path, str):
            raise SkillValidationError("skill_script_path_denied")
        normalized = script_relative_path.replace("\\", "/")
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or len(parts) < 2
            or parts[0] != "scripts"
            or any(part in {"", ".", ".."} for part in parts)
            or normalized != script_relative_path
        ):
            raise SkillValidationError("skill_script_path_denied")

        skill_rel = Path(skill.relative_path)
        if skill_rel.name != "SKILL.md" or skill_rel.is_absolute():
            raise SkillValidationError("skill_script_path_denied")
        skill_parts = skill_rel.parent.parts
        if not skill_parts or any(part in {"", ".", ".."} for part in skill_parts):
            raise SkillValidationError("skill_script_path_denied")

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | nofollow
        )
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
        fds: list[int] = []
        try:
            root_metadata = self.root.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                raise SkillValidationError("skill_script_path_denied")
            fd = os.open(self.root, directory_flags)
            fds.append(fd)
            opened_root = os.fstat(fd)
            if (opened_root.st_dev, opened_root.st_ino) != (
                root_metadata.st_dev,
                root_metadata.st_ino,
            ):
                raise SkillValidationError("skill_script_path_denied")
            for component in (*skill_parts, *parts[:-1]):
                fd = os.open(component, directory_flags, dir_fd=fds[-1])
                fds.append(fd)
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise SkillValidationError("skill_script_path_denied")
            fd = os.open(parts[-1], file_flags, dir_fd=fds[-1])
            fds.append(fd)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SkillValidationError("skill_script_path_denied")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 65536):
                total += len(chunk)
                if total > self.config.max_view_bytes:
                    raise SkillValidationError("skill_script_too_large")
                chunks.append(chunk)
            return b"".join(chunks)
        except SkillValidationError:
            raise
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SkillValidationError("skill_script_path_denied") from exc
        finally:
            for fd in reversed(fds):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _read_linked_file_sync(self, skill: Skill, file_path: str) -> str:
        from app.infrastructure.path_security import (
            has_traversal_component,
            validate_within_dir,
        )

        if has_traversal_component(file_path):
            raise SkillValidationError("Path traversal ('..') is not allowed.")
        skill_dir = (self.root / Path(skill.relative_path).parent).resolve()
        target = (skill_dir / file_path).resolve()
        err = validate_within_dir(target, skill_dir)
        if err is not None:
            raise SkillValidationError(err)
        if not target.is_file():
            raise FileNotFoundError("file not found")
        data = target.read_bytes()
        if len(data) > self.config.max_view_bytes:
            cut = self.config.max_view_bytes
            return (
                data[:cut].decode("utf-8", errors="ignore")
                + f"\n...[truncated {len(data) - cut} bytes]"
            )
        return data.decode("utf-8", errors="ignore")

    async def list_linked_files(self, skill: Skill) -> dict[str, list[str]]:
        return await asyncio.to_thread(self._list_linked_files_sync, skill)

    def _list_linked_files_sync(self, skill: Skill) -> dict[str, list[str]]:
        skill_dir = (self.root / Path(skill.relative_path).parent).resolve()
        out: dict[str, list[str]] = {key: [] for key in LINKED_DIRS}
        for sub in LINKED_DIRS:
            d = skill_dir / sub
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        out[sub].append(str(p.relative_to(skill_dir)))
        return out

    async def write_skill_file(self, skill: Skill, content: str) -> None:
        await asyncio.to_thread(self._write_skill_file_sync, skill, content)

    def _write_skill_file_sync(self, skill: Skill, content: str) -> None:
        from app.infrastructure.path_security import validate_within_dir

        skill_file = self.root / skill.relative_path
        err = validate_within_dir(skill_file, self.root)
        if err is not None:
            raise SkillValidationError(err)
        if skill_file.is_symlink():
            raise SkillValidationError("symlink_not_allowed")
        if not content.strip():
            # Defense-in-depth: manage_skill already rejects empty EDIT/CREATE
            # content, but never let an empty write through to disk -- it would
            # silently destroy the SKILL.md.
            raise SkillValidationError("empty_content")
        # Fail-fast injection check on the original content.
        for pattern in INJECTION_PATTERNS:
            if pattern.search(content):
                raise SkillValidationError("injection_detected")
        # Normalize frontmatter (sinks legacy fields into metadata, stable
        # order). Raises SkillScanError on non-mapping YAML frontmatter.
        try:
            final_content = _normalize_skill_content(content)
        except SkillScanError as exc:
            raise SkillValidationError(str(exc)) from exc
        # Defense-in-depth: re-check injection on the normalized content.
        for pattern in INJECTION_PATTERNS:
            if pattern.search(final_content):
                raise SkillValidationError("injection_detected")
        _atomic_write(skill_file, final_content)

    async def patch_skill_file(
        self, skill: Skill, old_string: str, new_string: str
    ) -> None:
        await asyncio.to_thread(
            self._patch_skill_file_sync, skill, old_string, new_string
        )

    def _patch_skill_file_sync(
        self, skill: Skill, old_string: str, new_string: str
    ) -> None:
        from app.infrastructure.path_security import validate_within_dir

        skill_file = self.root / skill.relative_path
        err = validate_within_dir(skill_file, self.root)
        if err is not None:
            raise SkillValidationError(err)
        if skill_file.is_symlink():
            raise SkillValidationError("symlink_not_allowed")
        content = skill_file.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            raise SkillPatchConflictError("not_found")
        if count > 1:
            raise SkillPatchConflictError("not_unique")
        candidate = content.replace(old_string, new_string)
        # Determine whether the frontmatter is affected. The patch touches
        # frontmatter if old_string appears within the frontmatter region OR
        # the parsed frontmatter dict changes (e.g. a body edit that
        # introduces/removes a '---' fence).
        fm_region = _frontmatter_region(content)
        frontmatter_touched = fm_region is not None and old_string in fm_region
        frontmatter_changed = False
        if not frontmatter_touched:
            try:
                fm_current, _ = _split_frontmatter(content)
                fm_candidate, _ = _split_frontmatter(candidate)
                frontmatter_changed = fm_candidate != fm_current
            except SkillScanError:
                # Corrupt frontmatter in the original file; cannot compare.
                # Treat as unchanged to preserve body-only patch semantics.
                frontmatter_changed = False
        if frontmatter_touched or frontmatter_changed:
            try:
                final_content = _normalize_skill_content(candidate)
            except SkillScanError as exc:
                raise SkillValidationError(str(exc)) from exc
        else:
            final_content = candidate
        # Injection guard on the final content (must not bypass).
        for pattern in INJECTION_PATTERNS:
            if pattern.search(final_content):
                raise SkillValidationError("injection_detected")
        _atomic_write(skill_file, final_content)

    async def delete_skill(self, skill: Skill) -> None:
        await asyncio.to_thread(self._delete_skill_sync, skill)

    def _delete_skill_sync(self, skill: Skill) -> None:
        skill_rel = Path(skill.relative_path)
        skill_dir = (self.root / skill_rel.parent).resolve()
        if not skill_dir.exists():
            raise FileNotFoundError(f"skill dir not found: {skill_dir}")
        archive_root = self.root / ".archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        suffix = datetime.now(timezone.utc).isoformat()
        dest = archive_root / f"{skill.name}-{suffix}"
        shutil.move(str(skill_dir), str(dest))

    async def write_linked_file(
        self, skill: Skill, file_path: str, content: str
    ) -> None:
        await asyncio.to_thread(
            self._write_linked_file_sync, skill, file_path, content
        )

    def _write_linked_file_sync(
        self, skill: Skill, file_path: str, content: str
    ) -> None:
        from app.infrastructure.path_security import (
            has_traversal_component,
            validate_within_dir,
        )

        if has_traversal_component(file_path):
            raise SkillValidationError("Path traversal ('..') is not allowed.")
        parts = Path(file_path).parts
        if not parts or parts[0] not in LINKED_DIRS:
            raise SkillValidationError("linked_file_dir_not_allowed")
        skill_dir = (self.root / Path(skill.relative_path).parent).resolve()
        target = (skill_dir / file_path).resolve()
        err = validate_within_dir(target, skill_dir)
        if err is not None:
            raise SkillValidationError(err)
        if target.is_symlink():
            raise SkillValidationError("symlink_not_allowed")
        _atomic_write(target, content)

    async def remove_linked_file(self, skill: Skill, file_path: str) -> None:
        await asyncio.to_thread(self._remove_linked_file_sync, skill, file_path)

    def _remove_linked_file_sync(self, skill: Skill, file_path: str) -> None:
        from app.infrastructure.path_security import (
            has_traversal_component,
            validate_within_dir,
        )

        if has_traversal_component(file_path):
            raise SkillValidationError("Path traversal ('..') is not allowed.")
        parts = Path(file_path).parts
        if not parts or parts[0] not in LINKED_DIRS:
            raise SkillValidationError("linked_file_dir_not_allowed")
        skill_dir = (self.root / Path(skill.relative_path).parent).resolve()
        target = (skill_dir / file_path).resolve()
        err = validate_within_dir(target, skill_dir)
        if err is not None:
            raise SkillValidationError(err)
        if not target.exists():
            raise FileNotFoundError(f"linked file not found: {target}")
        archive_root = self.root / ".archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        suffix = datetime.now(timezone.utc).isoformat()
        dest_dir = archive_root / f"{skill.name}-{suffix}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(dest))

    async def restore_skill(self, name: str) -> Path:
        return await asyncio.to_thread(self._restore_skill_sync, name)

    def _restore_skill_sync(self, name: str) -> Path:
        archive_root = self.root / ".archive"
        if not archive_root.exists():
            raise FileNotFoundError(f"archive root not found: {archive_root}")
        candidates: list[Path] = []
        for p in archive_root.iterdir():
            if not p.is_dir():
                continue
            parsed = _split_archive_name(p.name)
            if parsed is not None and parsed[0] == name:
                candidates.append(p)
        candidates.sort(reverse=True)
        if not candidates:
            raise FileNotFoundError(f"archived skill not found: {name}")
        src = candidates[0]
        dest = self.root / name
        if dest.exists():
            raise FileExistsError(f"destination exists: {dest}")
        shutil.move(str(src), str(dest))
        return dest

    async def list_archived(self) -> list[dict]:
        return await asyncio.to_thread(self._list_archived_sync)

    def _list_archived_sync(self) -> list[dict]:
        archive_root = self.root / ".archive"
        if not archive_root.exists():
            return []
        results: list[dict] = []
        for p in sorted(archive_root.iterdir()):
            if not p.is_dir():
                continue
            parsed = _split_archive_name(p.name)
            if parsed is None:
                continue
            skill_name, ts = parsed
            results.append(
                {
                    "name": skill_name,
                    "archive_path": str(p),
                    "archived_at": ts,
                }
            )
        return results


_ARCHIVE_TS_RE = re.compile(r"^(?P<name>.+)-(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.+\-]+)$")


def _split_archive_name(dir_name: str) -> tuple[str, str] | None:
    """从 archive 目录名分离 skill name 与 ISO 时间戳。

    delete_skill 归档目录名为 <name>-<utc-iso>，ISO 时间戳形如
    2026-07-17T10:30:00.123456+00:00（自身含多个连字符）。用正则按时间戳
    后缀切分，正确处理含连字符的 skill name（如 deploy-staging），不能用
    rsplit("-", 1) 或 startswith(f"{name}-")。
    """
    m = _ARCHIVE_TS_RE.match(dir_name)
    if m is None:
        return None
    return m.group("name"), m.group("ts")


def _iter_skill_files(root: Path) -> list[Path]:
    matches: list[Path] = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if "SKILL.md" in files:
            matches.append(Path(dirpath) / "SKILL.md")
    matches.sort(key=lambda p: str(p.relative_to(root)))
    return matches


def _seed_dir_names() -> set[str]:
    """Return the set of top-level directory names under the bundled seeds dir.

    Used during scan to tag skills whose top-level dir matches a seed as
    ``SkillSource.SEED``. Returns an empty set if the seeds dir is missing.
    """
    seeds_dir = Path(__file__).parent / "seeds"
    if not seeds_dir.is_dir():
        return set()
    return {p.name for p in seeds_dir.iterdir() if p.is_dir()}


def _resolve_source(fm_dict: dict[str, Any], default: SkillSource) -> SkillSource:
    """Resolve SkillSource from frontmatter ``metadata.source`` (validated),
    falling back to *default* when absent or invalid."""
    raw = metadata_get(fm_dict, "source")
    if raw:
        try:
            return SkillSource(str(raw).strip())
        except ValueError:
            return default
    return default


def _atomic_write(target: Path, content: str) -> None:
    """Atomically write ``content`` (utf-8) to ``target``.

    Writes to a temp file in the same directory, fsyncs, then ``os.replace``.
    The temp file is created with mode 0600 (no execute bit), so script writes
    never grant execute permission.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".__tmp_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw, dict):
        raise SkillScanError("frontmatter is not a mapping")
    return raw, parts[2].lstrip("\n")


def _dump_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize a normalized frontmatter dict to a YAML string.

    ``sort_keys=False`` preserves the stable order produced by
    ``normalize_frontmatter``; ``default_flow_style=False`` emits readable
    block style. The result has no trailing newline.
    """
    return yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()


def _frontmatter_region(text: str) -> str | None:
    """Return the frontmatter fence text (including the ``---`` delimiters)
    or ``None`` if *text* has no frontmatter block."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return "---" + parts[1] + "---"


def _normalize_skill_content(content: str) -> str:
    """Normalize the frontmatter of *content*; preserve the body verbatim.

    Legacy top-level fields are sunk into ``metadata`` and the frontmatter is
    re-serialized in stable order (only whitelist keys, empty fields omitted).

    *Body preservation*: the exact text following the closing ``---`` --
    including leading blank lines -- is kept unchanged. Only the frontmatter
    block is re-serialized.

    *No-frontmatter files*: if *content* has no frontmatter block or an empty
    frontmatter mapping, it is returned unchanged so that body-only files are
    never given a synthetic frontmatter block.

    Raises ``SkillScanError`` if the frontmatter YAML is a non-mapping.
    """
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
    raw = yaml.safe_load(parts[1]) or {}
    if not isinstance(raw, dict):
        raise SkillScanError("frontmatter is not a mapping")
    if not raw:
        # Empty frontmatter mapping (e.g. ``---\n---\nbody``). Return as-is.
        return content
    try:
        normalized = normalize_frontmatter(raw)
    except SkillFormatError:
        normalized = raw
    raw_body = parts[2]
    if raw_body and not raw_body.startswith("\n"):
        raw_body = "\n" + raw_body
    if not normalized:
        # All fields were unknown/dropped; write body without a frontmatter
        # block rather than an empty ``---\n{}\n---`` fence.
        return raw_body.lstrip("\n")
    yaml_str = _dump_frontmatter(normalized)
    return f"---\n{yaml_str}\n---{raw_body}"


def _format_detail(errors: list[str], warnings: list[str]) -> str:
    """Combine format errors and warnings into a detail string (max 500 chars).

    Errors come first, then warnings, joined by `` | ``.
    """
    parts = list(errors) + list(warnings)
    return " | ".join(parts)[:500]


_INLINE_SHELL_RE = re.compile(r"!`([^`]+)`")


def _expand_inline_shell(text: str, cwd: Path, timeout: int) -> str:
    def repl(match: re.Match[str]) -> str:
        cmd = match.group(1)
        try:
            result = subprocess.run(
                ["bash", "-c", cmd], cwd=str(cwd), capture_output=True,
                text=True, timeout=timeout, check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return output[:4000]
        except Exception as exc:
            return f"[inline-shell error: {exc}]"

    return _INLINE_SHELL_RE.sub(repl, text)
