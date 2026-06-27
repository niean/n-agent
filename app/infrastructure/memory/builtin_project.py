from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.domain.memory_provider import ExternalMemoryProvider
from app.infrastructure.memory.retriever import MemoryRetriever
from app.infrastructure.memory.trust import MemoryTrustStore, entry_hash


logger = logging.getLogger(__name__)


# Safe scan patterns for prompt injection
_PROHIBITED_PATTERNS = [
    re.compile(r'ignore\s+previous\s+instructions', re.IGNORECASE),
    re.compile(r'you\s+are\s+now', re.IGNORECASE),
    re.compile(r'do\s+not\s+tell', re.IGNORECASE),
    re.compile(r'do\s+not\s+reveal', re.IGNORECASE),
    re.compile(r'[​-‏﻿]'),  # Zero-width spaces and invisible characters
]

# Entry separator
ENTRY_SEPARATOR = "\n---\n"
OBSERVATIONS_SEPARATOR = "\n---\n"

# sync_turn keyword extraction config
_OBSERVATIONS_CHAR_LIMIT = 4000
_OBSERVATIONS_MAX_KEYWORDS = 5
_OBSERVATIONS_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"

# ASCII identifiers (2-31 chars, start with letter) or CJK runs
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,30}|[一-鿿]+")

_STOPWORDS = frozenset({
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "of", "to", "in", "on", "at", "by",
    "for", "with", "about", "as", "from", "up", "down", "out", "into", "over",
    "i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those",
    "do", "does", "did", "have", "has", "had", "will", "would", "could", "should",
    "yes", "no", "not", "ok", "okay", "please", "thanks", "thank",
    # Chinese common particles / aux
    "的", "了", "是", "在", "和", "与", "或", "也", "都", "就", "还", "又",
    "我", "你", "他", "她", "它", "这", "那", "哪", "谁", "什么", "怎么",
    "不", "没", "没有", "有", "一个", "一些",
    # Chinese fillers (no semantic content)
    "嗯", "好的", "谢谢", "好", "吧", "呢", "啊", "哦", "哈", "嘿", "对",
})


