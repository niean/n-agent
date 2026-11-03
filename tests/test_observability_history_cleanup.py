"""Standalone maintenance contract; no application imports."""
import importlib.util
from pathlib import Path
import pytest

PATH = Path(__file__).parents[1] / "docker/observability/history-cleanup.py"

@pytest.fixture
def ops():
    assert PATH.exists(), "history cleanup implementation missing"
    spec = importlib.util.spec_from_file_location("history_cleanup", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class Backend:
    def __init__(self, kind="prometheus"):
        self.kind = kind
        self.calls = []
    def request(self, method, path, params=None):
        self.calls.append((method, path, params))
        if "buildinfo" in path or path == "/api/status/buildinfo":
            return {"data": {"version": "3.15.1"}, "version": "3.15.1"}
        if path.endswith("/labels"):
            return {"status":"success","data":["nagent_source","nagent_end_reason","nagent_kind","nagent_outcome"]}
        if path.endswith("/series"):
            return {"status": "success", "data": [{"__name__": "nagent_turns_total", "service_name": "n-agent"}]}
        return {"status": "success"}

def manifest(ops, backend=None):
    return ops.inventory("prometheus", backend or Backend(), 100, 200)

def test_inventory_is_read_only_and_hashed(ops):
    api = Backend()
    m = manifest(ops, api)
    assert m["service"] == "n-agent" and m["complete"]
    assert m["count"] == 1 and len(m["sha256"]) == 64
    assert all(call[0] == "GET" for call in api.calls)

@pytest.mark.parametrize("start,end", [(0,0), (200,100), (1,10**12)])
def test_rejects_invalid_or_future_bounds(ops,start,end):
    with pytest.raises(ValueError): ops.inventory("prometheus", Backend(),start,end)

@pytest.mark.parametrize("field,value", [("service","n-kb"),("cutoff",999),("count",9),("selectors",["{}"]),("backend","unknown")])
def test_manifest_tampering_cannot_mutate(ops,field,value):
    m = manifest(ops); m[field] = value
    api = Backend()
    with pytest.raises(ValueError): ops.apply(m,api,m["sha256"])
    assert not api.calls

def test_apply_requires_exact_digest_and_version(ops):
    m = manifest(ops)
    with pytest.raises(ValueError): ops.apply(m,Backend(),"wrong")
    api = Backend()
    result = ops.apply(m,api,m["sha256"])
    assert result["state"] == "tombstoned"
    deletes = [c for c in api.calls if c[0] == "POST"]
    assert deletes and all(c[2]["start"] == 100 and c[2]["end"] == "199.999" for c in deletes)
    assert all('service_name="n-agent"' in c[2]["match[]"] for c in deletes)

def test_unknown_backend_rejected(ops):
    with pytest.raises(ValueError): ops.inventory("profile",Backend(),100,200)

def test_tempo_truncated_search_blocks(ops):
    class API(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"2.9.0"}
            return {"traces":[],"metrics":{"completedJobs":1,"totalJobs":2}}
    m=ops.inventory("tempo",API(),100,200)
    assert not m["complete"] and m["state"] == "blocked"

def test_tempo_full_trace_requires_single_service_and_old_timestamps(ops):
    trace={"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-agent"}}]},"scopeSpans":[{"spans":[{"startTimeUnixNano":"110000000000","endTimeUnixNano":"120000000000"}]}]}]}
    assert ops.safe_trace(trace,100,200)
    trace["batches"][0]["resource"]["attributes"][0]["value"]["stringValue"]="n-kb"
    assert not ops.safe_trace(trace,100,200)

def test_network_failure_never_success(ops):
    class API(Backend):
        def request(self,method,path,params=None): raise OSError("offline")
    with pytest.raises(OSError): ops.inventory("prometheus",API(),100,200)


def test_rehashed_broad_selector_still_rejected(ops):
    m=manifest(ops); m["selectors"]=["{}"]
    m["sha256"]=ops.digest(m)
    with pytest.raises(ValueError): ops.apply(m,Backend(),m["sha256"])

