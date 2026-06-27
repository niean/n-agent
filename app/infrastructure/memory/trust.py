from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from app.infrastructure.memory.retriever import MemoryRetriever


logger = logging.getLogger(__name__)


def entry_hash(entry: str) -> str:
    return hashlib.sha1(entry.strip().encode("utf-8")).hexdigest()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamp_trust(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class EntryMeta:
    trust: float
    created_at: str
    last_hit_at: str


class MemoryTrustStore:
    """Sidecar trust/decay metadata store for Markdown-file memory providers.

    Stores per-entry trust/created_at/last_hit_at in a JSON sidecar file,
    separate from the human-readable memory.md. Provides scoring (relevance x
    trust x temporal_decay), contradiction detection via Jaccard overlap, and
    throttled persistence for prefetch-hit feedback.
    """

    def __init__(
        self,
        meta_file: Path,
        *,
        default_trust: float = 0.5,
        temporal_decay_half_life_days: int = 0,
        contradiction_overlap_threshold: float = 0.4,
        duplicate_overlap_threshold: float = 0.85,
        contradiction_trust_delta: float = -0.3,
        prefetch_hit_trust_boost: float = 0.05,
        meta_flush_interval_seconds: int = 60,
    ):
        self._meta_file = meta_file
        self._default_trust = _clamp_trust(default_trust)
        self._half_life_days = max(0, temporal_decay_half_life_days)
        self._contradiction_threshold = contradiction_overlap_threshold
        self._duplicate_threshold = duplicate_overlap_threshold
        self._contradiction_delta = contradiction_trust_delta
        self._hit_boost = prefetch_hit_trust_boost
        self._flush_interval = max(0, meta_flush_interval_seconds)
        self._entries: dict[str, EntryMeta] = {}
        self._dirty = False
        self._last_flush_ts: float = 0.0
        self._retriever = MemoryRetriever(max_results=0, min_score=0.0)

    def load(self) -> None:
        try:
            with open(self._meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._entries = {}
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("trust store load failed, starting empty: %s", exc)
            self._entries = {}
            return
        raw = data.get("entries", {}) if isinstance(data, dict) else {}
        self._entries = {}
        for h, meta in raw.items():
            if not isinstance(meta, dict):
                continue
            try:
                self._entries[h] = EntryMeta(
                    trust=_clamp_trust(float(meta.get("trust", self._default_trust))),
                    created_at=str(meta.get("created_at", "")),
                    last_hit_at=str(meta.get("last_hit_at", "")),
                )
            except (TypeError, ValueError):
                continue
        self._dirty = False

    def get(self, entry_hash: str) -> EntryMeta | None:
        return self._entries.get(entry_hash)

    def ensure(self, entry_hash: str, *, now: str) -> EntryMeta:
        meta = self._entries.get(entry_hash)
        if meta is None:
            meta = EntryMeta(trust=self._default_trust, created_at=now, last_hit_at=now)
            self._entries[entry_hash] = meta
            self._dirty = True
        return meta

    def demote(self, entry_hash: str, delta: float) -> None:
        meta = self._entries.get(entry_hash)
        if meta is None:
            return
        meta.trust = _clamp_trust(meta.trust + delta)
        self._dirty = True

    def boost_on_hit(self, entry_hash: str, *, now: str) -> None:
        meta = self._entries.get(entry_hash)
        if meta is None:
            return
        meta.trust = _clamp_trust(meta.trust + self._hit_boost)
        meta.last_hit_at = now
        self._dirty = True

    def remove(self, entry_hash: str) -> None:
        if entry_hash in self._entries:
            del self._entries[entry_hash]
            self._dirty = True

    def prune(self, live_hashes: set[str]) -> None:
        stale = [h for h in self._entries if h not in live_hashes]
        for h in stale:
            del self._entries[h]
        if stale:
            self._dirty = True

    def all_hashes(self) -> list[str]:
        return list(self._entries.keys())

    def is_dirty(self) -> bool:
        return self._dirty

    def maybe_flush(self, *, force: bool = False) -> bool:
        if not self._dirty:
            return False
        now_ts = time.monotonic()
        if not force and (now_ts - self._last_flush_ts) < self._flush_interval:
            return False
        self._write_meta()
        self._dirty = False
        self._last_flush_ts = now_ts
        return True

    def score(self, entry_hash: str, *, relevance: float, now: str) -> float:
        meta = self._entries.get(entry_hash)
        trust = meta.trust if meta else self._default_trust
        decay = self._temporal_decay(meta, now)
        return relevance * trust * decay

    def detect_contradiction(
        self,
        new_entry: str,
        existing_entries: list[str],
    ) -> tuple[str, str | None]:
        """Return (verdict, contradicted_hash | None).

        verdict in {"add", "duplicate", "contradict"}.
        """
        if not existing_entries or not new_entry.strip():
            return "add", None
        new_tokens = self._retriever._tokenize(new_entry)
        if not new_tokens:
            return "add", None
        best_overlap = 0.0
        best_hash: str | None = None
        for existing in existing_entries:
            if not existing or not existing.strip():
                continue
            existing_tokens = self._retriever._tokenize(existing)
            if not existing_tokens:
                continue
            union = new_tokens | existing_tokens
            if not union:
                continue
            overlap = len(new_tokens & existing_tokens) / len(union)
            if overlap > best_overlap:
                best_overlap = overlap
                best_hash = entry_hash(existing)
        if best_overlap >= self._duplicate_threshold:
            return "duplicate", best_hash
        if best_overlap >= self._contradiction_threshold:
            return "contradict", best_hash
        return "add", None

    def _temporal_decay(self, meta: EntryMeta | None, now: str) -> float:
        if self._half_life_days <= 0 or meta is None:
            return 1.0
        ts_str = meta.last_hit_at or meta.created_at
        if not ts_str:
            return 1.0
        age_days = self._age_days(ts_str, now)
        if age_days <= 0:
            return 1.0
        return 0.5 ** (age_days / self._half_life_days)

    @staticmethod
    def _age_days(ts_str: str, now: str) -> float:
        from datetime import datetime, timezone
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            now_dt = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        return max(0.0, (now_dt - ts).total_seconds() / 86400.0)

    def _write_meta(self) -> None:
        data = {
            "version": 1,
            "entries": {h: asdict(m) for h, m in self._entries.items()},
        }
        tmp = self._meta_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._meta_file)
        except OSError as exc:
            logger.warning("trust store write failed: %s", exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
