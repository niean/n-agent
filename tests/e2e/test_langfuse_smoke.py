"""Contract tests for the Docker Langfuse runner (no live services)."""
import importlib.util
from pathlib import Path
import pytest

SOURCE = Path(__file__).with_name('langfuse_smoke.py')

def module():
    assert SOURCE.exists(), 'dedicated Docker smoke runner is missing'
    spec = importlib.util.spec_from_file_location('langfuse_smoke', SOURCE)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_report_preserves_attempts_and_cleanup_failure(tmp_path):
    m = module()
    report = m.Report(tmp_path, 'test', 'https://example.org/grafana/explore?orgId=3')
    report.record('business', 'FAIL', trace='a'*32, reason='provider unavailable')
    report.record('business', 'PASS', trace='b'*32)
    report.record('cleanup', 'FAIL', reason='restoration failed')
    data = report.save()
    assert data['exit_code'] == 1
    assert [x['attempt'] for x in data['cases']] == [1, 2, 1]
    assert 'orgId=3' in data['cases'][1]['trace_url']
    assert '## 重试记录' in (tmp_path/'result.md').read_text()


def test_otlp_payload_keeps_topology_and_all_sentinel_surfaces():
    m = module()
    trace, payload, expected = m.synthetic_payload()
    spans = payload['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert len(spans) >= 8
    assert {s['traceId'] for s in spans} == {trace}
    assert spans[1]['parentSpanId'] == spans[0]['spanId']
    assert spans[2]['parentSpanId'] == spans[0]['spanId']
    assert expected['long_input'].encode().__len__() == 1024000
    assert expected['long_output'].encode().__len__() == 1024000
    assert len(expected['sentinels']) >= 9


def test_pagination_reads_all_observations():
    m = module()
    calls=[]
    def get(url):
        calls.append(url)
        return {'data':[{'id':str(len(calls))}], 'meta':{'cursor':'next' if len(calls)==1 else None}}
    assert len(m.observations(get, 'a'*32)) == 2
    assert 'cursor=next' in calls[1]


def test_sanitized_errors_never_render_authentication_values():
    m=module()
    assert m.safe_error(RuntimeError('Basic secret-value')) == 'RuntimeError'


def test_shell_restores_only_owned_services_and_isolates_environment():
    source=SOURCE.with_suffix('.sh')
    assert source.exists(), 'Docker orchestration entry point is missing'
    text=source.read_text()
    assert 'e2e_environment default' in text
    assert 'trap restore EXIT' in text
    assert '--no-deps' in text
    assert 'down' not in text


def test_queue_metrics_select_only_langfuse_branch():
    m=module()
    text='''otelcol_exporter_enqueue_failed_spans_total{exporter="otlphttp/langfuse"} 4
    otelcol_exporter_enqueue_failed_spans_total{exporter="otlp/tempo"} 100
    otelcol_exporter_queue_size{exporter="otlphttp/langfuse"} 16700000'''
    assert m.queue_metrics(text)=={'failed':4.0,'size':16700000.0}


def test_check_returns_failure_and_does_not_publish_unconfirmed_trace(tmp_path):
    m=module()
    runner=object.__new__(m.Runner)
    runner.report=m.Report(tmp_path,'unit','https://example.org/explore')
    def fail():
        runner.trace='a'*32
        raise RuntimeError('Authorization: secret')
    assert runner.check('business',fail) is False
    row=runner.report.data['cases'][0]
    assert row['trace_id'] is None
    assert row['evidence']['requested_trace_id']=='a'*32


def test_metadata_expansion_is_explicit():
    m=module(); calls=[]
    m.observations(lambda url:calls.append(url) or {'data':[]},'a'*32)
    from urllib.parse import parse_qs,urlsplit
    assert parse_qs(urlsplit(calls[0]).query)['expandMetadata']==['input,output,links,system_prompt,tool_definitions,error,judge_reason']


def test_multiple_trace_evidence_shares_one_attempt(tmp_path):
    m=module(); report=m.Report(tmp_path,'test','https://example.org/explore')
    report.record('concurrent','PASS',trace=['a'*32,'b'*32])
    rows=report.save()['cases']
    assert [r['trace_id'] for r in rows]==['a'*32,'b'*32]
    assert [r['attempt'] for r in rows]==[1,1]


def test_planned_cases_remain_skipped_without_fake_retry(tmp_path):
    m=module(); report=m.Report(tmp_path,'test','https://example.org/explore')
    report.plan(['business','fault-auth','cleanup'])
    report.record('business','PASS',trace='a'*32)
    rows=report.save()['cases']
    assert len(rows)==3
    assert next(r for r in rows if r['id']=='fault-auth')['result']=='SKIP'
    assert next(r for r in rows if r['id']=='business')['attempt']==1


@pytest.mark.parametrize('restore_fails', [False, True])
@pytest.mark.parametrize('remove_fails', [False, True])
def test_shell_finally_restores_collector_and_reports_cleanup(tmp_path,restore_fails,remove_fails):
    import os, shutil, subprocess
    target=tmp_path/'tests/e2e'; target.mkdir(parents=True)
    shutil.copy(SOURCE.with_suffix('.sh'),target/'langfuse_smoke.sh')
    shutil.copy(SOURCE.with_name('environment.sh'),target/'environment.sh')
    binpath=tmp_path/'bin'; binpath.mkdir()
    log=tmp_path/'calls'
    docker=binpath/'docker'
    docker.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = inspect ]; then echo test-image; fi