def test_changed_backend_version_blocks_mutation(ops):
    class Changed(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"data":{"version":"new"}}
            return super().request(method,path,params)
    m=manifest(ops); api=Changed()
    with pytest.raises(ValueError): ops.apply(m,api,m["sha256"])
    assert not any(c[0]=="POST" for c in api.calls)

def test_partial_delete_failure_propagates(ops):
    class Disabled(Backend):
        def request(self,method,path,params=None):
            if method=="POST": raise OSError("admin API disabled")
            return super().request(method,path,params)
    m=manifest(ops)
    with pytest.raises(OSError,match="admin API disabled"): ops.apply(m,Disabled(),m["sha256"])

def test_tempo_splits_saturated_search_and_deduplicates(ops):
    class Tempo(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"2.9.0"}
            if path=="/api/search":
                traces=[{"traceID":"a"*32}] * (ops.LIMIT if params["end"]-params["start"]>50 else 1)
                return {"traces":traces,"metrics":{"totalJobs":2,"completedJobs":2}}
            return {"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-agent"}}]},"scopeSpans":[{"spans":[{"startTimeUnixNano":"110000000000","endTimeUnixNano":"120000000000"}]}]}]}
    api=Tempo(); m=ops.inventory("tempo",api,100,200)
    assert m["complete"] and m["count"]==1 and len(m["windows"])==2
    assert "--drop-trace" not in m["dry_run_argv"]
    assert ops.apply(m,api,m["sha256"])["state"]=="blocked"

@pytest.mark.parametrize("mutation", ["future","unknown","mixed"])
def test_tempo_unsafe_full_trace_blocks(ops,mutation):
    trace={"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-agent"}}]},"scopeSpans":[{"spans":[{"startTimeUnixNano":"110000000000","endTimeUnixNano":"120000000000"}]}]}]}
    if mutation=="future": trace["batches"][0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"]="210000000000"
    if mutation=="unknown": trace["batches"][0]["resource"]={}
    if mutation=="mixed": trace["batches"].append({"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-kb"}}]}})
    assert not ops.safe_trace(trace,100,200)

class Loki(Backend):
    def __init__(self, requests=None): super().__init__(); self.requests=requests or []
    def request(self,method,path,params=None):
        self.calls.append((method,path,params))
        if "buildinfo" in path: return {"version":"3.5.5"}
        if path.endswith("/query"): return {"status":"success","data":{"resultType":"vector","result":[{"value":[200,"5"]}]}}
        if method=="GET" and path.endswith("/delete"): return self.requests
        return {}

def test_loki_submission_never_claims_physical_completion(ops):
    api=Loki(); m=ops.inventory("loki",api,100,200)
    assert m["count"]==5 and m["count_unit"]=="log_entries"
    result=ops.apply(m,api,m["sha256"])
    assert result["state"]=="submitted" and "unknown" in result["physical_state"]
    post=[c for c in api.calls if c[0]=="POST"]
    assert len(post)==1 and post[0][2]=={"query":'{service_name="n-agent"}',"start":"1970-01-01T00:01:40.000Z","end":"1970-01-01T00:03:19.999Z"}

def test_loki_existing_submission_is_reported_without_resubmitting(ops):
    api=Loki([{"query":'{service_name="n-agent"}',"start_time":100,"end_time":199.999,"status":"received"}])
    m=ops.inventory("loki",api,100,200)
    assert ops.apply(m,api,m["sha256"])["state"]=="already_submitted"
    assert not any(c[0]=="POST" for c in api.calls)


def test_actual_tempo_version_v_prefix_is_supported(ops):
    class Tempo(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"v2.9.0"}
            return {"traces":[],"metrics":{"completedJobs":1,"totalJobs":1}}
    assert ops.inventory("tempo",Tempo(),100,200)["version"]=="2.9.0"

def test_tempo_incomplete_jobs_adaptively_split(ops):
    class Tempo(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"2.9.0"}
            complete=params["end"]-params["start"] <= 50
            return {"traces":[],"metrics":{"completedJobs":1,"totalJobs":1 if complete else 2}}
    m=ops.inventory("tempo",Tempo(),100,200)
    assert m["complete"] and len(m["windows"])==2

