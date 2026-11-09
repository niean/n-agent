"""Docker-only Langfuse integration probes; stdout contains safe summaries only."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
import uuid
import threading


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urlopen(request, timeout=30):
    # Project Basic auth must neither use ambient proxy settings nor follow redirects.
    return build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=timeout)

spec = importlib.util.spec_from_file_location('langfuse_result_report', Path(__file__).with_name('result_report.py'))
reporting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporting)


def safe_error(error):
    # HTTP errors can contain credentials, URLs and request bodies. Retain class only.
    code=getattr(error,'code',None)
    return type(error).__name__ + (f' HTTP {code}' if isinstance(code,int) else '')


class Report:
    def __init__(self, directory, run_id, base_url):
        self.directory, self.base_url = Path(directory), base_url
        path = self.directory/'report.json'
        self.data = json.loads(path.read_text()) if path.exists() else {'run_id':run_id, 'suite':'langfuse-smoke', 'cases':[]}

    def plan(self, keys):
        for key in keys:
            if not any(row['id']==key for row in self.data['cases']):
                self.record(key,'SKIP',reason='未执行；前置步骤失败或执行中断',evidence={'unexecuted':True})

    def record(self, key, result, trace=None, reason='', evidence=None):
        self.data['cases']=[row for row in self.data['cases'] if not (row['id']==key and row.get('evidence',{}).get('unexecuted'))]
        attempt = 1 + max((r['attempt'] for r in self.data['cases'] if r['id']==key), default=0)
        for trace in (trace or [None]) if isinstance(trace,list) else [trace]:
            self.data['cases'].append({'category':'OTel','id':key,'case_id':key,'name':key,'attempt':attempt,
            'result':result,'trace_id':trace,'trace_url':reporting.trace_url(self.base_url, trace),
                'reason':reason or ('此检查不产生Trace' if not trace else ''), 'evidence':evidence or {}})
        self.save()

    def save(self):
        final, _ = reporting.split_attempts(self.data['cases'])
        self.data['exit_code'] = int(any(r['result']!='PASS' for r in final))
        reporting.write_report(self.directory, self.data)
        return self.data


class SessionManifest:
    def __init__(self,directory,run_id):
        self.path=Path(directory)/'owned-sessions.jsonl'; self.run_id=run_id

    def add(self,session_id,source):
        if not re.fullmatch('e2e-langfuse-[0-9a-f]{32}',session_id) or source not in ('api','dashboard'):
            raise ValueError('invalid owned session')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        entry={'run_id':self.run_id,'session_id':session_id,'source':source}
        descriptor=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        try: os.write(descriptor,(json.dumps(entry)+'\n').encode())
        finally: os.close(descriptor)

    def entries(self):
        if not self.path.exists(): return []
        entries=[json.loads(line) for line in self.path.read_text().splitlines()]
        return [entry for entry in entries if entry['run_id']==self.run_id]

    def owns(self,session_id,source):
        return (re.fullmatch('e2e-langfuse-[0-9a-f]{32}',session_id) is not None and
                any(entry['session_id']==session_id and entry['source']==source for entry in self.entries()))


def request(url, data=None, headers=None, timeout=30, method=None):
    body = None if data is None else json.dumps(data).encode()
    headers = {'Content-Type':'application/json', **(headers or {})}
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def poll(fn, seconds=100):
    deadline=time.monotonic()+seconds
    last=None
    while time.monotonic()<deadline:
        try:
            value=fn()
            if value: return value
        except Exception as error: last=error
        time.sleep(2)
    raise TimeoutError('bounded delivery deadline') from last


def observations(get, trace):
    rows=[]; cursor=None; seen=set()
    while True:
        query={'traceId':trace,'fields':'basic,time,io,metadata,model,usage,trace_context','expandMetadata':'input,output,links,system_prompt,tool_definitions,error,judge_reason','limit':100}
        if cursor: query['cursor']=cursor
        data=get('http://n-langfuse-web:3000/api/public/v2/observations?'+urlencode(query))
        rows.extend(data.get('data', []))
        cursor=data.get('meta', {}).get('cursor')
        if not cursor: return rows
        if cursor in seen: raise AssertionError('pagination cursor repeated')
        seen.add(cursor)


def attr(key,value):
    if isinstance(value,(list,tuple)):
        return {'key':key,'value':{'arrayValue':{'values':[attr('',item)['value'] for item in value]}}}
    kind='boolValue' if type(value) is bool else 'intValue' if type(value) is int else 'doubleValue' if type(value) is float else 'stringValue'
    return {'key':key,'value':{kind:value}}


def synthetic_payload():
    trace=uuid.uuid4().hex; now=time.time_ns()
    sentinels=['e2e-private-'+x for x in ('unknown','injected','resource','scope','name','status','event','event-attr','link')]
    large_in='入'*341333+'x'; large_out='出'*341333+'y'
    base={'gen_ai.conversation.id':'e2e-langfuse-'+trace,'nagent.agent.run_id':'run-'+trace,
          'unknown.leak':sentinels[0], 'langfuse.observation.input':sentinels[1]}
    definitions=[('task.run', {'nagent.content.input_messages':'entry input','nagent.content.output_messages':'entry output'}), ('chat', {'gen_ai.request.model':'e2e-model','gen_ai.usage.input_tokens':3,
          'gen_ai.usage.output_tokens':0,'nagent.model.cache_read_tokens':2,'nagent.model.cache_write_tokens':1,
          'gen_ai.provider.name':'openai','gen_ai.response.finish_reasons':['stop'],'gen_ai.request.stream':True,
          'nagent.model.parameter.temperature':0.25,'nagent.model.parameter.presence_penalty':-1,
          'nagent.model.completion_start_time':datetime.fromtimestamp(now/1e9,timezone.utc).isoformat().replace('+00:00','Z'),
          'nagent.model.cost_usd':0.125,'nagent.model.cost_status':'estimated','nagent.model.pricing_version':'e2e-v1',
          'nagent.model.cost_currency':'USD',
          'nagent.model.cost_decimal':'0.125','nagent.model.price_id':'e2e-price','nagent.model.billing_provider':'openai',
          'nagent.model.billing_model':'e2e-model','nagent.model.cost_input_usd':0.05,'nagent.model.cost_output_usd':0.05,
          'nagent.model.cost_cache_read_usd':0.025,
          'nagent.content.system_prompt':'safe policy','nagent.content.tool_definitions':'safe tool schema',
          'nagent.content.input_messages':'short exact input','nagent.content.output_messages':'short exact output'}),
        ('execute_tool', {'nagent.tool.executed':False,'nagent.tool.policy_outcome':'deny','nagent.tool.outcome':'denied',
          'nagent.content.input_messages':'lower input','nagent.content.output_messages':'lower output',
          **{f'nagent.content.{field}_{suffix}':value for field in ('input_messages','output_messages') for suffix,value in [('original_bytes',999),('observed_bytes',888),('incomplete',True),('truncated',True)]},
          'nagent.content.tool_arguments':'{"x":1}','nagent.content.tool_result':'denied'}),
        ('context.compress', {}), ('execute_tool', {'error.type':'ValueError','nagent.tool.outcome':'failure'}),
        ('chat', {'nagent.model.outcome':'cancelled'}), ('chat', {'error.type':'TimeoutError','nagent.model.outcome':'timed_out'}),
        ('chat', {'nagent.content.input_messages':large_in,'nagent.content.output_messages':large_out,
          'nagent.content.input_messages_original_bytes':1024010,'nagent.content.output_messages_original_bytes':1024020,
          'nagent.content.input_messages_truncated':True,'nagent.content.output_messages_truncated':True,
          'nagent.content.input_messages_incomplete':True,'nagent.content.output_messages_incomplete':True}),
        ('invoke_agent', {}),
        ('knowledge.search', {'nagent.content.retrieval_query':'lookup','nagent.content.retrieval_results':'safe hits'}),
        ('task.evaluate', {'nagent.task.judge_attempt':1,'nagent.task.outcome':'failure','error.type':'ValueError'}),
        ('task.evaluate', {'nagent.task.judge_attempt':2,'nagent.task.achieved':False,'nagent.task.outcome':'success','nagent.content.judge_verdict':'false','nagent.content.judge_reason':'goal missing'}),
        ('memory.search', {}),('schedule.run', {}),('delegation.run_member', {}),('gateway.handle_message', {}),('skill.review', {}),('curator.run', {})]
    spans=[]
    for i,(operation,extra) in enumerate(definitions):
        s={'traceId':trace,'spanId':f'{i+1:016x}','name':sentinels[4], 'startTimeUnixNano':str(now+i),
           'endTimeUnixNano':str(now+1000000+i),'attributes':[attr(k,v) for k,v in {**base,**({'gen_ai.operation.name':operation} if operation in ('invoke_agent','chat','execute_tool') else {f'nagent.{operation.split(".")[0]}.operation':operation.split('.')[1]}),**extra}.items()],
           'status':{'code':2 if i in (4,6,10) else 1,'message':sentinels[5]},
           'events':[{'name':sentinels[6],'timeUnixNano':str(now),'attributes':[attr('unknown.event',sentinels[7])]}],
           'links':[{'traceId':'a'*32,'spanId':'b'*16,'attributes':[attr('unknown.link',sentinels[8])]}]}
        if i: s['parentSpanId']='0000000000000001'
        spans.append(s)
    payload={'resourceSpans':[{'resource':{'attributes':[attr('service.name','n-agent'),attr('service.version','e2e-release'),attr('deployment.environment.name','e2e'),attr('unknown.resource',sentinels[2])]},
        'scopeSpans':[{'scope':{'name':sentinels[3], 'version':sentinels[3], 'attributes':[attr('unknown.scope',sentinels[3])]},'spans':spans}]}]}
    excluded={**spans[0],'spanId':'ffffffffffffffff','name':'e2e-excluded-service-sentinel'}
    payload['resourceSpans'].append({'resource':{'attributes':[attr('service.name','e2e-excluded')]},
                                   'scopeSpans':[{'spans':[excluded]}]})
    return trace,payload,{'sentinels':sentinels,'long_input':large_in,'long_output':large_out,'observation_count':len(spans)}


def kb_synthetic_payload():
    """Synthetic N-KB spans covering every projection branch; sentinels are controlled fake secrets."""
    trace=uuid.uuid4().hex; remote=uuid.uuid4().hex; now=time.time_ns()
    sentinels=['e2e-kb-private-'+x for x in ('unknown','injected','resource','scope','name','status','event','event-attr','link','nagent')]
    suffix={'original_bytes':64,'observed_bytes':32,'truncated':True,'incomplete':True}
    def content(field,body,meta=True):
        attrs={f'nkb.content.{field}':body}
        if meta: attrs.update({f'nkb.content.{field}_{key}':value for key,value in suffix.items()})
        return attrs
    def span(tid,identifier,extra,parent=None,error=False,events=None):
        s={'traceId':tid,'spanId':f'{identifier:016x}','name':sentinels[4],'startTimeUnixNano':str(now+identifier),
           'endTimeUnixNano':str(now+1000000+identifier),
           'attributes':[attr(key,value) for key,value in {'langfuse.observation.input':sentinels[1],
             'langfuse.session.id':sentinels[1],'gen_ai.conversation.id':sentinels[1],
             'nagent.agent.run_id':sentinels[9],'nagent.content.input_messages':sentinels[9],
             'unknown.leak':sentinels[0],**extra}.items()],
           'status':{'code':2 if error else 1,'message':sentinels[5]},
           'events':events if events is not None else [{'name':sentinels[6],'timeUnixNano':str(now),
              'attributes':[attr('unknown.event',sentinels[7])]}],
           'links':[{'traceId':'c'*32,'spanId':'d'*16,'attributes':[attr('unknown.link',sentinels[8])]}]}
        if parent: s['parentSpanId']=parent
        return s
    # One registered event with a legal fact plus a disguised secret fact and an unknown attribute.
    legal_event={'name':'nkb.index.completed','timeUnixNano':str(now),
        'attributes':[attr('nkb.index.outcome','success'),attr('unknown.event',sentinels[7]),
                      attr('error.type','SQLinjection-evil')]}
    def cost_group(amount, decimal):
        """T5 合法 estimated 成本组：金额、decimal 与受信计费身份齐全。"""
        return {'nkb.model.cost_status':'estimated','nkb.model.cost_currency':'USD',
                'nkb.model.cost_usd':amount,'nkb.model.cost_input_usd':amount,
                'nkb.model.cost_decimal':decimal,'nkb.model.pricing_version':'e2e-kb-v1',
                'nkb.model.price_id':'e2e-embed-price','nkb.model.billing_provider':'ollama',
                'nkb.model.billing_model':'e2e-embed-model'}
    trace_spans=[
      # Global-root index.run: chain with observation and trace bodies.
      span(trace,1,{'nkb.index.operation':'run','nkb.index.outcome':'success','nkb.index.duration_ms':12.5,
        'nkb.index.document_id':'doc-e2e','nkb.index.job_id':'job-e2e','nkb.index.chunk_count':7,
        **content('index_input','kb index input'),**content('index_result','kb index result')},events=[legal_event]),
      span(trace,2,{'nkb.retrieval.operation':'search','nkb.retrieval.outcome':'success','nkb.retrieval.duration_ms':3.25,
        'nkb.retrieval.result_count':2,**content('retrieval_input','kb query'),**content('retrieval_output','kb snippets')},parent=f'{1:016x}'),
      # Generation with known usage, no completeness metadata on the input.
      span(trace,3,{'gen_ai.operation.name':'embeddings','nkb.model.usage_known':True,'gen_ai.usage.input_tokens':5,
        'gen_ai.request.model':'e2e-embed-model','gen_ai.provider.name':'ollama',
        **cost_group(0.125,'0.125'),
        **content('embedding_input','kb embed prefix',meta=False)},parent=f'{1:016x}'),
      # A real zero must survive; an unknown count must be omitted entirely.
      span(trace,4,{'gen_ai.operation.name':'embeddings','nkb.model.usage_known':True,'gen_ai.usage.input_tokens':0,
        'gen_ai.request.model':'e2e-embed-model',**cost_group(0,'0')},parent=f'{1:016x}'),
      # unknown 组：受控 reason、无金额，model.name 不得投影（阻断自动推价）。
      span(trace,5,{'gen_ai.operation.name':'embeddings','nkb.model.usage_known':False,
        'gen_ai.request.model':'e2e-embed-model','nkb.model.cost_status':'unknown',
        'nkb.model.cost_reason':'missing_usage'},parent=f'{1:016x}'),
      # Unregistered operations fall back to a plain span with no body.
      span(trace,6,{'nkb.mcp.operation':'status','nkb.mcp.outcome':'success'},parent=f'{1:016x}'),
      # Content for an operation that never opens it must be deleted, not projected.
      span(trace,7,{'nkb.widget.operation':'frobnicate',**content('embedding_input','unopened body secret')},parent=f'{1:016x}'),
      # Two discriminators at once is a producer bug: plain span, no bodies.
      span(trace,8,{'nkb.retrieval.operation':'search','nkb.index.operation':'run',**content('retrieval_input','conflict body')},parent=f'{1:016x}'),
      # A nested run stays chain but must never write trace bodies.
      span(trace,9,{'nkb.index.operation':'run','nkb.index.outcome':'success',
        **content('index_input','nested input'),**content('index_result','nested result')},parent=f'{1:016x}'),
      # Illegal types and enums are each dropped by their typed guard.
      span(trace,10,{'nkb.index.operation':'delete','nkb.index.duration_ms':True,'nkb.retrieval.duration_ms':True,
        'nkb.index.chunk_count':'three','nkb.model.usage_known':1,'nkb.index.outcome':'exploded',
        'error.type':'tool_error'},parent=f'{1:016x}'),
      # Real ERROR: level and a fixed status_message only.
      span(trace,11,{'nkb.retrieval.operation':'search','nkb.retrieval.outcome':'failure','error.type':'timeout'},parent=f'{1:016x}',error=True),
      # A chain without content keeps neither direction.
      span(trace,12,{'nkb.index.operation':'run','nkb.index.outcome':'success'},parent=f'{1:016x}')]
    # A remote parent must never promote the run to a trace root.
    remote_spans=[span(remote,0xfe,{'nkb.index.operation':'run','nkb.index.outcome':'success',
        **content('index_input','remote input'),**content('index_result','remote result')},parent='1234567890abcdef')]
    def resource(spans):
        return {'resource':{'attributes':[attr('service.name','n-kb'),attr('service.version','e2e-kb-release'),
            attr('deployment.environment.name','e2e'),attr('unknown.resource',sentinels[2])]},
            'scopeSpans':[{'scope':{'name':sentinels[3],'version':sentinels[3],
                'attributes':[attr('unknown.scope',sentinels[3])]},'spans':spans}]}
    payload={'resourceSpans':[resource(trace_spans),resource(remote_spans)]}
    expected={'trace':trace,'remote':remote,'sentinels':sentinels,'model':'e2e-embed-model',
      'index_input':'kb index input','index_result':'kb index result',
      'retrieval_input':'kb query','retrieval_output':'kb snippets','embedding_input':'kb embed prefix',
      'nested_input':'nested input','nested_result':'nested result',
      'remote_input':'remote input','remote_result':'remote result',
      'counts':{trace:12,remote:1}}
    return payload,expected


def kb_probe_payload(body='kb fault probe'):
    """One root index.run span used by fault injection and recovery probes."""
    trace=uuid.uuid4().hex; now=time.time_ns()
    payload={'resourceSpans':[{'resource':{'attributes':[attr('service.name','n-kb'),attr('service.version','e2e-kb-release'),
        attr('deployment.environment.name','e2e')]},
        'scopeSpans':[{'spans':[{'traceId':trace,'spanId':'1234567890abcdef','name':'index.run',
        'startTimeUnixNano':str(now),'endTimeUnixNano':str(now+1000000),
        'attributes':[attr(key,value) for key,value in {'nkb.index.operation':'run','nkb.index.outcome':'success',
          'nkb.content.index_input':body,'nkb.content.index_input_original_bytes':len(body.encode()),
          'nkb.content.index_input_observed_bytes':len(body.encode()),'nkb.content.index_input_truncated':False,
          'nkb.content.index_input_incomplete':False}.items()]}]}]}]}
    return trace,payload


def exporter_queue_metrics(text, exporter):
    totals={'failed':0.0,'size':0.0}
    for line in text.splitlines():
        # The closing quote keeps the -kb exporter out of the n-agent branch.
        if f'exporter="{exporter}"' not in line: continue
        if line.strip().startswith('otelcol_exporter_enqueue_failed_spans'):
            totals['failed']+=float(line.rsplit(' ',1)[1])
        elif line.strip().startswith('otelcol_exporter_queue_size'):
            totals['size']+=float(line.rsplit(' ',1)[1])
    return totals


def queue_metrics(text):
    return exporter_queue_metrics(text,'otlphttp/langfuse')


def kb_queue_metrics(text):
    return exporter_queue_metrics(text,'otlphttp/langfuse-kb')


def failure_metrics(text):
    totals={'langfuse':0.0,'kb':0.0,'original':0.0}
    for line in text.splitlines():
        if not line.strip().startswith('otelcol_exporter_send_failed_'): continue
        if 'exporter="otlphttp/langfuse-kb"' in line: target='kb'
        elif 'exporter="otlphttp/langfuse"' in line: target='langfuse'
        else: target='original'
        totals[target]+=float(line.rsplit(' ',1)[1])
    return totals


def expected_usage(attributes):
    mapping={'input':'gen_ai.usage.input_tokens','output':'gen_ai.usage.output_tokens',
             'cache_read':'nagent.model.cache_read_tokens','cache_write':'nagent.model.cache_write_tokens'}
    usage={bucket:int(attributes[key]) for bucket,key in mapping.items() if key in attributes}
    if usage: usage['total']=sum(usage.values())
    return usage


def original_spans(tempo):
    def value(item):
        if 'arrayValue' in item: return [value(v) for v in item['arrayValue'].get('values',[])]
        if 'intValue' in item: return int(item['intValue'])
        return next(iter(item.values()))
    def identifier(value):
        if not value: return None
        return value if re.fullmatch('[0-9a-f]{16}',value) else base64.b64decode(value).hex()
    spans={}
    for batch in tempo.get('batches',tempo.get('resourceSpans',[])):
        resource={a['key']:value(a['value']) for a in batch.get('resource',{}).get('attributes',[])}
        if resource.get('service.name') not in (None,'n-agent','n-kb'): continue
        for scope in batch.get('scopeSpans',batch.get('instrumentationLibrarySpans',[])):
            for span in scope.get('spans',[]):
                spans[identifier(span['spanId'])]={**span,'parent':identifier(span.get('parentSpanId')),
                    'values':{a['key']:value(a['value']) for a in span.get('attributes',[])}}
    return spans


def compare_backends(tempo,rows):
    spans=original_spans(tempo)
    assert set(spans)=={row['id'] for row in rows}
    for row in rows:
        original=spans[row['id']]; values=original['values']
        assert row['parentObservationId']==original['parent']
        for target,source in [('startTime','startTimeUnixNano'),('endTime','endTimeUnixNano')]:
            assert row[target]
            actual=datetime.fromisoformat(row[target].replace('Z','+00:00')).timestamp()
            assert abs(actual-int(original[source])/1e9)<.0011
        operation=values.get('gen_ai.operation.name')
        for direction,fields in [('input',['user_message','input_messages']),('output',['assistant_message','output_messages'])]:
            if operation=='execute_tool': fields.append('tool_arguments' if direction=='input' else 'tool_result')
            if values.get('nagent.knowledge.operation')=='search': fields.append('retrieval_query' if direction=='input' else 'retrieval_results')
            if values.get('nagent.task.operation')=='evaluate' and direction=='output': fields.append('judge_verdict')
            expected=None
            for field in fields:
                if 'nagent.content.'+field in values: expected=values['nagent.content.'+field]
            assert row.get(direction)==expected
        if row['type']=='GENERATION':
            assert row.get('usageDetails',{})==expected_usage(values)
            model=values.get('gen_ai.response.model',values.get('gen_ai.request.model'))
            if model is not None: assert row.get('model')==model
            parameters={key.removeprefix('nagent.model.parameter.'):v for key,v in values.items() if key.startswith('nagent.model.parameter.')}
            assert (row.get('modelParameters') or {})==parameters
            started=values.get('nagent.model.completion_start_time')
            actual=row.get('completionStartTime')
            if started:
                assert actual and abs(datetime.fromisoformat(actual.replace('Z','+00:00')).timestamp()-datetime.fromisoformat(started.replace('Z','+00:00')).timestamp())<.0011
            else: assert not actual
            if values.get('nagent.model.cost_status')=='estimated' and values.get('nagent.model.pricing_version'):
                assert 'total' in (row.get('costDetails') or {})
                assert abs(float((row.get('costDetails') or {})['total'])-values['nagent.model.cost_usd'])<1e-10
                assert row['metadata']['nagent.model.pricing_version']==values['nagent.model.pricing_version']
                assert row['metadata']['nagent.model.cost_status']=='estimated'


def assert_kb_projection(rows,remote,expected):
    """Project n-kb API observations: types, bodies, metadata, usage, root-only trace bodies."""
    by_id={row['id']:row for row in rows}
    assert len(rows)==12 and set(by_id)=={f'{i:016x}' for i in range(1,13)}
    def envelope(row,key):
        value=row['metadata'][key]
        return json.loads(value) if isinstance(value,str) else value
    root=by_id[f'{1:016x}']
    assert root['type']=='CHAIN' and root['name']=='index.run'
    assert root['input']==expected['index_input'] and root['output']==expected['index_result']
    assert root['level']!='ERROR'
    meta=root['metadata']
    assert meta['nkb.index.operation']=='run' and meta['nkb.index.outcome']=='success'
    assert meta['nkb.index.duration_ms']==12.5 and meta['nkb.index.chunk_count']==7
    assert meta['nkb.index.document_id']=='doc-e2e' and meta['nkb.index.job_id']=='job-e2e'
    assert envelope(root,'input')=={'source':'index_input','original_bytes':64,'observed_bytes':32,'truncated':True,'incomplete':True}
    assert envelope(root,'output')['source']=='index_result'
    retriever=by_id[f'{2:016x}']
    assert retriever['type']=='RETRIEVER' and retriever['name']=='retrieval.search'
    assert retriever['input']==expected['retrieval_input'] and retriever['output']==expected['retrieval_output']
    assert retriever['metadata']['nkb.retrieval.result_count']==2
    assert retriever['level']!='ERROR'
    embedding=by_id[f'{3:016x}']
    assert embedding['type']=='EMBEDDING' and embedding['name']=='embeddings'
    assert embedding['input']==expected['embedding_input'] and embedding.get('output') is None
    assert embedding['usageDetails']=={'input':5,'total':5}
    assert embedding['model']==expected['model']
    # T5 成本组：estimated 组投影 cost_details 单 input 分项与受控 metadata。
    assert embedding['costDetails']=={'input':0.125,'total':0.125}
    for key,value in (('nkb.model.cost_status','estimated'),('nkb.model.cost_currency','USD'),
                      ('nkb.model.pricing_version','e2e-kb-v1'),('nkb.model.price_id','e2e-embed-price'),
                      ('nkb.model.billing_provider','ollama'),('nkb.model.billing_model','e2e-embed-model')):
        assert embedding['metadata'][key]==value
    assert 'nkb.model.cost_usd' not in embedding['metadata']
    zero=by_id[f'{4:016x}']
    assert zero['usageDetails']=={'input':0,'total':0}
    assert zero['costDetails']=={'input':0,'total':0}
    unknown=by_id[f'{5:016x}']
    assert not unknown.get('usageDetails') and unknown.get('input') is None
    # unknown 组阻断自动推价：model 不投影，受控 reason 进 metadata。
    assert unknown.get('model') is None
    assert unknown['metadata']['nkb.model.cost_status']=='unknown'
    assert unknown['metadata']['nkb.model.cost_reason']=='missing_usage'
    for i in (6,7,8):
        row=by_id[f'{i:016x}']
        assert row['type']=='SPAN' and row['name']=='span'
        assert row.get('input') is None and row.get('output') is None
    nested=by_id[f'{9:016x}']
    assert nested['type']=='CHAIN'
    assert nested['input']==expected['nested_input'] and nested['output']==expected['nested_result']
    illegal=by_id[f'{10:016x}']
    assert illegal['type']=='SPAN'
    for key in ('nkb.index.chunk_count','error.type','nkb.model.usage_known','nkb.index.duration_ms','nkb.index.outcome'):
        assert key not in illegal['metadata']
    failure=by_id[f'{11:016x}']
    assert failure['type']=='RETRIEVER' and failure['level']=='ERROR' and failure['statusMessage']=='timeout'
    assert failure.get('output') is None
    childless=by_id[f'{12:016x}']
    assert childless['type']=='CHAIN' and childless.get('input') is None and childless.get('output') is None
    only=remote[0]
    assert only['type']=='CHAIN' and only['input']==expected['remote_input'] and only['output']==expected['remote_result']
    assert only['parentObservationId']=='1234567890abcdef'
    for row in rows+remote: assert not row.get('sessionId')
    for sentinel in expected['sentinels']:
        assert sentinel not in json.dumps(rows+remote)


def assert_kb_trace_bodies(documents,expected):
    """Trace bodies exist only for the global-root index.run; the remote-parent run stays observation-level."""
    assert documents['trace']['input']==expected['index_input'] and documents['trace']['output']==expected['index_result']
    assert documents['remote'].get('input') is None and documents['remote'].get('output') is None


def assert_shared_project_purity(agent_rows,kb_rows,remote_rows,agent_expected,kb_expected):
    """One shared project: each projected trace carries only its own service's data."""
    for sentinel in kb_expected['sentinels']:
        assert sentinel not in json.dumps(agent_rows)
    for sentinel in agent_expected['sentinels']:
        assert sentinel not in json.dumps(kb_rows+remote_rows)


