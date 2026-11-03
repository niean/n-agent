#!/usr/bin/env python3
"""N-Agent old observation inventory and bounded deletion via supported APIs.

No app imports, volume access, docker invocation, or automatic Tempo mutation.
Run in an operator-controlled container on the observability network.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = "n-agent"
VERSIONS = {"tempo": "2.9.0", "loki": "3.5.5", "prometheus": "3.15.1"}
# Only affected old project counters or standard series carrying old dimensions.
PROM_SELECTORS = [
    '{service_name="n-agent",__name__=~"nagent_turns_total|nagent_context_compressions_total|nagent_budget_denied_total"}',
    '{service_name="n-agent",__name__=~"gen_ai_invoke_agent_(duration_seconds|inference_calls|tool_calls)_(bucket|sum|count)",nagent_source!=""}',
]
LOKI_SELECTOR = '{service_name="n-agent"}'
TEMPO_SELECTOR = '{resource.service.name="n-agent"}'
LIMIT = 1000
MAX_REQUESTS = 2048
INITIAL_WINDOW = 3600

class API:
    def __init__(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("HTTP backend URL required")
        self.url = url.rstrip("/")

    def request(self, method, path, params=None):
        query = urllib.parse.urlencode(params or {}, doseq=True)
        request = urllib.request.Request(self.url + path + ("?" + query if query else ""), method=method)
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        result = json.loads(body) if body else {}
        if isinstance(result, list):
            return result
        if not isinstance(result, dict):
            raise ValueError("unexpected backend response")
        if result.get("status") == "error":
            raise ValueError("backend reported error: " + str(result.get("error")))
        if result.get("warnings"):
            raise ValueError("partial backend response: " + str(result["warnings"]))
        return result


def bounds(start, cutoff):
    if not isinstance(start, int) or not isinstance(cutoff, int) or not 0 < start < cutoff <= int(time.time()):
        raise ValueError("explicit positive start < cutoff <= now required (Unix seconds)")


def digest(manifest):
    value = {k:v for k,v in manifest.items() if k != "sha256"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def version(backend, api):
    path = {"prometheus":"/api/v1/status/buildinfo", "loki":"/loki/api/v1/status/buildinfo", "tempo":"/api/status/buildinfo"}[backend]
    result = api.request("GET", path)
    value = result.get("data", result).get("version")
    if isinstance(value, str) and value.startswith("v"):
        value = value[1:]
    if value != VERSIONS[backend]:
        raise ValueError("unsupported or changed backend version: " + str(value))
    return value


def selectors(backend):
    return PROM_SELECTORS if backend == "prometheus" else [LOKI_SELECTOR if backend == "loki" else TEMPO_SELECTOR]


def loki_delete_bounds(start, cutoff):
    # v3.5.5 parseTime: RFC3339 -> UnixMilli. Integer nanoseconds are rejected.
    begin = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    end = datetime.fromtimestamp(cutoff - 1, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".999Z"
    return {"query":LOKI_SELECTOR, "start":begin, "end":end}


def loki_request_milliseconds(value):
    # prometheus/common v0.62.0 model.Time.MarshalJSON emits decimal seconds.
    try:
        value = Decimal(str(value)) * 1000
        return int(value) if value.is_finite() and value == value.to_integral_value() else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def trace_block_reason(trace, start, cutoff):
    batches = trace.get("batches", trace.get("resourceSpans", []))
    if not batches:
        return "unknown_resource"
    spans_seen = 0
    for batch in batches:
        identities = [a.get("value", {}).get("stringValue") for a in batch.get("resource", {}).get("attributes", []) if a.get("key") == "service.name"]
        if not identities:
            return "unknown_resource"
        if identities != [SERVICE]:
            return "mixed_or_other_service"
        scopes = batch.get("scopeSpans", batch.get("instrumentationLibrarySpans", []))
        for scope in scopes:
            for span in scope.get("spans", []):
                spans_seen += 1
                try:
                    begin, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
                except (KeyError, TypeError, ValueError):
                    return "unknown_span_time"
                if not start * 10**9 <= begin <= end < cutoff * 10**9:
                    return "outside_window"
    return None if spans_seen else "unknown_span_time"


def safe_trace(trace, start, cutoff):
    return trace_block_reason(trace, start, cutoff) is None


def tempo_inventory(api, start, cutoff):
    result = {"eligible_trace_ids":[], "blocked_traces":[], "candidate_count":0,
              "eligible_count":0, "blocked_count":0, "inspected_count":0,
              "enumeration_complete":False, "inspection_complete":False}
    ids = set()
    def finish(reason=None):
        result["reason"] = reason
        result["candidate_count"] = len(ids)
        result["uninspected_count"] = len(ids) - result["inspected_count"]
        return result, windows
    windows = []
    pending = [(begin, min(begin + INITIAL_WINDOW, cutoff)) for begin in range(start, cutoff, INITIAL_WINDOW)]
    if len(pending) > MAX_REQUESTS:
        return finish("inventory request budget exceeded; use smaller reviewed intervals")
    requests = 0
    started = time.monotonic()
    windows = []
    while pending:
        begin, end = pending.pop()
        requests += 1
        if requests > MAX_REQUESTS or time.monotonic() - started > 300:
            return finish("inventory request/time budget exceeded")
        response = api.request("GET", "/api/search", {"q":TEMPO_SELECTOR,"start":begin,"end":end,"limit":LIMIT})
        metrics = response.get("metrics")
        traces = response.get("traces", [])
        if not isinstance(metrics, dict) or not isinstance(traces, list):
            return finish("missing or invalid search metrics/traces")
        # Tempo 2.9.0 SearchMetrics uses proto3 uint32 job counters; its
        # JSON marshaler omits default zero fields. An explicitly present
        # empty metrics message with no traces is the valid zero-job case.
        completed, total = metrics.get("completedJobs"), metrics.get("totalJobs")
        valid_jobs = all(type(value) is int and 0 <= value <= 2**32 - 1 for value in (completed, total))
        complete = (metrics == {} and not traces) or (valid_jobs and completed == total and (total > 0 or not traces))
        if not complete or len(traces) >= LIMIT:
            if end - begin <= 1:
                return finish("search saturated/incomplete at minimum one-second window")
            middle = (begin + end) // 2
            pending.extend([(begin,middle),(middle,end)])
            continue
        windows.append({"start":begin,"end":end,"count":len(traces),"metrics":metrics})
        for trace in traces:
            trace_id = trace.get("traceID", "")
            if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-fA-F]{1,32}", trace_id) or int(trace_id, 16) == 0:
                return finish("unknown TraceID")
            # Tempo 2.9.0 trims leading zeros in search IDs. Restore the
            # same 128-bit identity, then deduplicate before full retrieval.
            ids.add(trace_id.lower().zfill(32))
    result["enumeration_complete"] = True
    result["candidate_count"] = len(ids)
    eligible = []
    for trace_id in sorted(ids):
        requests += 1
        if requests > MAX_REQUESTS or time.monotonic() - started > 300:
            return finish("trace inspection request/time budget exceeded")
        try:
            trace = api.request("GET", "/api/traces/" + trace_id)
            reason = trace_block_reason(trace, start, cutoff)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            return finish("trace inspection failed: " + trace_id + ": " + str(error))
        result["inspected_count"] += 1
        if reason:
            result["blocked_traces"].append({"trace_id":trace_id, "reason":reason})
            result["blocked_count"] += 1
        else:
            eligible.append(trace_id)
            result["eligible_count"] += 1
    result["inspection_complete"] = True
    result["eligible_trace_ids"] = eligible
    return finish()


def inventory(backend, api, start, cutoff):
    if backend not in VERSIONS:
        raise ValueError("unknown backend")
    bounds(start, cutoff)
    manifest = {"schema":1,"backend":backend,"service":SERVICE,"start":start,"cutoff":cutoff,
                "endpoint":getattr(api,"url",None),"version":version(backend,api),"selectors":selectors(backend),"complete":True,
                "state":"inventoried","count":0,"records":[],"count_unit":"series", "interval":"[start, cutoff)"}
    if backend == "prometheus":
        label_result = api.request("GET", "/api/v1/labels", {"match[]":'{service_name="n-agent"}', "start":start, "end":f"{cutoff - 1}.999"})
        if label_result.get("status") != "success" or not isinstance(label_result.get("data"),list):
            raise ValueError("incomplete metric label inventory")
        old_overflow = [name for name in label_result["data"] if "overflow" in name and name != "nagent_metric_overflow"]
        manifest["overflow_inventory"] = {"labels":old_overflow, "state":"unconfirmed_translation" if old_overflow else "not_found_in_scoped_window"}
        manifest["residual_state"] = "blocked" if old_overflow else "none"
        manifest["residual_reason"] = "old overflow label translation not confirmed; no speculative deletion selector" if old_overflow else None
        records = {}
        for selector in PROM_SELECTORS:
            response = api.request("GET", "/api/v1/series", {"match[]":selector,"start":start,"end":f"{cutoff - 1}.999"})
            if response.get("status") != "success" or not isinstance(response.get("data"),list):
                raise ValueError("incomplete series response")
            for record in response["data"]:
                if record.get("service_name") != SERVICE:
                    raise ValueError("cross service series")
                records[json.dumps(record, sort_keys=True)] = record
        manifest["records"] = [records[k] for k in sorted(records)]
        manifest["count"] = len(records)
    elif backend == "loki":
        # Exact scalar count avoids mistaking a limited log sample for an inventory.
        response = api.request("GET", "/loki/api/v1/query", {"query":f'sum(count_over_time({LOKI_SELECTOR}[{cutoff-start}s]))',"time":str(cutoff * 10**9 - 1)})
        data = response.get("data", {})
        if response.get("status") != "success" or data.get("resultType") != "vector":
            raise ValueError("incomplete Loki count")
        values = data.get("result", [])
        if len(values) > 1:
            raise ValueError("unexpected Loki count")
        manifest["count"] = int(float(values[0]["value"][1])) if values else 0
        manifest["count_unit"] = "log_entries"
        manifest["delete_parameters"] = loki_delete_bounds(start, cutoff)
        manifest["precision_gap"] = {"start_exclusive_ns":cutoff * 10**9 - 10**6,
                                     "end_exclusive_ns":cutoff * 10**9,
                                     "state":"unverified; last sub-millisecond interval excluded from deletion"}
        manifest["delete_requests"] = api.request("GET", "/loki/api/v1/delete")
        manifest["physical_state"] = "unknown; API submission/filtering does not prove physical deletion"
    else:
        classification, windows = tempo_inventory(api,start,cutoff)
        manifest.update(classification)
        ids = classification["eligible_trace_ids"]
        manifest.update(records=ids, count=classification["candidate_count"], count_unit="traces", windows=windows)
        inspected = classification["enumeration_complete"] and classification["inspection_complete"]
        manifest["complete"] = inspected and classification["blocked_count"] == 0
        manifest["state"] = ("blocked" if not inspected else
                             "partially_eligible_with_blocked_residual" if classification["blocked_count"] else
                             "external_offline_rewrite_required")
        manifest["offline_requirements"] = ["stop ingestion; flush all ingesters", "stop Tempo and compactor", "snapshot for rollback", "revalidate TraceID allowlist on frozen store", "use tempo-cli 2.9.0; inspect dry-run before --drop-trace"]
        manifest["dry_run_argv"] = ["tempo-cli","rewrite-blocks","drop-traces","single-tenant",",".join(ids),"--backend=local","--bucket=/var/tempo/blocks"] if ids else []
    manifest["sha256"] = digest(manifest)
    return manifest


def validate(manifest, confirmation):
    if manifest.get("sha256") != digest(manifest) or confirmation != manifest.get("sha256"):
        raise ValueError("manifest hash mismatch; review inventory again")
    backend = manifest.get("backend")
    if backend not in VERSIONS or manifest.get("schema") != 1 or manifest.get("service") != SERVICE:
        raise ValueError("unsupported manifest scope")
    bounds(manifest.get("start"),manifest.get("cutoff"))
    if manifest.get("selectors") != selectors(backend) or manifest.get("version") != VERSIONS[backend]:
        raise ValueError("selector/version mismatch")
    if manifest.get("complete") is not True:
        raise ValueError("incomplete inventory is blocked")


def apply(manifest, api, confirmation):
    validate(manifest,confirmation)
    backend = manifest["backend"]
    if manifest.get("endpoint") != getattr(api,"url",None):
        raise ValueError("backend endpoint changed")
    version(backend,api)
    if backend == "tempo":
        # Offline operator validates frozen data. Never grant docker socket access.
        return {"state":"blocked", "reason":"external offline rewrite required", "dry_run_argv":manifest["dry_run_argv"]}
    current = inventory(backend,api,manifest["start"],manifest["cutoff"])
    if backend == "prometheus":
        if any(current[key] != manifest[key] for key in ("overflow_inventory", "residual_state", "residual_reason")):
            raise ValueError("overflow inventory changed; generate and review new manifest")
        if current["records"] != manifest["records"]:
            raise ValueError("series inventory changed; generate and review new manifest")
        for selector in PROM_SELECTORS:
            api.request("POST", "/api/v1/admin/tsdb/delete_series", {"match[]":selector,"start":manifest["start"],"end":f'{manifest["cutoff"] - 1}.999'})
        return {"state":"tombstoned","residual_state":manifest["residual_state"],"residual_reason":manifest["residual_reason"],"physical_state":"not verified; no global clean_tombstones invoked"}
    # Loki requests may already exist; retry reports them without resubmission.
    requests = current["delete_requests"]
    if isinstance(requests,list):
        matches = [r for r in requests if r.get("query") == LOKI_SELECTOR and loki_request_milliseconds(r.get("start_time")) == manifest["start"] * 1000 and loki_request_milliseconds(r.get("end_time")) == manifest["cutoff"] * 1000 - 1]
        if matches:
            return {"state":"already_submitted","requests":matches,"precision_gap":manifest["precision_gap"],"physical_state":"unknown; inspect deletion mode and compactor status"}
    if current["count"] != manifest["count"]:
        raise ValueError("log count changed; generate and review new manifest")
    api.request("POST", "/loki/api/v1/delete", loki_delete_bounds(manifest["start"],manifest["cutoff"]))
    return {"state":"submitted","precision_gap":manifest["precision_gap"],"physical_state":"pending/unknown; filter-only is not physical deletion; cancellation window unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=VERSIONS)
    parser.add_argument("--url", required=True)
    parser.add_argument("--start", type=int)
    parser.add_argument("--cutoff", type=int)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha256")
    args = parser.parse_args()
    api = API(args.url)
    try:
        if args.apply:
            manifest = json.loads(args.manifest.read_text())
            if manifest.get("backend") != args.backend:
                raise ValueError("backend does not match manifest")
            result = apply(manifest,api,args.confirm_sha256)
        else:
            result = inventory(args.backend,api,args.start,args.cutoff)
            with args.manifest.open("x") as output:
                json.dump(result,output,ensure_ascii=False,indent=2)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result.get("state") in ("blocked", "partially_eligible_with_blocked_residual") else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"state":"blocked","reason":str(error)}))
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