if [[ "$*" == "rm -f e2e-unit" ]] && [ "$REMOVE_FAILS" = 1 ]; then exit 1; fi
# Trigger an early controlled stop after the first Collector mutation.
if [[ "$*" == *" fault-auth" ]]; then exit 0; fi
exit 0
''')
    helper=binpath/'python3'
    helper.write_text('''#!/bin/bash
printf 'collector %s\\n' "$*" >> "$CALL_LOG"
if [[ "$*" == *" auth "* ]]; then exit 1; fi
if [[ "$*" == *" restore "* ]] && [ "$RESTORE_FAILS" = 1 ]; then exit 1; fi
exit 0
''')
    shell=binpath/'sh'
    shell.write_text('''#!/bin/bash
printf 'lifecycle %s\\n' "$*" >> "$CALL_LOG"
exit 0
''')
    for path in (docker,helper,shell): path.chmod(0o755)
    env={**os.environ,'PATH':str(binpath)+':'+os.environ['PATH'],'CALL_LOG':str(log),
      'N_AGENT_E2E_ENVIRONMENT':'default','LANGFUSE_SMOKE_RUN_ID':'unit','RESTORE_FAILS':str(int(restore_fails)),
      'REMOVE_FAILS':str(int(remove_fails))}
    result=subprocess.run(['bash',str(target/'langfuse_smoke.sh')],env=env,capture_output=True,text=True)
    assert result.returncode==1  # setup failed; successful cleanup cannot hide it
    calls=log.read_text()
    assert ' restore ' in calls
    assert f' cleanup {"FAIL" if restore_fails or remove_fails else "PASS"}' in calls
    assert 'rm -f e2e-unit' in calls


def test_history_trace_compaction_order_does_not_change_sample_identity():
    m=module()
    one={'batches':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':[{'spanId':'b','attributes':[{'key':'z'},{'key':'a'}]},{'spanId':'a','attributes':[]}]}]}]}
    two={'batches':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':[{'spanId':'a','attributes':[]},{'spanId':'b','attributes':[{'key':'a'},{'key':'z'}]}]}]}]}
    assert m.history_identity('trace',one)==m.history_identity('trace',two)
    two['batches'][0]['scopeSpans'][0]['spans'][0]['spanId']='c'
    assert m.history_identity('trace',one)!=m.history_identity('trace',two)


def test_history_metric_series_order_is_irrelevant_but_values_are_not():
    m=module(); a={'data':{'result':[{'metric':{'job':'a'},'value':[123,'1']},{'metric':{'job':'b'},'value':[123,'1']}]}}
    b={'data':{'result':list(reversed(a['data']['result']))}}
    assert m.history_identity('metric',a)==m.history_identity('metric',b)
    b['data']['result'][0]={'metric':{'job':'b'},'value':[123,'0']}
    assert m.history_identity('metric',a)!=m.history_identity('metric',b)


def test_otlp_includes_non_allowlisted_service_for_real_filter_proof():
    m=module(); trace,payload,_=m.synthetic_payload()
    excluded=payload['resourceSpans'][1]
    assert excluded['resource']['attributes'][0]['value']['stringValue']=='e2e-excluded'
    assert excluded['scopeSpans'][0]['spans'][0]['traceId']==trace


def test_fault_counter_separates_original_exporters():
    m=module()
    values=m.failure_metrics('''otelcol_exporter_send_failed_spans_total{exporter="otlphttp/langfuse"} 3
    otelcol_exporter_send_failed_spans_total{exporter="otlp/tempo"} 1
    otelcol_exporter_send_failed_log_records_total{exporter="otlphttp/loki"} 2''')
    assert values=={'langfuse':3.0,'original':3.0}


def test_business_usage_comparison_preserves_missing_and_zero_buckets():
    m=module()
    assert m.expected_usage({'gen_ai.usage.input_tokens':3,'gen_ai.usage.output_tokens':0,'nagent.model.cache_read_tokens':2})=={'input':3,'output':0,'cache_read':2,'total':5}
    assert m.expected_usage({})=={}


def test_environment_restore_publishes_optional_safe_status():
    text=SOURCE.with_name('environment.sh').read_text()
    assert 'N_AGENT_E2E_RESTORE_STATUS_FILE' in text
    assert 'restore_status=FAIL' in text


def test_authenticated_requests_never_follow_redirects_or_environment_proxy(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from urllib.error import HTTPError,URLError
    m=module(); hits=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(302 if self.path=='/redirect' else 200)
            if self.path=='/redirect': self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/target')
            self.end_headers(); self.wfile.write(b'{}')
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        port=server.server_port
        with pytest.raises(HTTPError) as error:
            m.request(f'http://127.0.0.1:{port}/redirect',headers={'Authorization':'Basic unit-secret'})
        assert error.value.code==302 and hits==['/redirect']
        hits.clear()
        monkeypatch.setenv('http_proxy',f'http://127.0.0.1:{port}')
        monkeypatch.setenv('HTTP_PROXY',f'http://127.0.0.1:{port}')
        monkeypatch.setenv('no_proxy',''); monkeypatch.setenv('NO_PROXY','')
        with pytest.raises(URLError):
            m.request(f'http://127.0.0.2:{port}/target',headers={'Authorization':'Basic unit-secret'},timeout=1)
        assert hits==[]
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_dual_backend_comparison_checks_tree_io_time_and_usage():
    m=module()
    span={'spanId':'0000000000000001','startTimeUnixNano':'1000000000','endTimeUnixNano':'2000000000',
          'attributes':[{'key':'gen_ai.operation.name','value':{'stringValue':'chat'}},
                        {'key':'nagent.content.input_messages','value':{'stringValue':'input'}},
                        {'key':'nagent.content.output_messages','value':{'stringValue':'output'}}]}
    tempo={'batches':[{'scopeSpans':[{'spans':[span]}]}]}
    row={'id':'0000000000000001','parentObservationId':None,'input':'input','output':'output',
         'startTime':'1970-01-01T00:00:01.000Z','endTime':'1970-01-01T00:00:02.000Z','type':'GENERATION','model':None,'usageDetails':{}}
    m.compare_backends(tempo,[row])
    for field,value in [('parentObservationId','b'*16),('input','changed'),('startTime','1970-01-01T00:00:03.000Z')]:
        with pytest.raises(AssertionError): m.compare_backends(tempo,[{**row,field:value}])


def test_owned_session_manifest_is_exact_and_source_guarded(tmp_path):
    m=module(); manifest=m.SessionManifest(tmp_path,'run')
    session='e2e-langfuse-'+'a'*32
    manifest.add(session,'api')
    assert manifest.entries()==[{'run_id':'run','session_id':session,'source':'api'}]
    assert manifest.owns(session,'api')
    assert not manifest.owns(session,'dashboard')
    assert not manifest.owns('e2e-langfuse-'+'b'*32,'api')
    with pytest.raises(ValueError): manifest.add('user-session','api')


def test_concurrent_double_failure_keeps_one_failed_case(tmp_path,monkeypatch):
    m=module(); report=m.Report(tmp_path,'unit','https://example.org/explore')
    report.plan(['concurrent'])
    parent=object.__new__(m.Runner); parent.report=report
    def business(self): raise RuntimeError('provider unavailable')
    monkeypatch.setattr(m.Runner,'__init__',lambda self,*args,**kwargs:None)
    monkeypatch.setattr(m.Runner,'business',business)
    assert parent.check('concurrent',parent.concurrent) is False
    rows=report.save()['cases']
    assert len(rows)==1 and rows[0]['result']=='FAIL' and rows[0]['trace_id'] is None


def test_semantic_payload_exercises_new_types_and_atomic_source_replacement():
    m=module(); _,payload,expected=m.synthetic_payload()
    spans=payload['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert expected['observation_count']==len(spans)
    values=[{a['key']:a['value'] for a in span['attributes']} for span in spans]
    assert any('nagent.knowledge.operation' in v for v in values)
    judges=[s for s,v in zip(spans,values) if v.get('nagent.task.operation')=={'stringValue':'evaluate'}]
    assert len(judges)==2 and judges[0]['parentSpanId']==judges[1]['parentSpanId']
    tool=values[2]
    for direction,low,high in [('input','input_messages','tool_arguments'),('output','output_messages','tool_result')]:
        assert 'nagent.content.'+low+'_observed_bytes' in tool
        assert not any('nagent.content.'+high+'_'+suffix in tool for suffix in ('original_bytes','observed_bytes','incomplete','truncated'))


def test_observations_requests_explicit_semantic_field_groups():
    m=module(); calls=[]
    m.observations(lambda url:calls.append(url) or {'data':[]},'a'*32)
    from urllib.parse import parse_qs,urlsplit
    fields=parse_qs(urlsplit(calls[0]).query)['fields'][0].split(',')
    # Official v2 groups: costs live in usage; release lives in trace_context.
    assert {'usage','model','time','trace_context'} <= set(fields)


def test_business_comparison_checks_actual_parameters_first_output_and_cost():
    m=module()
    values={'gen_ai.operation.name':'chat','nagent.model.parameter.temperature':0.5,
            'nagent.model.completion_start_time':'1970-01-01T00:00:01.500Z',
            'nagent.model.cost_usd':0.125,'nagent.model.cost_status':'estimated','nagent.model.pricing_version':'v1'}
    span={'spanId':'0000000000000001','startTimeUnixNano':'1000000000','endTimeUnixNano':'2000000000',
          'attributes':[m.attr(k,v) for k,v in values.items()]}
    row={'id':span['spanId'],'parentObservationId':None,'type':'GENERATION','model':None,'usageDetails':{},
         'startTime':'1970-01-01T00:00:01Z','endTime':'1970-01-01T00:00:02Z','modelParameters':{'temperature':0.5},
         'completionStartTime':'1970-01-01T00:00:01.500Z','costDetails':{'total':0.125},
         'metadata':{'nagent.model.cost_status':'estimated','nagent.model.pricing_version':'v1'}}
    tempo={'batches':[{'scopeSpans':[{'spans':[span]}]}]}
    m.compare_backends(tempo,[row])
    for field,value in [('modelParameters',{}),('completionStartTime',None),('costDetails',{})]:
        with pytest.raises(AssertionError): m.compare_backends(tempo,[{**row,field:value}])


def test_persistence_uses_recorded_synthetic_count(tmp_path):
    m=module(); runner=object.__new__(m.Runner)
    runner.report=m.Report(tmp_path,'unit','https://example.org/explore')
    runner.report.record('synthetic','PASS',trace='a'*32,evidence={'observations':18})
    runner.get=lambda url:{'data':[{} for _ in range(18)],'meta':{}}
    assert runner.persisted()['observations']==18


def test_business_probes_explicit_final_model_parameter(monkeypatch):
    import json
    from types import SimpleNamespace
    m=module(); runner=object.__new__(m.Runner)
    runner.app='http://app'; runner.manifest=SimpleNamespace(add=lambda *args:None)
    runner.metric_count=lambda:0
    def capture(request,**kwargs):
        assert json.loads(request.data)['temperature']==0.25
        raise RuntimeError('captured')
    monkeypatch.setattr(m,'urlopen',capture)
    with pytest.raises(RuntimeError,match='captured'): runner.business()