def test_api_accepts_loki_delete_list(ops,monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'[]'
    monkeypatch.setattr(ops.urllib.request,"urlopen",lambda *args,**kwargs:Response())
    assert ops.API("http://loki:3100").request("GET","/loki/api/v1/delete")==[]

def test_manifest_is_bound_to_backend_endpoint(ops):
    api=Backend(); api.url="http://a"
    m=manifest(ops,api)
    api.url="http://b"
    with pytest.raises(ValueError,match="endpoint"): ops.apply(m,api,m["sha256"])


def test_inventory_discloses_pending_overflow_scope(ops):
    m=manifest(ops)
    assert m["overflow_inventory"]["state"]=="not_found_in_scoped_window"
    assert m["residual_state"]=="none"

def test_loki_inventory_and_delete_exclude_cutoff_nanosecond(ops):
    api=Loki(); m=ops.inventory("loki",api,100,200)
    query=[c for c in api.calls if c[1].endswith("/query")][0]
    assert query[2]["time"]=="199999999999"
    ops.apply(m,api,m["sha256"])
    assert [c for c in api.calls if c[0]=="POST"][0][2]["end"]=="1970-01-01T00:03:19.999Z"


def test_unknown_overflow_translation_is_residual_blocked(ops):
    class Overflow(Backend):
        def request(self,method,path,params=None):
            if path.endswith("/labels"): return {"status":"success","data":["nagent_overflow_"]}
            return super().request(method,path,params)
    m=manifest(ops,Overflow())
    assert m["residual_state"]=="blocked"
    assert m["overflow_inventory"]["labels"]==["nagent_overflow_"]


def test_loki_real_v355_parse_time_contract(ops):
    from datetime import datetime
    class Compatible(Loki):
        def request(self,method,path,params=None):
            if method=="POST":
                # v3.5.5 parseTime accepts RFC3339 or exactly 10 Unix-second digits.
                for key in ("start","end"):
                    raw=params[key]
                    if len(raw)==10 and raw.isdigit(): continue
                    datetime.fromisoformat(raw.replace("Z","+00:00"))
            return super().request(method,path,params)
    api=Compatible(); m=ops.inventory("loki",api,100,200)
    assert ops.apply(m,api,m["sha256"])["state"]=="submitted"
    assert m["precision_gap"]["start_exclusive_ns"]==199999000000
    assert m["precision_gap"]["end_exclusive_ns"]==200000000000

def test_loki_fractional_seconds_duplicate_requires_exact_millisecond(ops):
    api=Loki([{"query":'{service_name="n-agent"}',"start_time":100,"end_time":199.998,"status":"received"}])
    m=ops.inventory("loki",api,100,200)
    assert ops.apply(m,api,m["sha256"])["state"]=="submitted"


def test_apply_rejects_new_overflow_labels_since_review(ops):
    class NewOverflow(Backend):
        def request(self,method,path,params=None):
            if path.endswith("/labels"):
                return {"status":"success","data":["nagent_source","nagent_overflow_"]}
            return super().request(method,path,params)
    m=manifest(ops); api=NewOverflow()
    with pytest.raises(ValueError,match="overflow inventory changed"):
        ops.apply(m,api,m["sha256"])
    assert not any(c[0]=="POST" for c in api.calls)


def test_tempo_v290_empty_metrics_object_is_complete_zero_job_window(ops):
    class Empty(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"v2.9.0"}
            return {"metrics":{}}
    m=ops.inventory("tempo",Empty(),100,200)
    assert m["complete"] and m["count"]==0
    assert len(m["windows"])==1 and m["windows"][0]["metrics"]=={}

@pytest.mark.parametrize("response", [{}, {"metrics":None}, {"metrics":[]}, {"metrics":{"completedJobs":True,"totalJobs":True}}, {"metrics":{"completedJobs":1,"totalJobs":0}}, {"metrics":{},"traces":[{"traceID":"a"*32}]}, {"metrics":{"totalJobs":1}}, {"metrics":{"completedJobs":"bad","totalJobs":"bad"}}])
def test_tempo_missing_or_invalid_job_metadata_is_blocked(ops,response):
    class Invalid(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"2.9.0"}
            return response
    m=ops.inventory("tempo",Invalid(),100,101)
    assert not m["complete"] and m["state"]=="blocked"


@pytest.mark.parametrize("trace_ids,expected", [(["9548b1585c40ebdfe9346f3df031ca1"],["09548b1585c40ebdfe9346f3df031ca1"]),(["a","0000000000000000000000000000000A"],["0000000000000000000000000000000a"])])
def test_tempo_trimmed_trace_ids_are_normalized_and_deduplicated(ops,trace_ids,expected):
    class Trimmed(Backend):
        def request(self,method,path,params=None):
            self.calls.append((method,path,params))
            if "buildinfo" in path: return {"version":"2.9.0"}
            if path=="/api/search":
                return {"metrics":{"completedJobs":1,"totalJobs":1},"traces":[{"traceID":v} for v in trace_ids]}
            return {"batches":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-agent"}}]},"scopeSpans":[{"spans":[{"startTimeUnixNano":"110000000000","endTimeUnixNano":"120000000000"}]}]}]}
    api=Trimmed(); m=ops.inventory("tempo",api,100,200)
    assert m["complete"] and m["records"]==expected
    paths=[c[1] for c in api.calls if c[1].startswith("/api/traces/")]
    assert paths==["/api/traces/"+v for v in expected]

@pytest.mark.parametrize("trace_id", ["","g","a"*33,"0","0"*32,None,12])
def test_tempo_invalid_or_zero_trace_ids_are_blocked(ops,trace_id):
    class Invalid(Backend):
        def request(self,method,path,params=None):
            if "buildinfo" in path: return {"version":"2.9.0"}
            return {"metrics":{"completedJobs":1,"totalJobs":1},"traces":[{"traceID":trace_id}]}
    m=ops.inventory("tempo",Invalid(),100,200)
    assert not m["complete"] and m["state"]=="blocked"


class ClassifiedTempo(Backend):
    def request(self,method,path,params=None):
        if "buildinfo" in path: return {"version":"2.9.0"}
        if path=="/api/search":
            return {"metrics":{"completedJobs":1,"totalJobs":1},"traces":[{"traceID":v*32} for v in "abcd"]}
        identity=path[-1]
        batches=[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-agent"}}]},"scopeSpans":[{"spans":[{"startTimeUnixNano":"110000000000","endTimeUnixNano":"120000000000"}]}]}]
        if identity=="b": batches.append({"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"n-kb"}}]}})
        if identity=="c": batches[0]["resource"]={}
        if identity=="d": batches[0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"]="210000000000"
        return {"batches":batches}

def test_tempo_complete_inventory_classifies_all_candidates_and_only_allows_safe(ops):
    m=ops.inventory("tempo",ClassifiedTempo(),100,200)
    assert m["enumeration_complete"] and m["inspection_complete"]
    assert m["candidate_count"]==4 and m["eligible_count"]==1 and m["blocked_count"]==3
    assert m["eligible_trace_ids"]==["a"*32]
    assert {v["reason"] for v in m["blocked_traces"]}=={"mixed_or_other_service","unknown_resource","outside_window"}
    assert not m["complete"] and m["state"]=="partially_eligible_with_blocked_residual"
    assert m["dry_run_argv"][4]=="a"*32

def test_tempo_inspection_budget_exhaustion_never_emits_allowlist(ops,monkeypatch):
    monkeypatch.setattr(ops,"MAX_REQUESTS",2)
    m=ops.inventory("tempo",ClassifiedTempo(),100,200)
    assert m["enumeration_complete"] and not m["inspection_complete"]
    assert m["candidate_count"]==4 and m["eligible_trace_ids"]==[] and m["dry_run_argv"]==[]

def test_tempo_inspection_network_failure_never_emits_allowlist(ops):
    class Failed(ClassifiedTempo):
        def request(self,method,path,params=None):
            if path.endswith("b"*32): raise OSError("offline")
            return super().request(method,path,params)
    m=ops.inventory("tempo",Failed(),100,200)
    assert m["enumeration_complete"] and not m["inspection_complete"]
    assert m["eligible_trace_ids"]==[] and m["dry_run_argv"]==[] and m["state"]=="blocked"
