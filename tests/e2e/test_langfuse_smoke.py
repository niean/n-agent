"""Contract tests for the Docker Langfuse runner (no live services)."""
import importlib.util
import json
import re
from pathlib import Path
import sys
import time
import pytest
import yaml

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
    assert values=={'langfuse':3.0,'kb':0.0,'original':3.0}
    # The -kb exporter is its own branch; its label must never fold into the n-agent one.
    separated=m.failure_metrics('otelcol_exporter_send_failed_spans_total{exporter="otlphttp/langfuse-kb"} 5')
    assert separated=={'langfuse':0.0,'kb':5.0,'original':0.0}


def test_queue_counters_separate_both_langfuse_branches():
    m=module()
    text='''otelcol_exporter_enqueue_failed_spans_total{exporter="otlphttp/langfuse-kb"} 7
    otelcol_exporter_enqueue_failed_spans_total{exporter="otlphttp/langfuse"} 1
    otelcol_exporter_queue_size{exporter="otlphttp/langfuse-kb"} 16000000'''
    assert m.kb_queue_metrics(text)=={'failed':7.0,'size':16000000.0}
    assert m.queue_metrics(text)=={'failed':1.0,'size':0.0}


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


def kb_expected_rows(expected):
    """Fabricate the observation rows the KB project must return for the synthetic payload."""
    def envelope(source,complete=True):
        return {'source':source,'original_bytes':64,'observed_bytes':32,'truncated':True,'incomplete':True} if complete else {'source':source}
    def row(identifier,**fields):
        base={'id':f'{identifier:016x}','parentObservationId':f'{1:016x}' if identifier!=1 else None,
              'sessionId':None,'level':'INFO','statusMessage':None,'metadata':{}}
        base.update(fields); return base
    index_meta={'nkb.index.operation':'run','nkb.index.outcome':'success','nkb.index.duration_ms':12.5,
                'nkb.index.document_id':'doc-e2e','nkb.index.job_id':'job-e2e','nkb.index.chunk_count':7}
    rows=[
      row(1,type='CHAIN',name='index.run',traceName='index.run',input=expected['index_input'],output=expected['index_result'],
          metadata={**index_meta,'input':envelope('index_input'),'output':envelope('index_result')}),
      row(2,type='RETRIEVER',name='retrieval.search',input=expected['retrieval_input'],output=expected['retrieval_output'],
          metadata={'nkb.retrieval.operation':'search','nkb.retrieval.outcome':'success','nkb.retrieval.duration_ms':3.25,
                    'nkb.retrieval.result_count':2,'input':envelope('retrieval_input'),'output':envelope('retrieval_output')}),
      row(3,type='EMBEDDING',name='embeddings',input=expected['embedding_input'],model=expected['model'],
          usageDetails={'input':5,'total':5},metadata={'gen_ai.provider.name':'ollama','input':envelope('embedding_input',complete=False)}),
      row(4,type='EMBEDDING',name='embeddings',model=expected['model'],usageDetails={'input':0,'total':0},
          metadata={'nkb.model.usage_known':True}),
      row(5,type='EMBEDDING',name='embeddings',model=expected['model'],metadata={'nkb.model.usage_known':False}),
      row(6,type='SPAN',name='span'),row(7,type='SPAN',name='span'),row(8,type='SPAN',name='span'),
      row(9,type='CHAIN',name='index.run',input=expected['nested_input'],output=expected['nested_result'],
          metadata={'nkb.index.operation':'run','nkb.index.outcome':'success'}),
      row(10,type='SPAN',name='span'),
      row(11,type='RETRIEVER',name='retrieval.search',level='ERROR',statusMessage='timeout',
          metadata={'nkb.retrieval.outcome':'failure','error.type':'timeout'}),
      row(12,type='CHAIN',name='index.run',metadata={'nkb.index.operation':'run','nkb.index.outcome':'success'})]
    remote=[{'id':'00000000000000fe','type':'CHAIN','name':'index.run','traceName':None,'input':expected['remote_input'],
             'output':expected['remote_result'],'parentObservationId':'1234567890abcdef','sessionId':None,
             'level':'INFO','statusMessage':None,
             'metadata':{'nkb.index.operation':'run','nkb.index.outcome':'success',
                         'input':envelope('index_input'),'output':envelope('index_result')}}]
    return rows,remote


