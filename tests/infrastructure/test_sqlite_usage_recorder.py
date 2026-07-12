import pytest

from app.domain.session import ConversationSession
from app.domain.usage import CanonicalUsage, UsageCost
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.usage.sqlite_usage_recorder import SqliteUsageRecorder


@pytest.fixture
def store(tmp_path):
    s = SQLiteMemoryStore(tmp_path / "test.db")
    return s


@pytest.mark.asyncio
async def test_record_call_persists_usage(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    # create session first
    session_id = (await store.create_session(ConversationSession(id="sess-1", title="test"))).id
    usage = CanonicalUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10)
    cost = UsageCost(amount_usd="0.005", status="estimated", pricing_version="2026-07")
    await recorder.record_call(session_id, "gpt-4o", "openai", usage, cost, latency_ms=200)
    stats = await recorder.get_session_stats(session_id)
    assert stats.input_tokens == 100
    assert stats.output_tokens == 50
    assert stats.api_call_count == 1
    # Tn = 100 + 10*0.2 + 50*5 = 100 + 2 + 250 = 352
    assert stats.normalized_tokens == 352
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].input_tokens == 100
    assert records[0].normalized_tokens == 352


@pytest.mark.asyncio
async def test_normalized_tokens_zero_for_empty_session(store, tmp_path):
    """Session with no API calls should report normalized_tokens=0."""
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-empty", title="t"))).id
    stats = await recorder.get_session_stats(session_id)
    assert stats.normalized_tokens == 0


@pytest.mark.asyncio
async def test_record_call_persists_requested_model(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-rm", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(
        session_id, "gpt-4o", "openai", usage, cost, latency_ms=10,
        requested_model="N-Agent",
    )
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].model == "gpt-4o"
    assert records[0].requested_model == "N-Agent"


@pytest.mark.asyncio
async def test_record_call_requested_model_defaults_null(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-rm2", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(session_id, "gpt-4o", "openai", usage, cost, latency_ms=10)
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].requested_model is None


@pytest.mark.asyncio
async def test_record_call_persists_trigger_type(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-tt", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(
        session_id, "gpt-4o", "openai", usage, cost, latency_ms=10,
        trigger_type="tool",
    )
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].trigger_type == "tool"


@pytest.mark.asyncio
async def test_record_call_trigger_type_defaults_null(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-tt2", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(session_id, "gpt-4o", "openai", usage, cost, latency_ms=10)
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].trigger_type is None


@pytest.mark.asyncio
async def test_record_call_persists_request_response(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-rr", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    req_json = '[{"role":"user","content":"hi"}]'
    resp_json = '{"role":"assistant","content":"hello"}'
    await recorder.record_call(
        session_id, "gpt-4o", "openai", usage, cost, latency_ms=10,
        request_messages=req_json, response_message=resp_json,
    )
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].request_messages == req_json
    assert records[0].response_message == resp_json


@pytest.mark.asyncio
async def test_record_call_persists_tools(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-tl", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    tools_json = '[{"type":"function","function":{"name":"get_time","description":"get current time","parameters":{"type":"object","properties":{}}}}]'
    await recorder.record_call(
        session_id, "gpt-4o", "openai", usage, cost, latency_ms=10,
        tools=tools_json,
    )
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].tools == tools_json


@pytest.mark.asyncio
async def test_record_call_tools_defaults_null(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-tl2", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(session_id, "gpt-4o", "openai", usage, cost, latency_ms=10)
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].tools is None


@pytest.mark.asyncio
async def test_record_call_persists_generation_params(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-gp", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    gen_json = '{"temperature":0.7,"max_tokens":4096,"top_p":1.0}'
    await recorder.record_call(
        session_id, "gpt-4o", "openai", usage, cost, latency_ms=10,
        generation_params=gen_json,
    )
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].generation_params == gen_json