def shared_trace_payload():
    """Cross-service trace correlation: one trace id, an n-agent root and an n-kb child.

    Both pipelines share the Langfuse project, so the two projected spans must
    land in one trace tree; the KB projection keeps parent/trace ids untouched
    and only the n-agent root writes the trace-level body.
    """
    trace=uuid.uuid4().hex; now=time.time_ns()
    agent={'traceId':trace,'spanId':'a'*16,'name':'chat','startTimeUnixNano':str(now),
        'endTimeUnixNano':str(now+1000000),
        'attributes':[attr('gen_ai.operation.name','chat'),attr('nagent.content.input_messages','agent parent input'),
            attr('gen_ai.conversation.id','e2e-langfuse-'+trace)]}
    child={'traceId':trace,'spanId':'b'*16,'parentSpanId':'a'*16,'name':'retrieval.search','startTimeUnixNano':str(now+1),
        'endTimeUnixNano':str(now+1000001),
        'attributes':[attr('nkb.retrieval.operation','search'),attr('nkb.retrieval.outcome','success'),
            attr('nkb.content.retrieval_input','kb child input')]}
    payload={'resourceSpans':[
        {'resource':{'attributes':[attr('service.name','n-agent'),attr('service.version','e2e-release')]},
         'scopeSpans':[{'spans':[agent]}]},
        {'resource':{'attributes':[attr('service.name','n-kb'),attr('service.version','e2e-kb-release')]},
         'scopeSpans':[{'spans':[child]}]}]}
    return trace,payload


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length',0)))
            time.sleep(20)
            try:
                self.send_response(503); self.end_headers()
            except (BrokenPipeError,ConnectionResetError): pass
        def log_message(self,*args): pass
    ThreadingHTTPServer(('0.0.0.0',9988),Slow).serve_forever()


