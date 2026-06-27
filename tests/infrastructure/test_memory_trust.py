import json
import time

import pytest

from app.infrastructure.memory.trust import MemoryTrustStore, entry_hash


@pytest.fixture
def store(tmp_path):
    s = MemoryTrustStore(
        tmp_path / "memory.meta.json",
        default_trust=0.5,
        temporal_decay_half_life_days=30,
        contradiction_overlap_threshold=0.4,
        duplicate_overlap_threshold=0.85,
        contradiction_trust_delta=-0.3,
        prefetch_hit_trust_boost=0.05,
        meta_flush_interval_seconds=60,
    )
    s.load()
    return s


def test_load_missing_meta_starts_empty(store):
    assert store.all_hashes() == []
    assert store.is_dirty() is False


def test_load_existing_meta(tmp_path):
    meta_file = tmp_path / "memory.meta.json"
    h = entry_hash("hello")
    meta_file.write_text(json.dumps({
        "version": 1,
        "entries": {h: {"trust": 0.7, "created_at": "2026-06-01T00:00:00Z", "last_hit_at": "2026-06-20T00:00:00Z"}},
    }), encoding="utf-8")
    s = MemoryTrustStore(meta_file)
    s.load()
    meta = s.get(h)
    assert meta is not None
    assert meta.trust == 0.7
    assert meta.last_hit_at == "2026-06-20T00:00:00Z"


def test_load_corrupt_meta_starts_empty(tmp_path):
    meta_file = tmp_path / "memory.meta.json"
    meta_file.write_text("not json", encoding="utf-8")
    s = MemoryTrustStore(meta_file)
    s.load()
    assert s.all_hashes() == []


def test_ensure_creates_with_default_trust(store):
    h = entry_hash("entry1")
    now = "2026-06-28T10:00:00Z"
    meta = store.ensure(h, now=now)
    assert meta.trust == 0.5
    assert meta.created_at == now
    assert meta.last_hit_at == now
    # idempotent
    meta2 = store.ensure(h, now="2026-06-28T11:00:00Z")
    assert meta2 is meta


def test_score_with_trust_and_decay(store):
    h = entry_hash("entry")
    now = "2026-06-28T00:00:00Z"
    store.ensure(h, now=now)
    store.demote(h, -0.3)  # trust 0.5 -> 0.2
    # age 30 days == half_life -> decay 0.5
    score = store.score(h, relevance=1.0, now="2026-07-28T00:00:00Z")
    assert score == pytest.approx(0.2 * 0.5, rel=1e-3)


def test_score_no_decay_when_half_life_zero(tmp_path):
    s = MemoryTrustStore(tmp_path / "m.json", temporal_decay_half_life_days=0)
    s.load()
    h = entry_hash("e")
    s.ensure(h, now="2026-06-28T00:00:00Z")
    score = s.score(h, relevance=0.8, now="2027-06-28T00:00:00Z")
    # decay disabled -> 0.8 * 0.5 * 1.0
    assert score == pytest.approx(0.4, rel=1e-3)


def test_score_missing_meta_uses_default_trust(store):
    h = entry_hash("ghost")
    score = store.score(h, relevance=1.0, now="2026-06-28T00:00:00Z")
    assert score == pytest.approx(0.5, rel=1e-3)


def test_detect_contradiction_duplicate(store):
    existing = ["Python 项目使用 FastAPI 框架"]
    verdict, contradicted = store.detect_contradiction(
        "Python 项目使用 FastAPI 框架", existing
    )
    assert verdict == "duplicate"
    assert contradicted == entry_hash(existing[0])


def test_detect_contradiction_contradict(store):
    existing = ["Python 项目使用 FastAPI 框架"]
    verdict, contradicted = store.detect_contradiction(
        "Python 项目使用 Flask 框架", existing
    )
    assert verdict == "contradict"
    assert contradicted == entry_hash(existing[0])


def test_detect_contradiction_add(store):
    # Genuinely unrelated entries — character bigram Jaccard near zero
    existing = ["Python 项目使用 FastAPI 框架"]
    verdict, _ = store.detect_contradiction(
        "今天天气晴朗适合户外活动", existing
    )
    assert verdict == "add"


def test_detect_contradiction_empty_existing(store):
    verdict, contradicted = store.detect_contradiction("anything", [])
    assert verdict == "add"
    assert contradicted is None


def test_boost_on_hit_clamps_trust(store):
    h = entry_hash("e")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    # boost until clamp
    for _ in range(20):
        store.boost_on_hit(h, now="2026-06-28T01:00:00Z")
    assert store.get(h).trust == 1.0
    assert store.get(h).last_hit_at == "2026-06-28T01:00:00Z"


def test_demote_clamps_trust(store):
    h = entry_hash("e")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    store.demote(h, -0.9)
    assert store.get(h).trust == 0.0


def test_prune_removes_stale(store):
    h1 = entry_hash("alive")
    h2 = entry_hash("dead")
    store.ensure(h1, now="2026-06-28T00:00:00Z")
    store.ensure(h2, now="2026-06-28T00:00:00Z")
    store.prune({h1})
    assert store.get(h1) is not None
    assert store.get(h2) is None
    assert store.is_dirty() is True


def test_remove_entry(store):
    h = entry_hash("e")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    store.remove(h)
    assert store.get(h) is None
    assert store.is_dirty() is True


def test_maybe_flush_throttle(store, tmp_path):
    h = entry_hash("e")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    # first flush should write
    flushed = store.maybe_flush(force=True)
    assert flushed is True
    assert store.is_dirty() is False
    assert (tmp_path / "memory.meta.json").exists()
    # mark dirty again, throttled flush should skip
    store.ensure(entry_hash("e2"), now="2026-06-28T01:00:00Z")
    flushed = store.maybe_flush()
    assert flushed is False
    assert store.is_dirty() is True


def test_maybe_flush_force_overrides_throttle(store, tmp_path):
    h = entry_hash("e")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    # force always flushes
    assert store.maybe_flush(force=True) is True
    store.ensure(entry_hash("e2"), now="2026-06-28T01:00:00Z")
    assert store.maybe_flush(force=True) is True


def test_maybe_flush_no_dirty_skips(store):
    assert store.maybe_flush(force=True) is False


def test_maybe_flush_zero_interval_always_flushes(tmp_path):
    s = MemoryTrustStore(tmp_path / "m.json", meta_flush_interval_seconds=0)
    s.load()
    s.ensure(entry_hash("e"), now="2026-06-28T00:00:00Z")
    # dirty + interval 0 -> should flush
    assert s.maybe_flush() is True


def test_flush_persists_to_disk(store, tmp_path):
    h = entry_hash("persisted")
    store.ensure(h, now="2026-06-28T00:00:00Z")
    store.demote(h, -0.2)
    store.maybe_flush(force=True)
    data = json.loads((tmp_path / "memory.meta.json").read_text(encoding="utf-8"))
    assert data["entries"][h]["trust"] == pytest.approx(0.3, rel=1e-3)