def test_kb_payload_topology_and_sentinels():
    m=module()
    payload,expected=m.kb_synthetic_payload()
    spans=[span for batch in payload['resourceSpans'] for scope in batch['scopeSpans'] for span in scope['spans']]
    assert len(spans)==13
    assert {span['traceId'] for span in spans}=={expected['trace'],expected['remote']}
    root=next(span for span in spans if span['spanId']=='0000000000000001')
    assert 'parentSpanId' not in root
    nested=next(span for span in spans if span['spanId']=='0000000000000009')
    assert nested['parentSpanId']=='0000000000000001'
    remote=next(span for span in spans if span['spanId']=='00000000000000fe')
    assert remote['parentSpanId']=='1234567890abcdef' and remote['traceId']==expected['remote']
    conflict=next(span for span in spans if span['spanId']=='0000000000000008')
    keys={item['key'] for item in conflict['attributes']}
    assert {'nkb.retrieval.operation','nkb.index.operation'} <= keys
    illegal=next(span for span in spans if span['spanId']=='000000000000000a')
    kinds={item['key']:next(iter(item['value'])) for item in illegal['attributes']}
    assert kinds['nkb.retrieval.duration_ms']=='boolValue' and kinds['nkb.index.chunk_count']=='stringValue'
    assert kinds['nkb.model.usage_known']=='intValue' and kinds['error.type']=='stringValue'
    assert len(expected['sentinels'])>=10
    assert all(span['name']==expected['sentinels'][4] for span in spans)
    probe_trace,probe=m.kb_probe_payload()
    assert probe['resourceSpans'][0]['resource']['attributes'][0]['key']=='service.name'
    body=probe['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
    assert body['name']=='index.run' and 'parentSpanId' not in body


def test_kb_projection_assertions_fail_on_real_mutations():
    m=module()
    _,expected=m.kb_synthetic_payload()
    rows,remote=kb_expected_rows(expected)
    m.assert_kb_projection(rows,remote,expected)
    m.assert_kb_trace_bodies({'trace':{'input':expected['index_input'],'output':expected['index_result']},'remote':{}},expected)
    mutations=[
        (0,{**rows[0],'type':'GENERATION'}),                                # 类型映射错误
        (3,{**rows[3],'usageDetails':{}}),                                   # 真实零丢失
        (4,{**rows[4],'usageDetails':{'input':1,'total':1}}),                # unknown 被补造
        (8,{**rows[8],'input':'tampered nested body'}),                      # 嵌套 run 正文被篡改
        (9,{**rows[9],'metadata':{'nkb.index.chunk_count':3}}),              # 非法类型守卫泄漏
        (10,{**rows[10],'statusMessage':'TimeoutError(...)'}),               # 非枚举 status_message
        (7,{**rows[7],'input':expected['retrieval_input']}),                 # 冲突回落仍带正文
        (0,{**rows[0],'metadata':{**rows[0]['metadata'],'input':{'source':'index_input'}}}),       # 完整性元数据丢失
    ]
    for index,mutated in mutations:
        with pytest.raises(AssertionError):
            m.assert_kb_projection(rows[:index]+[mutated]+rows[index+1:],remote,expected)
    with pytest.raises(AssertionError):  # 远程父级被改写
        m.assert_kb_projection(rows,[{**remote[0],'parentObservationId':'0000000000000001'}],expected)
    with pytest.raises(AssertionError):
        m.assert_kb_trace_bodies({'trace':{'input':None,'output':None},'remote':{}},expected)
    with pytest.raises(AssertionError):
        m.assert_kb_trace_bodies({'trace':{'input':expected['index_input'],'output':expected['index_result']},
                                  'remote':{'input':expected['remote_input']}},expected)
    with pytest.raises(AssertionError):  # 秘密哨兵出现在 KB 分支输出
        m.assert_kb_projection(rows,[{**remote[0],'name':expected['sentinels'][0]}],expected)


def test_kb_pipeline_real_fork_projection(tmp_path,monkeypatch):
    m=module()
    kb_payload,kb_expected=m.kb_synthetic_payload()
    rows,remote=kb_expected_rows(kb_expected)
    agent_trace,agent_payload,agent_expected=m.synthetic_payload()
    agent_rows=[{'id':f'{i:016x}'} for i in range(1,19)]
    # kb_synthetic rebuilds payloads internally; freeze them so trace ids match by_trace.
    monkeypatch.setattr(m,'synthetic_payload',lambda:(agent_trace,agent_payload,agent_expected))
    monkeypatch.setattr(m,'kb_synthetic_payload',lambda:(kb_payload,kb_expected))
    merge_trace='f'*32
    merge_rows=[{'id':'a'*16,'type':'GENERATION','name':'chat','parentObservationId':None,
                 'sessionId':'e2e-langfuse-'+merge_trace,'traceName':'chat','input':'agent parent input'},
                {'id':'b'*16,'type':'RETRIEVER','name':'retrieval.search','parentObservationId':'a'*16,
                 'sessionId':None,'traceName':None,'input':'kb child input'}]
    merge_payload={'resourceSpans':[
        {'resource':{'attributes':[m.attr('service.name','n-agent')]},'scopeSpans':[{'spans':[{}]}]},
        {'resource':{'attributes':[m.attr('service.name','n-kb')]},'scopeSpans':[{'spans':[{}]}]}]}
    monkeypatch.setattr(m,'shared_trace_payload',lambda:(merge_trace,merge_payload))
    by_trace={kb_expected['trace']:rows,kb_expected['remote']:remote,agent_trace:agent_rows,merge_trace:merge_rows}
    posted=[]
    def fake_request(url,data=None,headers=None,timeout=30,method=None):
        assert url=='http://otel-collector:4318/v1/traces'
        posted.append(data); return {}
    monkeypatch.setattr(m,'request',fake_request)
    monkeypatch.setattr(m,'observations',lambda get,trace:[dict(row) for row in by_trace[trace]])
    compared=[]
    monkeypatch.setattr(m,'compare_backends',lambda tempo,observations:compared.append((tempo,observations)))
    def fake_tempo(trace):
        if trace==agent_trace:
            return {'batches':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':[{'spanId':row['id']} for row in agent_rows]}]}]}
        return {'batches':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':'n-kb'}}]},
            'scopeSpans':[{'spans':[{'spanId':'0000000000000001','attributes':[
                {'key':'nkb.retrieval.operation','value':{'stringValue':'search'}},
                {'key':'nkb.index.operation','value':{'stringValue':'run'}},
                *[{'key':sentinel,'value':{'stringValue':sentinel}} for sentinel in kb_expected['sentinels']]]}]}]}]}
    def runner_with(directory):
        runner=object.__new__(m.Runner)
        runner.report=m.Report(directory,'unit','https://example.org/explore')
        runner.tempo=fake_tempo; runner.get=lambda url:{}
        runner.trace=None; runner.confirmed_trace=None; runner.stage='start'
        return runner
    runner=runner_with(tmp_path)
    assert runner.check('kb-synthetic',runner.kb_synthetic) is True
    assert len(posted)==1
    services={batch['resource']['attributes'][0]['value']['stringValue'] for batch in posted[0]['resourceSpans']}
    assert services=={'n-agent','n-kb','e2e-excluded'}
    assert compared and compared[0][1]==agent_rows
    assert next(row for row in runner.report.data['cases'] if row['id']=='kb-synthetic')['result']=='PASS'
    # 错误投影必须让场景失败，而不是空跑通过
    by_trace[kb_expected['trace']]=[{**rows[0],'type':'GENERATION'}]+rows[1:]
    failing=runner_with(tmp_path/'bad')
    assert failing.check('kb-synthetic',failing.kb_synthetic) is False
    assert next(row for row in failing.report.data['cases'] if row['id']=='kb-synthetic')['result']=='FAIL'