def _extract_keywords(text: str, max_keywords: int = _OBSERVATIONS_MAX_KEYWORDS) -> list[str]:
    """Extract top-N keywords from text by frequency.

    Tokenizes ASCII identifiers and CJK runs; filters stopwords; returns
    most-common keywords. Used by sync_turn to persist lightweight
    per-turn observations without an LLM call.
    """
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    counter = Counter(t for t in tokens if t not in _STOPWORDS)
    return [kw for kw, _ in counter.most_common(max_keywords)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BuiltinProjectMemory(ExternalMemoryProvider):
    """系统记忆文本文件存储。

    存储位置: {project_root}/{memory_path}/
    - memory.md — 系统知识：架构约定、编码规范、常见问题、经验教训
    - user.md — 用户偏好（这个系统记忆下的沟通风格、工作习惯，可选）
    """

    def __init__(
        self,
        project_root: Path,
        memory_path: str = "./locals/external-memory",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        observations_char_limit: int = _OBSERVATIONS_CHAR_LIMIT,
        *,
        default_trust: float = 0.5,
        temporal_decay_half_life_days: int = 0,
        contradiction_overlap_threshold: float = 0.4,
        duplicate_overlap_threshold: float = 0.85,
        contradiction_trust_delta: float = -0.3,
        prefetch_hit_trust_boost: float = 0.05,
        system_prompt_min_trust: float = 0.3,
        meta_flush_interval_seconds: int = 60,
    ):
        self._project_root = project_root
        self._memory_path = memory_path
        self._memory_char_limit = memory_char_limit
        self._user_char_limit = user_char_limit
        self._observations_char_limit = observations_char_limit
        self._system_prompt_min_trust = system_prompt_min_trust
        self._memory_dir = self._project_root / self._memory_path
        resolved_memory_dir = self._memory_dir.resolve()
        resolved_project_root = self._project_root.resolve()
        if not str(resolved_memory_dir).startswith(str(resolved_project_root) + os.path.sep):
            raise ValueError("memory_path must stay within project_root")
        self._memory_dir = resolved_memory_dir
        self._memory_file = self._memory_dir / "memory.md"
        self._user_file = self._memory_dir / "user.md"
        self._observations_file = self._memory_dir / "observations.md"
        self._meta_file = self._memory_dir / "memory.meta.json"
        self._memory_content: str = ""
        self._user_content: str = ""
        self._observations_content: str = ""
        self._trust_store = MemoryTrustStore(
            self._meta_file,
            default_trust=default_trust,
            temporal_decay_half_life_days=temporal_decay_half_life_days,
            contradiction_overlap_threshold=contradiction_overlap_threshold,
            duplicate_overlap_threshold=duplicate_overlap_threshold,
            contradiction_trust_delta=contradiction_trust_delta,
            prefetch_hit_trust_boost=prefetch_hit_trust_boost,
            meta_flush_interval_seconds=meta_flush_interval_seconds,
        )

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize: create directory if not exists, load snapshot."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        if self._memory_file.exists():
            self._memory_content = self._read_file(self._memory_file)
        if self._user_file.exists():
            self._user_content = self._read_file(self._user_file)
        if self._observations_file.exists():
            self._observations_content = self._read_file(self._observations_file)
        self._trust_store.load()
        self._sync_trust_to_memory()

    def _sync_trust_to_memory(self) -> None:
        """Prune stale trust entries and ensure all current memory.md entries
        have meta (so demote/boost_on_hit never silently no-op on existing
        content)."""
        live = {entry_hash(e) for e in self._split_entries(self._memory_content)}
        self._trust_store.prune(live)
        now = _now_iso()
        for h in live:
            self._trust_store.ensure(h, now=now)

    def system_prompt_block(self) -> str:
        """Return filtered + trust-ranked snapshot of stable external memory.

        Low-trust / contradicted entries are filtered out; remaining entries
        are sorted by trust x temporal_decay so the model sees the most
        credible facts first. Meta fields are never injected — only the
        original memory.md entry text.
        """
        blocks: list[str] = []
        filtered = self._filtered_ranked_memory()
        if filtered:
            blocks.append("## External Stable Memory\n\n" + filtered)
        if self._user_content.strip():
            blocks.append("## User Preferences (this external memory)\n\n" + self._user_content.strip())
        if not blocks:
            return ""
        return "\n\n".join(blocks)

    def _filtered_ranked_memory(self) -> str:
        """Filter by system_prompt_min_trust and rank by trust x decay."""
        if not self._memory_content.strip():
            return ""
        entries = self._split_entries(self._memory_content)
        if not entries:
            return ""
        now = _now_iso()
        scored: list[tuple[float, str]] = []
        for entry in entries:
            h = entry_hash(entry)
            meta = self._trust_store.ensure(h, now=now)
            if meta.trust < self._system_prompt_min_trust:
                continue
            final = self._trust_store.score(h, relevance=1.0, now=now)
            scored.append((final, entry))
        if not scored:
            return ""
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return "\n---\n".join(entry for _, entry in scored)

    def prefetch(self, query: str, *, session_id: str) -> str:
        """Retrieve relevant entries from memory.md based on query.

        Reranks MemoryRetriever results by relevance x trust x temporal_decay.
        Hit entries get last_hit_at + trust boost (throttled persistence).
        """
        if not query or not self._memory_content.strip():
            return ""
        entries = self._split_entries(self._memory_content)
        if not entries:
            return ""
        retriever = MemoryRetriever(max_results=3, min_score=0.3)
        results = retriever.retrieve(query, entries)
        if not results:
            return ""
        now = _now_iso()
        scored: list[tuple[float, str]] = []
        for entry, relevance in results:
            h = entry_hash(entry)
            self._trust_store.ensure(h, now=now)
            self._trust_store.boost_on_hit(h, now=now)
            final = self._trust_store.score(h, relevance=relevance, now=now)
            scored.append((final, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        top_k = retriever.max_results
        top = scored[:top_k]
        self._trust_store.maybe_flush()
        return "\n---\n".join(entry for _, entry in top)

    def queue_prefetch(self, query: str, *, session_id: str) -> None:
        """No-op for builtin."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str) -> None:
        """Persist lightweight per-turn observations to observations.md.

        Auto-extracts keywords from this turn's user+assistant content and
        appends a timestamped entry to observations.md, separate from curated
        memory.md. observations.md is intentionally NOT surfaced in
        system_prompt_block or prefetch — it is raw auto-extracted signal,
        kept around for future compaction/UI use, never injected into LLM
        context directly.

        Failures are swallowed: sync_turn runs in finalize and must not block
        the response path. Manager already wraps in try/except; this inner
        guard is defense in depth.
        """
        try:
            combined = f"{user_content}\n{assistant_content}"
            keywords = _extract_keywords(combined)
            if not keywords:
                return
            timestamp = datetime.now(timezone.utc).strftime(_OBSERVATIONS_TIMESTAMP_FMT)
            entry = f"[{timestamp}] {', '.join(keywords)}"
            ok, error = self._safe_scan(entry)
            if not ok:
                logger.debug(
                    "sync_turn: skipping observation, safe_scan failed: %s", error
                )
                return
            self._append_observation(entry)
            logger.debug("sync_turn: appended observation to %s", self._observations_file)
        except Exception as exc:
            logger.warning("sync_turn: failed to append observation", exc_info=exc)

    def _append_observation(self, entry: str) -> None:
        """Append entry to observations.md with hard size-bounded rotation.

        When content exceeds 2x limit, drop oldest entries until under 1x limit.
        Uses the same _update_file_locked path as curated memory writes for
        crash safety and concurrency.
        """
        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if current.strip():
                new_content = current.rstrip() + OBSERVATIONS_SEPARATOR + entry
            else:
                new_content = entry
            if len(new_content) > self._observations_char_limit * 2:
                entries = [
                    e.strip() for e in new_content.split(OBSERVATIONS_SEPARATOR) if e.strip()
                ]
                while (
                    entries
                    and len(OBSERVATIONS_SEPARATOR.join(entries)) > self._observations_char_limit
                ):
                    entries.pop(0)
                new_content = OBSERVATIONS_SEPARATOR.join(entries)
            return {"success": True}, new_content

        result = self._update_file_locked(self._observations_file, update)
        if result.get("success"):
            self._observations_content = self._read_file(self._observations_file)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Expose external_memory tool for LLM to manage memory."""
        return [
            {
                "name": "external_memory",
                "description": (
                    "Add, replace, or remove stable external knowledge in persistent memory. "
                    "This memory is available across all sessions. "
                    "\n\n**WHEN TO USE (do this proactively, don't wait to be asked):**\n"
                    "- User corrects you or says \"remember this\" / \"don't do that again\"\n"
                    "- User shares conventions, preferences, or workflow habits\n"
                    "- You discover something about the environment (OS, installed tools, structure)\n"
                    "- You learn a convention, API quirk, or recurring solution\n"
                    "- You identify a stable fact that will be useful again in *future sessions*\n"
                    "\n**Writing is only allowed in interactive (primary) sessions. "
                    "Unattended/cron jobs will be rejected.**"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "replace", "remove"],
                            "description": "Action to perform: add new entry, replace matching entry, remove matching entry",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["memory", "user"],
                            "description": "Target file: memory (project knowledge) or user (user preferences)",
                            "default": "memory",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to add or replace (required for add/replace)",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Substring to match for replace/remove (required for replace/remove)",
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        ]

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs,
    ) -> str:
        """Handle external_memory tool call."""
        if tool_name != "external_memory":
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})

        agent_context = kwargs.get("agent_context", "unattended")
        if agent_context != "primary":
            return json.dumps({
                "success": False,
                "error": "write permission denied: only interactive primary sessions can modify external memory",
            })

        action = args.get("action")
        target = args.get("target", "memory")
        if target not in ["memory", "user"]:
            return json.dumps({"success": False, "error": f"invalid target {target}, must be memory or user"})

        # Validate content based on action
        if action in ["add", "replace"]:
            content = args.get("content")
            if not content or not isinstance(content, str):
                return json.dumps({"success": False, "error": "content is required for add/replace"})
            # Safety scan
            ok, error = self._safe_scan(content)
            if not ok:
                return json.dumps({"success": False, "error": f"security check failed: {error}"})

        if action in ["replace", "remove"]:
            old_text = args.get("old_text")
            if not old_text or not isinstance(old_text, str):
                return json.dumps({"success": False, "error": "old_text is required for replace/remove"})

        try:
            if action == "add":
                content = args["content"]
                result = self._add_entry(target, content)
            elif action == "replace":
                content = args["content"]
                old_text = args["old_text"]
                result = self._replace_entry(target, old_text, content)
            elif action == "remove":
                old_text = args["old_text"]
                result = self._remove_entry(target, old_text)
            else:
                return json.dumps({"success": False, "error": f"unknown action {action}"})
        except Exception as exc:
            logger.warning("project_memory tool call failed", exc_info=exc)
            return json.dumps({"success": False, "error": "io error"})

        if result["success"]:
            # Reload snapshot after successful write
            self._reload_snapshot()
            # Check size limit after write
            limit_ok, limit_error = self._check_size_limit()
            if not limit_ok:
                result["warning"] = limit_error
            return json.dumps(result)
        else:
            return json.dumps(result)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
    ) -> None:
        """No-op for builtin."""
        pass

    def on_session_end(self, session_id: str) -> None:
        """No-op for builtin; interface in place per G5."""
        pass

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """No-op for builtin."""
        pass

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str | None:
        """No-op for builtin; interface in place per G3."""
        return None

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """No-op for builtin; interface in place per G7.

        子 Agent 完成回调由父会话触发，builtin 不主动落盘子任务产出。
        """
        pass

    def shutdown(self) -> None:
        """Flush pending trust meta as a best-effort fallback."""
        try:
            self._trust_store.maybe_flush(force=True)
        except Exception as exc:
            logger.debug("trust store shutdown flush failed", exc_info=exc)

    def _read_file(self, path: Path) -> str:
        """Read file content."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def _update_file_locked(
        self,
        path: Path,
        update_fn: Callable[[str], tuple[dict[str, Any], str | None]],
    ) -> dict[str, Any]:
        """Run a read-modify-write update while holding an exclusive target-file lock."""
        temp_path = path.with_suffix(".tmp")
        while True:
            with open(path, "a+", encoding="utf-8") as target_file:
                fcntl.flock(target_file, fcntl.LOCK_EX)

                # If another writer replaced the target between open() and flock(),
                # retry on the current target inode so the read is not stale.
                fd_stat = os.fstat(target_file.fileno())
                path_stat = path.stat()
                if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                    # continue exits the with block first, releasing this stale file lock before retrying.
                    continue

                target_file.seek(0)
                current = target_file.read()

                result, new_content = update_fn(current)
                if not result.get("success") or new_content is None:
                    return result

                with open(temp_path, "w", encoding="utf-8") as temp_file:
                    temp_file.write(new_content)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                # Rename (atomic on POSIX) while the target file lock is held.
                os.replace(temp_path, path)
                return result

    def _safe_scan(self, content: str) -> tuple[bool, str]:
        """Scan content for injection patterns and invisible characters."""
        for pattern in _PROHIBITED_PATTERNS:
            match = pattern.search(content)
            if match:
                return False, f"prohibited pattern found: {match.group(0)}"
        return True, ""

    def _check_size_limit(self) -> tuple[bool, str]:
        """Check character limits."""
        if len(self._memory_content) > self._memory_char_limit * 2:
            return False, f"memory.md exceeds character limit ({self._memory_char_limit}), consider removing outdated entries"
        if len(self._user_content) > self._user_char_limit * 2:
            return False, f"user.md exceeds character limit ({self._user_char_limit}), consider removing outdated entries"
        return True, ""

    def _reload_snapshot(self) -> None:
        """Reload snapshot after write."""
        self._memory_content = self._read_file(self._memory_file)
        self._user_content = self._read_file(self._user_file)
        self._sync_trust_to_memory()

    def _prune_trust_to_memory(self) -> None:
        """Drop trust entries whose hash no longer exists in memory.md."""
        live = {entry_hash(e) for e in self._split_entries(self._memory_content)}
        self._trust_store.prune(live)

    def _add_entry(self, target: str, content: str) -> dict[str, Any]:
        """Add a new entry. For memory target, runs contradiction detection
        inside the file lock to avoid TOCTOU races under concurrent add."""
        file_path = self._memory_file if target == "memory" else self._user_file
        is_memory = target == "memory"

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if is_memory:
                existing = self._split_entries(current)
                verdict, contradicted = self._trust_store.detect_contradiction(content, existing)
                if verdict == "duplicate":
                    return {"success": False, "error": "duplicate entry"}, None
                now = _now_iso()
                if verdict == "contradict" and contradicted is not None:
                    self._trust_store.demote(contradicted, self._trust_store._contradiction_delta)
                new_entry_text = content.lstrip()
                new_hash = entry_hash(new_entry_text)
                self._trust_store.ensure(new_hash, now=now)
                new_content = (
                    current.rstrip() + ENTRY_SEPARATOR + new_entry_text
                    if current.strip()
                    else new_entry_text
                )
                result: dict[str, Any] = {"success": True, "message": f"added entry to {target}"}
                if verdict == "contradict":
                    result["warning"] = "contradiction detected, old entry trust demoted"
                return result, new_content
            # user.md: no trust tracking
            if current.strip():
                new_content = current.rstrip() + ENTRY_SEPARATOR + content.lstrip()
            else:
                new_content = content
            return {"success": True, "message": f"added entry to {target}"}, new_content

        result = self._update_file_locked(file_path, update)
        if result.get("success") and is_memory:
            self._trust_store.maybe_flush(force=True)
        return result

    def _replace_entry(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Replace entry containing old_text."""
        file_path = self._memory_file if target == "memory" else self._user_file
        is_memory = target == "memory"

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "old_text not found in target"}, None
            entries = self._split_entries(current)
            replaced_hash: str | None = None
            new_entries: list[str] = []
            replaced = False
            for entry in entries:
                if not replaced and old_text in entry:
                    replaced = True
                    if is_memory:
                        replaced_hash = entry_hash(entry)
                    continue
                new_entries.append(entry)
            if not replaced:
                return {"success": False, "error": "entry containing old_text not found"}, None
            new_entry_text = new_content.lstrip()
            if is_memory:
                now = _now_iso()
                if replaced_hash is not None:
                    self._trust_store.remove(replaced_hash)
                self._trust_store.ensure(entry_hash(new_entry_text), now=now)
            new_entries.append(new_entry_text)
            new_content_complete = ENTRY_SEPARATOR.join(new_entries)
            return {"success": True, "message": f"replaced entry in {target}"}, new_content_complete

        result = self._update_file_locked(file_path, update)
        if result.get("success") and is_memory:
            self._trust_store.maybe_flush(force=True)
        return result

    def _remove_entry(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove entry containing old_text."""
        file_path = self._memory_file if target == "memory" else self._user_file
        is_memory = target == "memory"

        def update(current: str) -> tuple[dict[str, Any], str | None]:
            if old_text not in current:
                return {"success": False, "error": "old_text not found in target"}, None

            entries = self._split_entries(current)
            new_entries: list[str] = []
            removed_hashes: list[str] = []
            removed = False
            for entry in entries:
                if not removed and old_text in entry:
                    removed = True
                    if is_memory:
                        removed_hashes.append(entry_hash(entry))
                    continue
                new_entries.append(entry)

            if not removed:
                return {"success": False, "error": "entry containing old_text not found"}, None

            if is_memory:
                for h in removed_hashes:
                    self._trust_store.remove(h)
            new_content = ENTRY_SEPARATOR.join(new_entries)
            return {"success": True, "message": f"removed entry from {target}"}, new_content

        result = self._update_file_locked(file_path, update)
        if result.get("success") and is_memory:
            self._sync_trust_to_memory()
            self._trust_store.maybe_flush(force=True)
        return result

    @staticmethod
    def _split_entries(content: str) -> list[str]:
        """Split content into entries by separator."""
        if not content.strip():
            return []
        entries = content.split(ENTRY_SEPARATOR)
        return [e.strip() for e in entries if e.strip()]