def history_identity(kind, data):
    def canonical(value):
        if isinstance(value,dict):
            return {key:(sorted((canonical(x) for x in item),key=lambda x:json.dumps(x,sort_keys=True))
                         if key=='attributes' and isinstance(item,list) else canonical(item)) for key,item in value.items()}
        if isinstance(value,list): return [canonical(x) for x in value]
        return value
    if kind=='trace':
        spans=[]
        for batch in data.get('batches',data.get('resourceSpans',[])):
            for scope in batch.get('scopeSpans',batch.get('instrumentationLibrarySpans',[])):
                for span in scope.get('spans',[]):
                    spans.append({'resource':canonical(batch.get('resource',{})),
                                  'scope':canonical(scope.get('scope',scope.get('instrumentationLibrary',{}))),
                                  'span':canonical(span)})
        assert spans
        return sorted(spans,key=lambda x:x['span']['spanId'])
    if kind=='profile':
        flame=data['flamebearer']
        assert flame['numTicks']>0
        return {'metadata':data['metadata'],'timeline':data['timeline'],
                'numTicks':flame['numTicks'],'names':sorted(flame['names'])}
    return sorted(data['data']['result'],key=lambda item:json.dumps(item,sort_keys=True))


class Runner:
    def __init__(self, directory, run_id, plan=True):
        credentials={}
        for line in Path('/credentials').read_text().splitlines():
            key,sep,value=line.partition('=')
            if sep: credentials[key]=value.strip('"\'')
        # Single shared Langfuse project: one Basic credential reads both services' spans.
        auth=base64.b64encode((credentials['LANGFUSE_INIT_PROJECT_PUBLIC_KEY']+':'+credentials['LANGFUSE_INIT_PROJECT_SECRET_KEY']).encode()).decode()
        self.get=lambda url:request(url,headers={'Authorization':'Basic '+auth})
        self.app='http://n-agent-n-agent-1:8000'
        # App port is resolved by shell from its running configuration.
        self.app=os.environ.get('LANGFUSE_APP_URL', self.app)
        status=request(self.app+'/chat/observations/otel')
        self.instance_id=status['instance_id']
        self.report=Report(directory,run_id,status['links']['Trace'])
        self.manifest=SessionManifest(directory,run_id)
        if plan: self.report.plan(['synthetic','kb-synthetic','business','sampling','concurrent','history','fault-web','fault-web-kb','kb-recovered','fault-auth','fault-slow','saturation','fault-queue','fault-queue-kb','persisted','sessions-cleanup','trace-audit','cleanup','environment-cleanup','orchestration'])
        self.trace=None

    def check(self,key,fn):
        self.trace=None; self.confirmed_trace=None; self.stage='start'
        try:
            evidence=fn()
            if self.confirmed_trace and isinstance(evidence,dict): evidence['tempo_trace_verified']=True
            self.report.record(key,'PASS',self.confirmed_trace,evidence=evidence)
            print(key+': PASS',flush=True)
            return True
        except Exception as error:
            self.report.record(key,'FAIL',self.confirmed_trace,reason=safe_error(error)+'; '+self.stage, evidence={'requested_trace_id':self.trace})
            print(key+': FAIL ('+safe_error(error)+')',flush=True)
            return False

    def tempo(self, trace):
        return request('http://tempo:3200/api/traces/'+trace,headers={'Accept':'application/json'})

    def synthetic(self):
        trace,payload,expected=synthetic_payload(); self.trace=trace
        request('http://otel-collector:4318/v1/traces',payload)
        rows=poll(lambda: (r if len(r:=observations(self.get,trace))==expected['observation_count'] else None))
        self.confirmed_trace=trace
        by_id={r['id']:r for r in rows}
        assert by_id['0000000000000001']['type']=='CHAIN'
        assert by_id['0000000000000002']['type']=='GENERATION'
        assert by_id['0000000000000003']['type']=='TOOL'
        assert by_id['0000000000000004']['type']=='SPAN'
        assert by_id['0000000000000009']['type']=='AGENT'
        assert by_id['000000000000000a']['type']=='RETRIEVER'
        assert by_id['000000000000000b']['type']=='EVALUATOR'
        assert by_id['000000000000000c']['type']=='EVALUATOR'
        assert by_id['000000000000000c']['metadata']['nagent.task.achieved'] is False
        assert by_id['000000000000000c'].get('input') is None
        assert by_id['000000000000000c']['output']=='false'
        reason=by_id['000000000000000c']['metadata']['judge_reason']
        if isinstance(reason,str): reason=json.loads(reason)
        assert reason['content']=='goal missing'
        assert by_id['000000000000000c']['level']!='ERROR'
        assert by_id['000000000000000d']['type']=='RETRIEVER'
        for i in range(14,19): assert by_id[f'{i:016x}']['type']=='CHAIN'
        for row in rows:
            assert row['sessionId']=='e2e-langfuse-'+trace
            if row['id']!='0000000000000001': assert row['parentObservationId']=='0000000000000001'
            assert row['metadata']['nagent.agent.run_id']=='run-'+trace
            assert row['metadata']['links']==[{'trace_id':'a'*32,'span_id':'b'*16,'flags':0}]
            assert row['release']=='e2e-release' and row['environment']=='e2e'
        chat=by_id['0000000000000002']
        assert chat['input']=='short exact input' and chat['output']=='short exact output'
        assert chat['model']=='e2e-model'
        assert chat['usageDetails']=={'input':3,'output':0,'cache_read':2,'cache_write':1,'total':6}
        assert chat['modelParameters']=={'temperature':.25,'presence_penalty':-1}
        assert chat['costDetails']['total']==.125 and chat['completionStartTime']
        assert chat['metadata']['gen_ai.response.finish_reasons']==['stop']
        for field,body in [('system_prompt','safe policy'),('tool_definitions','safe tool schema')]:
            envelope=chat['metadata'][field]
            if isinstance(envelope,str): envelope=json.loads(envelope)
            assert envelope['content']==body
        for direction,source in [('input','tool_arguments'),('output','tool_result')]:
            envelope=by_id['0000000000000003']['metadata'][direction]
            if isinstance(envelope,str): envelope=json.loads(envelope)
            assert envelope=={'source':source}, 'lower-priority completeness leaked'
        assert by_id['0000000000000003']['metadata']['nagent.tool.executed'] is False
        assert by_id['0000000000000003']['metadata']['nagent.tool.policy_outcome']=='deny'
        assert by_id['0000000000000003']['metadata']['nagent.tool.outcome']=='denied'
        assert by_id['0000000000000005']['metadata']['error.type']=='ValueError'
        assert by_id['0000000000000005']['metadata']['nagent.tool.outcome']=='failure'
        assert by_id['0000000000000005']['level']=='ERROR'
        assert by_id['0000000000000006']['metadata']['nagent.model.outcome']=='cancelled'
        assert by_id['0000000000000007']['metadata']['error.type']=='TimeoutError'
        assert by_id['0000000000000007']['metadata']['nagent.model.outcome']=='timed_out'
        assert by_id['0000000000000007']['level']=='ERROR'
        for i in (4,5,6): assert not by_id[f'{i+1:016x}'].get('usageDetails')
        large=by_id['0000000000000008']
        assert large['input']==expected['long_input'] and large['output']==expected['long_output']
        for direction,original in [('input',1024010),('output',1024020)]:
            metadata=large['metadata'][direction]
            if isinstance(metadata,str): metadata=json.loads(metadata)
            assert metadata['original_bytes']==original and metadata['truncated'] is True and metadata['incomplete'] is True
        raw=json.dumps(rows)
        for sentinel in expected['sentinels']: assert sentinel not in raw
        tempo=poll(lambda:self.tempo(trace))
        for sentinel in expected['sentinels']: assert sentinel in json.dumps(tempo)
        assert 'e2e-excluded-service-sentinel' in json.dumps(tempo)
        assert not any(row['id']=='ffffffffffffffff' for row in rows)
        compare_backends(tempo,rows)
        return {'observations':len(rows),'input_bytes':len(large['input'].encode()),'output_bytes':len(large['output'].encode()),'sentinels':9,'original_tempo_preserved':True,'new_types':'synthetic','atomic_completeness':True,'resource_order':True}

    def business(self, delivery=True, sampled=True):
        trace=uuid.uuid4().hex; self.trace=trace
        session='e2e-langfuse-'+trace
        self.manifest.add(session,'api')
        headers={'X-Session-ID':session,'traceparent':f'00-{trace}-1234567890abcdef-0{int(sampled)}','Content-Type':'application/json'}
        baseline=self.metric_count()
        body=json.dumps({'model':'N-Agent','messages':[{'role':'user','content':'请必须调用 calculator 工具计算 17*19，然后仅回答计算结果。'}],'stream':True,'temperature':0.25},ensure_ascii=False).encode()
        with urlopen(Request(self.app+'/v1/chat/completions',data=body,headers=headers),timeout=150) as response: text=response.read().decode()
        parts=[]
        for line in text.splitlines():
            if line.startswith('data: ') and line[6:]!='[DONE]':
                item=json.loads(line[6:])
                for choice in item.get('choices',[]): parts.append(choice.get('delta',{}).get('content') or '')
        output=''.join(parts); self.stage='real provider output'
        assert '323' in output, 'real active provider did not produce calculator result'
        if not sampled:
            time.sleep(8)
            assert not observations(self.get,trace)
            with urlopen('http://tempo:3200/ready',timeout=5) as response: assert response.status==200
            try: self.tempo(trace)
            except HTTPError as error:
                assert error.code==404
            else: raise AssertionError('unsampled real request exported')
            return {'session':session,'output':output,'unsampled_parent':True}
        self.stage='Tempo delivery'
        def complete_tempo():
            value=self.tempo(trace)
            serialized=json.dumps(value)
            return value if all(operation in serialized for operation in ('invoke_agent','execute_tool','chat')) else None
        tempo=poll(complete_tempo); self.confirmed_trace=trace; serialized=json.dumps(tempo)
        assert 'invoke_agent' in serialized and 'execute_tool' in serialized and 'chat' in serialized
        # Prove all original shared exporters continue delivering the business run.
        self.stage='Loki delivery'
        poll(lambda: request('http://loki:3100/loki/api/v1/query_range?'+urlencode({'query':'{service_name="n-agent"} |= "'+trace+'"','start':str(time.time_ns()-300_000_000_000),'limit':10}))['data']['result'])
        self.stage='Prometheus delta'
        after=poll(lambda:(count if (count:=self.metric_count())>baseline else None))
        evidence={'trace_id':trace,'session':session,'output':output,'tempo':True,'loki':True,'prometheus_run_delta':after-baseline}
        if delivery:
            self.stage='Langfuse complete business trace'
            def complete():
                nonlocal tempo
                rows=observations(self.get,trace)
                kinds={r['type'] for r in rows}
                ready=({'AGENT','GENERATION','TOOL'}<=kinds and
                       any(r.get('usageDetails') for r in rows if r['type']=='GENERATION') and
                       any('323' in str(r.get('output')) for r in rows))
                if ready:
                    tempo=self.tempo(trace)
                    try: compare_backends(tempo,rows)
                    except AssertionError: return None
                return rows if ready else None
            rows=poll(complete)
            assert {'AGENT','GENERATION','TOOL'} <= {r['type'] for r in rows}
            generations=[r for r in rows if r['type']=='GENERATION']
            assert any(r.get('usageDetails') for r in generations)
            assert any('323' in str(r.get('output')) for r in rows)
            compare_backends(tempo,rows)
            evidence['usage']=[r.get('usageDetails') for r in generations]
            evidence['usage_matches_original_tempo']=True
            evidence['same_span_ids_parents_io_times']=True
            evidence['model_parameters']=[r.get('modelParameters') for r in generations]
            evidence['completion_start_times']=[r.get('completionStartTime') for r in generations]
            evidence['cost_details']=[r.get('costDetails') for r in generations]
            evidence['retriever_judge_business']='未验证：此 calculator 业务场景不触发检索或 Goal Mode Judge；专用边界由真实 SDK 单测及合成后端链路验证'
        # Only our owned successful session is eligible for cleanup; failed sessions survive.
        self.stage='owned session cleanup'
        request(self.app+'/chat/sessions/'+session,method='DELETE')
        return evidence

    def metric_count(self):
        query='sum(nagent_agent_runs_total{service_instance_id='+json.dumps(self.instance_id)+'})'
        rows=request('http://prometheus:9090/api/v1/query?'+urlencode({'query':query}))['data']['result']
        return sum(float(row['value'][1]) for row in rows)

    def collector_kb_queue(self):
        with urlopen('http://otel-collector:8888/metrics',timeout=5) as response:
            return kb_queue_metrics(response.read().decode())

    def kb_synthetic(self):
        """Real fork, both pipelines, one combined POST: projection, shared-project merge, baselines."""
        agent_trace,agent_payload,agent_expected=synthetic_payload()
        kb_payload,kb_expected=kb_synthetic_payload()
        merge_trace,merge_payload=shared_trace_payload()
        request('http://otel-collector:4318/v1/traces',
                {'resourceSpans':agent_payload['resourceSpans']+kb_payload['resourceSpans']+merge_payload['resourceSpans']})
        self.trace=[agent_trace,kb_expected['trace'],kb_expected['remote'],merge_trace]
        def counted(trace,count):
            return poll(lambda:(rows if len(rows:=observations(self.get,trace))==count else None))
        rows=counted(kb_expected['trace'],kb_expected['counts'][kb_expected['trace']])
        remote=counted(kb_expected['remote'],kb_expected['counts'][kb_expected['remote']])
        assert_kb_projection(rows,remote,kb_expected)
        # Langfuse v4 events_only has no single-trace read API; the trace-body
        # projection is proven field-exact by the real-fork isolated-IO test.
        # The live evidence for root gating is the projected traceName.
        assert next(row for row in rows if row['id']=='0000000000000001')['traceName']=='index.run'
        assert remote[0]['traceName'] is None
        agent_rows=counted(agent_trace,agent_expected['observation_count'])
        assert not any(row['id']=='ffffffffffffffff' for row in agent_rows+rows+remote)
        ids={row['id'] for row in agent_rows}
        def complete_tempo():
            document=self.tempo(agent_trace)
            return document if ids<=set(original_spans(document)) else None
        tempo=poll(complete_tempo)
        # The n-agent output baseline is structurally re-proven with KB spans inside the same collector.
        compare_backends(tempo,agent_rows)
        # Cross-service trace correlation: a shared trace id merges the n-agent
        # root and the n-kb child into one tree inside the single project.
        merge_rows=counted(merge_trace,2)
        merge={row['id']:row for row in merge_rows}
        agent_root=merge['a'*16]; kb_child=merge['b'*16]
        assert agent_root['type']=='GENERATION' and agent_root['name']=='chat'
        assert agent_root['parentObservationId'] is None
        assert agent_root['sessionId']=='e2e-langfuse-'+merge_trace
        assert agent_root['traceName']=='chat'
        assert agent_root['input']=='agent parent input'
        assert kb_child['type']=='RETRIEVER' and kb_child['name']=='retrieval.search'
        assert kb_child['parentObservationId']=='a'*16
        assert not kb_child.get('sessionId') and not kb_child.get('traceName')
        assert kb_child['input']=='kb child input'
        self.confirmed_trace=[agent_trace,kb_expected['trace'],kb_expected['remote'],merge_trace]
        assert_shared_project_purity(agent_rows,rows,remote,agent_expected,kb_expected)
        # The four-signal branch keeps every raw KB span, sentinels included.
        poll(lambda:self.tempo(kb_expected['trace']),seconds=60)
        poll(lambda:self.tempo(kb_expected['remote']),seconds=60)
        serialized=json.dumps(self.tempo(kb_expected['trace']))
        assert 'nkb.retrieval.operation' in serialized and 'nkb.index.operation' in serialized
        for sentinel in kb_expected['sentinels']: assert sentinel in serialized
        return {'kb_observations':len(rows)+len(remote),'agent_observations':len(agent_rows),
                'agent_baseline':'compare_backends PASS with KB spans in the same collector',
                'shared_project_merge':{'trace_id':merge_trace,'agent_root':'a'*16,'kb_child':'b'*16},
                'tempo_keeps_raw':True,
                'trace_root_bodies_only':True,'remote_parent_never_root':True}

    def fault_web_kb(self):
        """Langfuse down: the KB exporter fails while Tempo still receives the KB trace."""
        baseline=self.collector_failures()
        trace,payload=kb_probe_payload(); self.trace=trace
        request('http://otel-collector:4318/v1/traces',payload)
        after=poll(lambda:(value if (value:=self.collector_failures())['kb']>baseline['kb'] else None),seconds=90)
        assert after['original']==baseline['original']
        poll(lambda:self.tempo(trace),seconds=60)
        self.confirmed_trace=trace
        return {'trace_id':trace,'exporter_failures_after':after,'tempo_received':True}

    def kb_recovered(self):
        """After recovery a fresh KB probe delivers complete results again."""
        trace,payload=kb_probe_payload(); self.trace=trace
        request('http://otel-collector:4318/v1/traces',payload)
        rows=poll(lambda:(rows if (rows:=observations(self.get,trace)) else None),seconds=120)
        self.confirmed_trace=trace
        row=rows[0]
        assert row['type']=='CHAIN' and row['input']=='kb fault probe'
        assert row['output'] is None
        return {'trace_id':trace,'delivered_after_recovery':True,'result_unchanged':True}

    def fault_queue_kb(self):
        """KB exporter queue saturation drops overflow without blocking; Tempo still receives."""
        baseline=self.collector_kb_queue(); failures=self.collector_failures()
        peak=baseline['size']; traces=[]
        for _ in range(32):
            trace,payload=kb_probe_payload('q'*1000000)
            request('http://otel-collector:4318/v1/traces',payload,timeout=5)
            traces.append(trace)
            current=self.collector_kb_queue(); peak=max(peak,current['size'])
            if current['failed']>baseline['failed']: break
            time.sleep(.1)
        current=poll(lambda:(value if (value:=self.collector_kb_queue())['failed']>baseline['failed'] else None),seconds=15)
        assert peak<=16777216
        poll(lambda:self.tempo(traces[0]),seconds=60)
        after=self.collector_failures()
        assert after['langfuse']==failures['langfuse'] and after['original']==failures['original']
        self.confirmed_trace=traces[0]
        return {'kb_enqueue_failed_delta':current['failed']-baseline['failed'],'peak_queue_bytes':peak,
                'queue_capacity_bytes':16777216,'kb_traces_sent':len(traces),'tempo_canary':traces[0],
                'other_exporters_unaffected':True}

    def collector_failures(self):
        with urlopen('http://otel-collector:8888/metrics',timeout=5) as response:
            return failure_metrics(response.read().decode())

    def collector_queue(self):
        with urlopen('http://otel-collector:8888/metrics',timeout=5) as response:
            return queue_metrics(response.read().decode())

    def fault(self):
        baseline=self.collector_failures()
        evidence=self.business(delivery=False)
        after=poll(lambda: (value if (value:=self.collector_failures())['langfuse']>baseline['langfuse'] else None),seconds=90)
        assert after['original']==baseline['original']
        return {**evidence,'exporter_failures_before':baseline,'exporter_failures_after':after}

    def fault_queue(self):
        baseline=self.collector_queue(); failures=self.collector_failures()
        stop=threading.Event(); samples=[]; errors=[]
        def fill():
            for _ in range(128):
                if stop.is_set(): break
                trace=uuid.uuid4().hex; now=time.time_ns()
                payload={'resourceSpans':[{'resource':{'attributes':[attr('service.name','n-agent')]},
                    'scopeSpans':[{'spans':[{'traceId':trace,'spanId':'1234567890abcdef','name':'queue-pressure',
                      'startTimeUnixNano':str(now),'endTimeUnixNano':str(now+1000),
                      'attributes':[attr('gen_ai.operation.name','chat'),attr('nagent.content.input_messages','q'*32768)]}]}]}]}
                try:
                    request('http://otel-collector:4318/v1/traces',payload,timeout=5)
                    samples.append(self.collector_queue())
                except Exception as error: errors.append(safe_error(error)); break
                if stop.wait(.5): break
        thread=threading.Thread(target=fill,daemon=True); thread.start()
        try: evidence=self.business(delivery=False)
        finally: stop.set(); thread.join(6)
        self.stage='queue pressure during real business'
        assert not thread.is_alive() and not errors and samples
        final=self.collector_queue(); after=self.collector_failures()
        assert final['failed']>baseline['failed']
        assert after['original']==failures['original']
        return {**evidence,'queue_before':baseline,'queue_after':final,
                'peak_queue_bytes':max(row['size'] for row in samples),
                'small_pressure_total_bytes':len(samples)*32768,'samples_during_business':len(samples),
                'exporter_failures_before':failures,'exporter_failures_after':after}

    def concurrent(self):
        def run(_):
            child=Runner(self.report.directory,self.report.data['run_id'],plan=False)
            child.confirmed_trace=None
            try: return {'result':'PASS','evidence':child.business(),'trace':child.confirmed_trace}
            except Exception as error: return {'result':'FAIL','error':safe_error(error),'stage':getattr(child,'stage','unknown'),'trace':child.confirmed_trace}
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(run,range(2)))
        self.trace=self.confirmed_trace=[result['trace'] for result in results if result['trace']]
        self.stage=json.dumps([{k:v for k,v in result.items() if k in ('error','stage')} for result in results])
        assert all(result['result']=='PASS' for result in results)
        return {'runs':results}

    def history(self):
        manifest=json.loads(Path('/history/langfuse-history-baseline.json').read_text())
        evidence={}
        for key,path in manifest['baseline_files'].items():
            old=json.loads((Path('/history')/Path(path).name).read_text())
            url=manifest[key+'_url']; parsed=urlsplit(url)
            host={'3200':'tempo:3200','13000':'grafana:3000','4040':'pyroscope:4040'}[str(parsed.port)]
            current=request(urlunsplit(('http',host,parsed.path,parsed.query,'')))
            self.stage='fixed history '+key
            assert history_identity(key,current)==history_identity(key,old)
            assert current
            evidence[key]='fixed sample unchanged'
        return evidence

    def saturation(self):
        def metrics():
            with urlopen('http://otel-collector:8888/metrics',timeout=5) as response:
                return queue_metrics(response.read().decode())
        baseline=metrics(); peak=baseline['size']
        # Each request is one span, bounded to 1 MiB; total load <= 32 MiB.
        for i in range(32):
            trace,payload,expected=synthetic_payload()
            span=payload['resourceSpans'][0]['scopeSpans'][0]['spans'][7]
            span.pop('parentSpanId',None)
            span['attributes']=[a for a in span['attributes'] if a['key']!='nagent.content.output_messages']
            payload['resourceSpans'][0]['scopeSpans'][0]['spans']=[span]
            request('http://otel-collector:4318/v1/traces',payload)
            current=metrics(); peak=max(peak,current['size'])
            if current['failed']>baseline['failed']: break
            time.sleep(.1)
        current=poll(lambda: (value if (value:=metrics())['failed']>baseline['failed'] else None),seconds=15)
        assert peak<=16777216
        return {'enqueue_failed_delta':current['failed']-baseline['failed'],'peak_queue_bytes':peak,'queue_capacity_bytes':16777216,'max_requests':32}

    def persisted(self):
        final,_=reporting.split_attempts(self.report.data['cases'])
        original=next(r for r in final if r['id']=='synthetic' and r['result']=='PASS')
        trace=original['trace_id']; count=original['evidence']['observations']
        self.trace=trace
        assert len(observations(self.get,trace))==count
        self.confirmed_trace=trace
        return {'observations':count,'restart_persistence':True}

    def audit(self):
        verified={}
        for row in self.report.data['cases']:
            trace=row.get('trace_id')
            if not trace: continue
            missing=row['id']=='sampling'
            if not missing:
                if row.get('evidence',{}).get('tempo_trace_verified'): continue
                # Only the first implementation attached unconfirmed IDs to
                # HTTP validation failures. Later records attach a Trace only
                # after actual Tempo delivery. Do not rescan every historical
                # multi-MiB Trace merely to re-prove those recorded assertions.
                if not (row.get('attempt')==1 and row.get('reason','').startswith('HTTPError')):
                    continue
                if trace not in verified:
                    def fetch():
                        try: self.tempo(trace); return 'present'
                        except HTTPError as error:
                            if error.code==404: return 'absent'
                            raise
                    verified[trace]=poll(fetch,seconds=20)
                    time.sleep(1)
                missing=verified[trace]=='absent'
            if missing:
                row.setdefault('evidence',{})['requested_trace_id']=trace
                row['trace_id']=None
                row['trace_url']=None
                row['reason']+='；未采样或Tempo确认未取得该Trace'
            else:
                row['trace_url']=reporting.trace_url(self.report.base_url,trace)
                row.setdefault('evidence',{})['tempo_trace_verified']=True
        self.report.save()
        return {'verified_unique_traces':len(verified)}

    def cleanup_sessions(self):
        # Migrate only exact historical IDs from this report's business records.
        # Never enumerate all sessions or delete by prefix alone.
        existing={entry['session_id'] for entry in self.manifest.entries()}
        for row in self.report.data['cases']:
            if row['id'] not in ('business','sampling','concurrent','fault-web','fault-auth','fault-slow','fault-queue'): continue
            evidence=row.get('evidence',{})
            ids=evidence.get('requested_trace_id') or row.get('trace_id') or []
            for trace in ids if isinstance(ids,list) else [ids]:
                if not isinstance(trace,str) or not re.fullmatch('[0-9a-f]{32}',trace): continue
                session='e2e-langfuse-'+trace
                if session in existing: continue
                source='dashboard' if row.get('attempt')==1 and row.get('reason','').startswith('HTTPError') else 'api'
                self.manifest.add(session,source); existing.add(session)
        removed=missing=0
        for entry in {entry['session_id']:entry for entry in self.manifest.entries()}.values():
            session=entry['session_id']
            detail=request(self.app+'/chat/sessions/'+session)
            value=detail.get('session')
            if value is None: missing+=1; continue
            assert self.manifest.owns(session,value['source'])
            for attempt in range(4):
                try:
                    request(self.app+'/chat/sessions/'+session,method='DELETE')
                    removed+=1; break
                except HTTPError as error:
                    if error.code!=409 or attempt==3: raise
                    time.sleep(2)
        return {'owned_removed':removed,'already_absent':missing,'manifest':'owned-sessions.jsonl'}