def test_shared_project_purity_replaces_cross_project_isolation():
    m=module()
    _,kb_expected=m.kb_synthetic_payload()
    _,_,agent_expected=m.synthetic_payload()
    rows,remote=kb_expected_rows(kb_expected)
    agent_rows=[{'id':f'{i:016x}'} for i in range(1,19)]
    m.assert_shared_project_purity(agent_rows,rows,remote,agent_expected,kb_expected)
    agent_leak=agent_rows+[{'id':'zz','name':kb_expected['sentinels'][0]}]
    with pytest.raises(AssertionError):
        m.assert_shared_project_purity(agent_leak,rows,remote,agent_expected,kb_expected)
    kb_leak=rows+[{'id':'zy','name':agent_expected['sentinels'][0]}]
    with pytest.raises(AssertionError):
        m.assert_shared_project_purity(agent_rows,kb_leak,remote,agent_expected,kb_expected)


def test_kb_fault_injection(tmp_path,monkeypatch):
    m=module()
    monkeypatch.setattr(m.time,'sleep',lambda seconds:None)
    # Failure counters are read before the probe POST, so the stub keys on its own POST count.
    def make_failures(kb_delta=0.0,original_delta=0.0,langfuse_delta=0.0):
        state={'posts':0}
        def fake(url,data=None,headers=None,timeout=30,method=None):
            if url.endswith('/v1/traces'): state['posts']+=1
            return {}
        def failures():
            value={'kb':0.0,'langfuse':0.0,'original':0.0}
            if state['posts']:
                value['kb']+=1+kb_delta; value['original']+=original_delta; value['langfuse']+=langfuse_delta
            return value
        return fake,failures
    def runner_with(directory,failures,tempo=None):
        runner=object.__new__(m.Runner)
        runner.report=m.Report(directory,'unit','https://example.org/explore')
        runner.collector_failures=failures
        runner.tempo=tempo or (lambda trace:{'batches':[{'resource':{'attributes':[]},'scopeSpans':[{'spans':[]}]}]})
        runner.get=lambda url:{}
        runner.trace=None; runner.confirmed_trace=None; runner.stage='start'
        return runner
    # 停机：KB exporter 失败但 Tempo 接收
    fake,failures=make_failures()
    monkeypatch.setattr(m,'request',fake)
    monkeypatch.setattr(m,'observations',lambda get,trace:[])
    down=runner_with(tmp_path/'down',failures).fault_web_kb()
    assert down['tempo_received'] is True and down['exporter_failures_after']['kb']==1.0
    assert down['exporter_failures_after']['original']==0.0
    # 恢复：新探针完整投递
    monkeypatch.setattr(m,'observations',lambda get,trace:[{'type':'CHAIN','input':'kb fault probe','output':None,'id':'1'}])
    assert runner_with(tmp_path/'ok',failures).kb_recovered()['delivered_after_recovery'] is True
    monkeypatch.setattr(m,'observations',lambda get,trace:[{'type':'SPAN','input':'kb fault probe','output':None,'id':'1'}])
    with pytest.raises(AssertionError):
        runner_with(tmp_path/'wrong',failures).kb_recovered()
    # 队列满：KB 队列丢弃、峰值不超容量、其余分支不受影响
    state={'posts':0}
    def queue_fake(url,data=None,headers=None,timeout=30,method=None):
        if url.endswith('/v1/traces'): state['posts']+=1
        return {}
    monkeypatch.setattr(m,'request',queue_fake)
    def kb_queue():
        return {'failed':1.0,'size':9000000.0} if state['posts'] else {'failed':0.0,'size':0.0}
    def steady_failures():
        return {'kb':0.0,'langfuse':0.0,'original':0.0}
    runner=runner_with(tmp_path/'queue',steady_failures)
    runner.collector_kb_queue=kb_queue
    flooded=runner.fault_queue_kb()
    assert flooded['kb_enqueue_failed_delta']==1.0 and flooded['peak_queue_bytes']<=16777216
    assert flooded['other_exporters_unaffected'] is True
    def leaking_failures():
        return {'kb':0.0,'langfuse':1.0 if state['posts'] else 0.0,'original':0.0}
    state['posts']=0
    leak=runner_with(tmp_path/'queue-bad',leaking_failures)
    leak.collector_kb_queue=kb_queue
    with pytest.raises(AssertionError): leak.fault_queue_kb()
    # 单共享项目：KB exporter 与主 exporter 使用同一组凭据
    config=collector_module().build_config('pk-na','sk-na')
    assert config['exporters']['otlphttp/langfuse-kb']['headers']['Authorization'] \
        == config['exporters']['otlphttp/langfuse']['headers']['Authorization']


