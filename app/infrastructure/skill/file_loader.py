from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillReadiness,
    SkillScanError,
    SkillValidationError,
)


EXCLUDED_SKILL_DIRS = {".git", ".github", ".hub", ".archive"}
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
        skills: list[Skill] = []
        warnings: list[SkillScanWarning] = []
        seen_names: dict[str, str] = {}
        count = 0
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
            raw_platforms = fm_dict.get("platforms")
            if not raw_platforms:
                platforms: list[str] = []
            elif isinstance(raw_platforms, list):
                platforms = [str(p) for p in raw_platforms]
            else:
                platforms = [str(raw_platforms)]
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
            fm = _frontmatter_from_dict(fm_dict, name, platforms)
            skill = Skill(
                id=str(uuid4()), name=name, relative_path=rel,
                description=fm.description, platforms=platforms,
                frontmatter=fm, enabled=True, readiness=readiness,
                last_scan_status="ok" if last_scan_error is None else "warning",
                last_scan_error=last_scan_error,
                last_seen_at=None, created_at=None, updated_at=None,
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


def _iter_skill_files(root: Path) -> list[Path]:
    matches: list[Path] = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if "SKILL.md" in files:
            matches.append(Path(dirpath) / "SKILL.md")
    matches.sort(key=lambda p: str(p.relative_to(root)))
    return matches


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


def _frontmatter_from_dict(
    raw: dict[str, Any], fallback_name: str, platforms: list[str]
) -> SkillFrontmatter:
    return SkillFrontmatter(
        name=str(raw.get("name") or fallback_name),
        description=str(raw.get("description") or ""),
        version=str(raw.get("version") or ""),
        platforms=list(platforms),
        tags=list(raw.get("tags") or []),
        related_skills=list(raw.get("related_skills") or []),
        author=str(raw.get("author") or ""),
        license=str(raw.get("license") or ""),
        setup_help=raw.get("setup_help"),
        required_env_vars=list(raw.get("required_env_vars") or []),
        raw=raw,
    )


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