def main():
    if not Path('/.dockerenv').exists(): raise SystemExit('E2E must run inside Docker')
    if sys.argv[1:] == ['serve']: return serve()
    directory,run_id,action=sys.argv[1:4]
    runner=Runner(directory,run_id)
    actions={'synthetic':runner.synthetic,'kb-synthetic':runner.kb_synthetic,'business':runner.business,
      'sampling':lambda:runner.business(sampled=False),
      'saturation':runner.saturation,'history':runner.history,'persisted':runner.persisted,'concurrent':runner.concurrent,
      'kb-recovered':runner.kb_recovered,'sessions-cleanup':runner.cleanup_sessions}
    success=True
    if action=='fault-queue': success=runner.check(action,runner.fault_queue)
    elif action=='fault-queue-kb': success=runner.check(action,runner.fault_queue_kb)
    elif action=='fault-web-kb': success=runner.check(action,runner.fault_web_kb)
    elif action.startswith('fault-'): success=runner.check(action,runner.fault)
    elif action=='cleanup': runner.report.record('cleanup',sys.argv[4],reason='Docker deployment restoration')
    elif action=='orchestration': runner.report.record('orchestration',sys.argv[4],reason='专用Docker测试阶段编排完成')
    elif action=='environment-cleanup':
        runner.report.record('environment-cleanup',sys.argv[4],reason='原业务部署与后台写开关恢复')
        if len(sys.argv)>5 and sys.argv[5]!='PASS':
            previous=[row for row in runner.report.data['cases'] if row['id']=='cleanup']
            if not previous or previous[-1]['result']!='FAIL':
                runner.report.record('cleanup','FAIL',reason='测试容器删除或内层清理失败')
        print(reporting.render_summary(runner.report.data['cases']))
        print('RESULT_FILE '+str(Path(directory)/'result.md'))
    elif action=='summary': print(reporting.render_summary(runner.report.data['cases']))
    elif action=='audit': success=runner.check('trace-audit',runner.audit)
    else: success=runner.check(action,actions[action])
    return 0 if success else 1

if __name__=='__main__': sys.exit(main())