def collector_module():
    path=SOURCE.parents[2]/'docker/observability/langfuse-config.py'
    assert path.is_file(), 'collector config generator is missing'
    spec=importlib.util.spec_from_file_location('langfuse_config_e2e',path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_disable_switch_short_circuits_before_credentials(tmp_path,monkeypatch):
    m=collector_module()
    reads=[]
    monkeypatch.setattr(m,'settings',lambda directory,root:(False,Path('/missing/credentials'),m.DEFAULT_ENDPOINT,m.DEFAULT_ENDPOINT))
    monkeypatch.setattr(m,'load_credentials',lambda root,path:reads.append(path))
    assert m.prepare(tmp_path,tmp_path) is None and reads==[]
    import yaml as _yaml
    base=_yaml.safe_load((SOURCE.parents[2]/'docker/observability/otel-collector.yaml').read_text())
    assert {'traces','metrics','logs'} <= set(base['service']['pipelines'])


def decode_value(node):
    if 'kvlistValue' in node: return {item['key']:decode_value(item['value']) for item in node['kvlistValue']['values']}
    if 'arrayValue' in node: return [decode_value(item) for item in node['arrayValue']['values']]
    if 'intValue' in node: return int(node['intValue'])
    if 'doubleValue' in node: return float(node['doubleValue'])
    return next(iter(node.values())) if isinstance(node,dict) and node else None


def decode_batch(batch):
    import base64
    def identifier(value):
        if not value: return ''
        return value if re.fullmatch('[0-9a-f]{32}|[0-9a-f]{16}',value) else base64.b64decode(value).hex()
    resource={item['key']:decode_value(item['value']) for item in batch.get('resource',{}).get('attributes',[])}
    result=[]
    for scope in batch.get('scopeSpans',[]):
        scope_node=scope.get('scope',{})
        for span in scope.get('spans',[]):
            result.append({
                'traceId':identifier(span.get('traceId')),'spanId':identifier(span.get('spanId')),'name':span['name'],
                'parentSpanId':identifier(span.get('parentSpanId','')),
                'status':{'code':span.get('status',{}).get('code',0),'message':span.get('status',{}).get('message','')},
                'resource':resource,
                'scope':{'name':scope_node.get('name') or '','version':scope_node.get('version') or '',
                         'attributes':{item['key']:decode_value(item['value']) for item in scope_node.get('attributes',[])}},
                'attrs':{item['key']:decode_value(item['value']) for item in span.get('attributes',[])},
                'events':[{'name':event['name'],'attrs':{item['key']:decode_value(item['value']) for item in event.get('attributes',[])}}
                          for event in span.get('events',[])],
                'links':[{'attrs':{item['key']:decode_value(item['value']) for item in link.get('attributes',[])}}
                         for link in span.get('links',[])]})
    return result


def decode_export(path):
    buckets={}
    for line in path.read_text().splitlines():
        if not line.strip(): continue
        for batch in json.loads(line).get('resourceSpans',[]):
            for span in decode_batch(batch):
                buckets.setdefault(span['resource'].get('service.name'),[]).append(span)
    return buckets


SINK_SCRIPT='''import gzip,sys
from http.server import HTTPServer,BaseHTTPRequestHandler
from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
out=open(sys.argv[1],'a',buffering=1)
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        if self.headers.get('Content-Encoding')=='gzip': body=gzip.decompress(body)
        request=ExportTraceServiceRequest(); request.ParseFromString(body)
        import json as _json
        out.write(_json.dumps(_json.loads(MessageToJson(request)))+'\\n')
        self.send_response(200); self.end_headers()
    def log_message(self,*args): pass
HTTPServer(('127.0.0.1',int(sys.argv[2])),Handler).serve_forever()
'''


def isolated_config(doc,port):
    """Only IO is replaced: the real fork runs the real processors of both pipelines."""
    processors=dict(doc['processors'])
    processors['memory_limiter']={'check_interval':'1s','limit_mib':160,'spike_limit_mib':32}
    pipelines={name:{'receivers':['otlp'],
                     'processors':['memory_limiter']+[step for step in pipeline['processors'] if step!='memory_limiter'],
                     'exporters':['otlphttp/sink']} for name,pipeline in doc['service']['pipelines'].items()}
    return {'receivers':{'otlp':{'protocols':{'http':{'endpoint':'0.0.0.0:4318'}}}},
            'processors':processors,
            'exporters':{'otlphttp/sink':{'endpoint':f'http://host.docker.internal:{port}',
                                          'traces_endpoint':f'http://host.docker.internal:{port}/v1/traces'}},
            'service':{'telemetry':{'metrics':{'level':'none'}},'pipelines':pipelines}}


FORK_IMAGE='n-agent-langfuse-collector:0.137.0'


def fork_available():
    import subprocess
    try:
        subprocess.run(['docker','image','inspect',FORK_IMAGE],check=True,capture_output=True,timeout=15)
        return True
    except (OSError,subprocess.SubprocessError):
        return False


def run_fork(config_builder,payload,directory,need):
    """Real fork with isolated IO: OTLP/HTTP in, otlphttp to a host-side sink out."""
    import socket
    import subprocess,uuid
    import urllib.request
    directory.mkdir(parents=True,exist_ok=True)
    probe=socket.socket(); probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]; probe.close()
    (directory/'sink.py').write_text(SINK_SCRIPT)
    output=directory/'sink.jsonl'
    sink=subprocess.Popen([sys.executable,'-I',str(directory/'sink.py'),str(output),str(port)],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    name='nagent-fork-e2e-'+uuid.uuid4().hex
    try:
        (directory/'config.yaml').write_text(yaml.safe_dump(config_builder(port)))
        subprocess.run(['docker','run','-d','--name',name,'-p','127.0.0.1::4318','-v',f'{directory}:/out',
                        FORK_IMAGE,'--config=/out/config.yaml'],check=True,capture_output=True,timeout=30)
        published=subprocess.check_output(['docker','port',name,'4318/tcp'],text=True,timeout=15).strip().rsplit(':',1)[1]
        deadline=time.monotonic()+60
        while True:
            try:
                request=urllib.request.Request(f'http://127.0.0.1:{published}/v1/traces',
                                               json.dumps(payload).encode(),{'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3) as response:
                    assert response.status==200
                break
            except (OSError,AssertionError):
                if time.monotonic()>deadline:
                    pytest.fail(subprocess.check_output(['docker','logs',name],stderr=subprocess.STDOUT,text=True,timeout=15))
                time.sleep(.2)
        offset=0; buckets={}
        while time.monotonic()<deadline:
            if sink.poll() is not None:
                pytest.fail('OTLP sink exited early: '+sink.stderr.read().decode(errors='replace'))
            text=output.read_text() if output.exists() else ''
            for line in text[offset:].splitlines():
                if not line.strip(): continue
                for batch in json.loads(line).get('resourceSpans',[]):
                    for span in decode_batch(batch):
                        buckets.setdefault(span['resource'].get('service.name'),[]).append(span)
            offset=len(text)
            if all(len(buckets.get(service,[]))>=count for service,count in need.items()):
                return buckets
            time.sleep(.2)
        pytest.fail('fork did not deliver pipelines before deadline: '
                    +str({service:len(spans) for service,spans in buckets.items()}))
    finally:
        errors=[]
        if sink.poll() is None:
            sink.terminate()
            try: sink.wait(timeout=10)
            except subprocess.TimeoutExpired: sink.kill(); sink.wait(timeout=10)
        try: subprocess.run(['docker','rm','-f',name],check=False,capture_output=True,timeout=15)
        except (subprocess.SubprocessError,OSError) as exc: errors.append(str(exc))
        try:
            remaining=subprocess.check_output(['docker','ps','-a','--filter',f'name={name}','--format','{{.Names}}'],
                                              text=True,timeout=15)
            if name in remaining: errors.append('container still present: '+name)
        except (subprocess.SubprocessError,OSError) as exc: errors.append(str(exc))
        if errors:
            diagnostic='fork cleanup: '+'; '.join(errors)
            if sys.exception() is not None: sys.exception().add_note(diagnostic)
            else: raise RuntimeError(diagnostic)


def assert_kb_export(spans,expected):
    """Real fork output: every projection branch, guards, sentinels and the final allowlist."""
    by_id={span['spanId']:span for span in spans}
    assert set(by_id)=={f'{i:016x}' for i in range(1,13)}|{'00000000000000fe'}
    def attrs(identifier): return by_id[f'{identifier:016x}']['attrs']
    assert all(span['status']['message']=='' for span in spans)
    root=by_id[f'{1:016x}']
    assert root['name']=='index.run'
    a=root['attrs']
    assert a['langfuse.observation.type']=='chain'
    assert a['langfuse.observation.input']==expected['index_input']
    assert a['langfuse.observation.output']==expected['index_result']
    assert a['langfuse.trace.name']=='index.run'
    assert a['langfuse.trace.input']==expected['index_input']
    assert a['langfuse.trace.output']==expected['index_result']
    assert a['langfuse.release']=='e2e-kb-release' and a['langfuse.environment']=='e2e'
    assert a['langfuse.observation.metadata.nkb.index.operation']=='run'
    assert a['langfuse.observation.metadata.nkb.index.duration_ms']==12.5
    assert a['langfuse.observation.metadata.nkb.index.chunk_count']==7
    assert json.loads(a['langfuse.observation.metadata.input'])=={'source':'index_input','original_bytes':64,
        'observed_bytes':32,'truncated':True,'incomplete':True}
    assert json.loads(a['langfuse.observation.metadata.output'])['source']=='index_result'
    assert [event['name'] for event in root['events']]==['nkb.index.completed']
    assert root['events'][0]['attrs']=={'nkb.index.outcome':'success'}
    assert all(not link['attrs'] for link in root['links'])
    retriever=attrs(2)
    assert retriever['langfuse.observation.type']=='retriever' and by_id[f'{2:016x}']['name']=='retrieval.search'
    assert retriever['langfuse.observation.input']==expected['retrieval_input']
    assert retriever['langfuse.observation.output']==expected['retrieval_output']
    assert retriever['langfuse.observation.metadata.nkb.retrieval.result_count']==2
    embedding=attrs(3)
    assert embedding['langfuse.observation.type']=='embedding' and by_id[f'{3:016x}']['name']=='embeddings'
    assert embedding['langfuse.observation.input']==expected['embedding_input']
    assert 'langfuse.observation.output' not in embedding
    assert json.loads(embedding['langfuse.observation.usage_details'])=={'input':5}
    assert embedding['langfuse.observation.model.name']==expected['model']
    assert json.loads(embedding['langfuse.observation.metadata.input'])=={'source':'embedding_input'}
    assert json.loads(attrs(4)['langfuse.observation.usage_details'])=={'input':0}
    assert 'langfuse.observation.usage_details' not in attrs(5)
    assert attrs(5)['langfuse.observation.model.name']==expected['model']
    assert attrs(5)['langfuse.observation.metadata.nkb.model.usage_known'] is False
    for identifier in (6,7,8):
        a=attrs(identifier)
        assert a['langfuse.observation.type']=='span' and by_id[f'{identifier:016x}']['name']=='span'
        assert 'langfuse.observation.input' not in a and 'langfuse.observation.output' not in a
    nested=attrs(9)
    assert nested['langfuse.observation.type']=='chain'
    assert nested['langfuse.observation.input']==expected['nested_input']
    assert nested['langfuse.observation.output']==expected['nested_result']
    assert 'langfuse.trace.input' not in nested and 'langfuse.trace.output' not in nested
    illegal=attrs(10)
    assert illegal['langfuse.observation.type']=='span'
    # The linkprocessor legitimately adds metadata.links for spans with links;
    # every typed-guard metadata key must still be gone.
    assert not any(key.startswith('langfuse.observation.metadata.') for key in illegal
                   if key!='langfuse.observation.metadata.links')
    failed=attrs(11)
    assert failed['langfuse.observation.type']=='retriever'
    assert failed['langfuse.observation.level']=='ERROR'
    assert failed['langfuse.observation.status_message']=='timeout'
    assert 'langfuse.observation.output' not in failed
    assert failed['langfuse.observation.metadata.nkb.retrieval.outcome']=='failure'
    assert failed['langfuse.observation.metadata.error.type']=='timeout'
    childless=attrs(12)
    assert childless['langfuse.observation.type']=='chain'
    assert 'langfuse.observation.input' not in childless and 'langfuse.observation.output' not in childless
    remote=by_id['00000000000000fe']
    assert remote['attrs']['langfuse.observation.type']=='chain'
    assert remote['attrs']['langfuse.observation.input']==expected['remote_input']
    assert remote['attrs']['langfuse.observation.output']==expected['remote_result']
    assert 'langfuse.trace.input' not in remote['attrs'] and 'langfuse.trace.output' not in remote['attrs']
    assert remote['parentSpanId']=='1234567890abcdef'
    for span in spans:
        assert all(key.startswith('langfuse.') for key in span['attrs'])
        assert span['resource']=={'service.name':'n-kb'}
        assert span['scope']=={'name':'','version':'','attributes':{}}


@pytest.mark.skipif(not fork_available(),reason='real fork image n-agent-langfuse-collector:0.137.0 unavailable')
def test_real_fork_dual_pipeline_isolated_io():
    """AC: 真实 0.137.0 fork + 隔离 IO 双管线；n-agent 输出与 HEAD 基线逐字段等价。"""
    import shutil,subprocess,tempfile,uuid
    m=module()
    config=collector_module()
    root=SOURCE.parents[2]
    current=config.build_config('pk-e2e','sk-e2e')
    head_source=subprocess.check_output(['git','-C',str(root),'show','HEAD:docker/observability/langfuse-config.py'],
                                        text=True,timeout=15)
    scratch=Path(tempfile.mkdtemp(prefix='fork-e2e-',dir=root/'locals/harness_tmp'))
    try:
        head_file=scratch/'head_langfuse_config.py'
        head_file.write_text(head_source)
        spec=importlib.util.spec_from_file_location('head_langfuse_config',head_file)
        head=importlib.util.module_from_spec(spec); spec.loader.exec_module(head)
        # HEAD has no KB pipeline; the baseline only feeds the n-agent pipeline.
        baseline=head.build_config('pk-e2e','sk-e2e')
        _,agent_payload,agent_expected=m.synthetic_payload()
        kb_payload,kb_expected=m.kb_synthetic_payload()
        combined={'resourceSpans':agent_payload['resourceSpans']+kb_payload['resourceSpans']}
        agent_count=agent_expected['observation_count']
        baseline_buckets=run_fork(lambda port:isolated_config(baseline,port),agent_payload,
                                  scratch/'baseline',{'n-agent':agent_count})
        buckets=run_fork(lambda port:isolated_config(current,port),combined,
                         scratch/'current',{'n-agent':agent_count,
                                            'n-kb':kb_expected['counts'][kb_expected['trace']]+kb_expected['counts'][kb_expected['remote']]})
    finally:
        shutil.rmtree(scratch,ignore_errors=True)
    assert set(buckets)=={'n-agent','n-kb'}
    assert_kb_export(buckets['n-kb'],kb_expected)
    # Sentinels are controlled fake secrets: neither project's Langfuse export may carry any of them.
    for sentinel in (*kb_expected['sentinels'],*agent_expected['sentinels']):
        assert sentinel not in json.dumps(buckets['n-agent'])
        assert sentinel not in json.dumps(buckets['n-kb'])
    keep={f'{i:016x}' for i in range(1,19)}
    def normalized(bucket):
        return sorted((span for span in bucket if span['spanId'] in keep),key=lambda span:span['spanId'])
    assert normalized(buckets['n-agent'])==normalized(baseline_buckets['n-agent'])
    sample=next(span for span in buckets['n-agent'] if span['spanId']=='0000000000000002')
    assert sample['attrs']['langfuse.observation.input']=='short exact input'