@pytest.mark.asyncio
async def test_record_call_generation_params_defaults_null(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-gp2", title="t"))).id
    usage = CanonicalUsage(input_tokens=10, output_tokens=5)
    cost = UsageCost(amount_usd="0", status="unknown", pricing_version=None)
    await recorder.record_call(session_id, "gpt-4o", "openai", usage, cost, latency_ms=10)
    records = await recorder.list_records(session_id)
    assert len(records) == 1
    assert records[0].generation_params is None


@pytest.mark.asyncio
async def test_record_compression(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-2", title="test"))).id
    await recorder.record_compression(session_id, before_tokens=5000, after_tokens=2000)
    comps = await recorder.list_compressions(session_id)
    assert len(comps) == 1
    assert comps[0].before_tokens == 5000
    assert comps[0].tokens_saved == 3000
    assert comps[0].compression_ratio == pytest.approx(0.4, rel=0.01)
    assert comps[0].before_messages is None
    assert comps[0].after_messages is None


@pytest.mark.asyncio
async def test_record_compression_persists_messages(store, tmp_path):
    """record_compression should persist before/after message JSON and
    list_compressions should return them."""
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    session_id = (await store.create_session(ConversationSession(id="sess-cm", title="t"))).id
    import json as _json
    before_json = _json.dumps([{"role": "user", "content": "hello"}], ensure_ascii=False)
    after_json = _json.dumps([{"role": "user", "content": "[CONTEXT SUMMARY]: summary"}], ensure_ascii=False)
    await recorder.record_compression(
        session_id, before_tokens=100, after_tokens=20,
        before_messages=before_json, after_messages=after_json,
    )
    comps = await recorder.list_compressions(session_id)
    assert len(comps) == 1
    assert comps[0].before_messages == before_json
    assert comps[0].after_messages == after_json


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    r1 = SqliteUsageRecorder(db_path)
    await r1.init()
    r2 = SqliteUsageRecorder(db_path)
    await r2.init()  # should not error
    # also verify store migration
    s = SQLiteMemoryStore(tmp_path / "test.db")
    # initialize() called in __init__; re-init via second instance for idempotency check
    s2 = SQLiteMemoryStore(tmp_path / "test.db")


@pytest.mark.asyncio
async def test_get_overview_stats_aggregates_across_sessions(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    sid1 = (await store.create_session(ConversationSession(id="ov-1", title="a"))).id
    sid2 = (await store.create_session(ConversationSession(id="ov-2", title="b"))).id
    await recorder.record_call(
        sid1, "gpt-4o", "openai",
        CanonicalUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10),
        UsageCost(amount_usd="0.005", status="estimated", pricing_version="2026-07"),
        latency_ms=200,
    )
    await recorder.record_call(
        sid2, "gpt-4o", "openai",
        CanonicalUsage(input_tokens=300, output_tokens=150, cache_read_tokens=30),
        UsageCost(amount_usd="0.015", status="estimated", pricing_version="2026-07"),
        latency_ms=300,
    )

    stats = await recorder.get_overview_stats()
    assert stats.session_count == 2
    assert stats.input_tokens == 400
    assert stats.output_tokens == 200
    assert stats.cache_read_tokens == 40
    assert stats.api_call_count == 2
    assert float(stats.estimated_cost_usd) == pytest.approx(0.02, rel=0.001)
    # Tn = 400 + 40*0.2 + 200*5 = 400 + 8 + 1000 = 1408
    assert stats.normalized_tokens == 1408


@pytest.mark.asyncio
async def test_get_overview_stats_empty_db(tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "empty.db"))
    await recorder.init()
    # init() skips sessions table creation (owned by SQLiteMemoryStore);
    # without sessions table, get_overview_stats raises - which is the
    # expected contract: caller must ensure store is initialized first.
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        await recorder.get_overview_stats()


@pytest.mark.asyncio
async def test_list_sessions_paginated_returns_total_and_page(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    # create 5 sessions with distinct titles
    for i in range(5):
        sid = f"pg-{i}"
        await store.create_session(ConversationSession(id=sid, title=f"t{i}"))
        await recorder.record_call(
            sid, "gpt-4o", "openai",
            CanonicalUsage(input_tokens=(i + 1) * 10),
            UsageCost(amount_usd="0", status="unknown", pricing_version=None),
            latency_ms=10,
        )

    summaries, total = await recorder.list_sessions_paginated(page=1, page_size=2)
    assert total == 5
    assert len(summaries) == 2
    # Each session i has input_tokens=(i+1)*10, no output/cache
    # Tn for session 4 (i=4): (4+1)*10 * 1 = 50
    # Tn for session 3 (i=3): (3+1)*10 * 1 = 40
    # Sessions are ordered by updated_at DESC, id DESC, so first page returns
    # the most recently created sessions. Verify normalized_tokens is populated
    # and matches formula for each returned row.
    for s in summaries:
        assert s.normalized_tokens == s.input_tokens  # no cache/output


    summaries_p2, total_p2 = await recorder.list_sessions_paginated(page=2, page_size=2)
    assert total_p2 == 5
    assert len(summaries_p2) == 2

    summaries_p3, total_p3 = await recorder.list_sessions_paginated(page=3, page_size=2)
    assert total_p3 == 5
    assert len(summaries_p3) == 1  # last page only has 1


@pytest.mark.asyncio
async def test_list_sessions_paginated_clamps_invalid_input(store, tmp_path):
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    await store.create_session(ConversationSession(id="cx-1", title="x"))

    # page=0 -> treated as page 1
    summaries, total = await recorder.list_sessions_paginated(page=0, page_size=10)
    assert total == 1
    assert len(summaries) == 1

    # page_size=10000 -> clamped to 500
    summaries_big, _ = await recorder.list_sessions_paginated(page=1, page_size=10000)
    assert len(summaries_big) == 1

    # page beyond last -> empty list, total still correct
    summaries_far, total_far = await recorder.list_sessions_paginated(page=99, page_size=10)
    assert total_far == 1
    assert summaries_far == []


@pytest.mark.asyncio
async def test_list_sessions_paginated_turn_count_counts_user_messages(store, tmp_path):
    """turn_count should equal the number of non-summary user messages in
    each session. Assistant/tool/system messages and summary messages are
    excluded."""
    from app.domain.session import ConversationMessage

    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    sid = (await store.create_session(ConversationSession(id="tc-1", title="t"))).id
    # 3 user turns + 2 assistant + 1 tool result + 1 summary user message
    await store.append_message(sid, ConversationMessage(role="user", content="q1"))
    await store.append_message(sid, ConversationMessage(role="assistant", content="a1"))
    await store.append_message(sid, ConversationMessage(role="user", content="q2"))
    await store.append_message(sid, ConversationMessage(role="assistant", content="a2"))
    await store.append_message(sid, ConversationMessage(role="user", content="q3"))
    await store.append_message(sid, ConversationMessage(role="tool", tool_call_id="t1", content="tr"))
    from app.domain.context import CONTEXT_SUMMARY_PREFIX
    await store.append_summary_message(
        sid, ConversationMessage(role="user", content=CONTEXT_SUMMARY_PREFIX + "summary", is_summary=True),
    )

    summaries, _ = await recorder.list_sessions_paginated(page=1, page_size=10)
    match = [s for s in summaries if s.session_id == sid]
    assert len(match) == 1
    assert match[0].turn_count == 3


@pytest.mark.asyncio
async def test_list_sessions_paginated_turn_count_zero_for_empty_session(store, tmp_path):
    """Session with no messages should report turn_count=0."""
    recorder = SqliteUsageRecorder(str(tmp_path / "test.db"))
    await recorder.init()
    await store.create_session(ConversationSession(id="tc-empty", title="t"))

    summaries, _ = await recorder.list_sessions_paginated(page=1, page_size=10)
    match = [s for s in summaries if s.session_id == "tc-empty"]
    assert len(match) == 1
    assert match[0].turn_count == 0
