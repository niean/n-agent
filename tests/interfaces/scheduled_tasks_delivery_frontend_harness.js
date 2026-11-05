'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const staticDir = path.join(__dirname, '../../app/interfaces/http/static');
function node(tag) {
  return { tag, children: [], listeners: {}, value: '', style: {}, className: '', _text: '',
    set textContent(v) { this._text = String(v); this.children = []; },
    get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); },
    set innerHTML(v) { throw Error('unsafe HTML'); },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach(c => this.appendChild(c)); },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
    setAttribute() {}, focus() {},
    querySelector(s) { return all(this).find(c => s[0] === '.' ? c.className.split(' ').includes(s.slice(1)) : c.tag === s) || null; },
    addEventListener(e, f) { (this.listeners[e] ||= []).push(f); },
    async fire(e) { for (const f of this.listeners[e] || []) await f({target:this, preventDefault(){}}); },
  };
}
function all(n) { return [n, ...n.children.flatMap(all)]; }
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function env(extra = {}) {
  const body = node('body'); const root = node('div'); let saved;
  const api = { listScheduledTasks: async () => [],
    listGatewaySessions: async (_, p) => ({items:[], total:0, limit:p.limit, offset:p.offset}),
    getGatewaySession: async () => { throw Error('missing'); },
    updateScheduledTask: async (_, p) => { saved = p; }, createScheduledTask: async p => { saved = p; }, ...extra };
  const document = {body, createElement:node, getElementById: () => root};
  const ctx = {document, NAGENT:{api}, location:{pathname:'/scheduled-tasks'}, history:{pushState(){}}, requestAnimationFrame:f=>f(), addEventListener(){}};
  ctx.window = ctx; vm.runInNewContext(fs.readFileSync(path.join(staticDir,'scheduled-tasks.js'),'utf8'),ctx);
  return { body, root, ui:ctx.NAGENT.scheduledTasks, saved:()=>saved,
    field(label) { const l = all(body).find(c => c.tag === 'label' && c.children[0]._text === label); assert(l, label); return l.children[1]; },
    click: async label => {const b = all(body).find(c => c.tag === 'button' && c._text === label); assert(b,label); await b.fire('click'); await tick();},
    submit:async () => {await all(body).find(c=>c.tag==='form').fire('submit'); await tick();} };
}
const task = (delivery, extra={}) => ({id:'t', name:'name', prompt:'prompt', cron_expression:'* * * * *', timezone:'Asia/Shanghai', allowed_tools:['calculator'], session_id:'s', delivery, ...extra});
const row = (id, available=true) => ({id, delivery_label:'聊天 '+id, delivery_available:available, delivery_unavailable_reason:available?null:'platform_unavailable', delivery_parent_chat:'chat-safe', thread_id:'thread'});
async function choose(e, id) {const f=e.field('飞书IM接收会话'); f.value=id; await f.fire('change'); await tick();}
const retentionHint='无论是否选择飞书接收会话，执行结果都会保留在定时任务对话中。';
function assertNoDeliveryNotes(e) {assert(!e.body.textContent.includes(retentionHint));assert(!/当前接收位置：|投递到父聊天|飞书线程会话将投递到其父聊天|原接收会话尚未确认|任务继续使用原来源会话执行/.test(e.body.textContent));assert(!all(e.body).some(c=>c.tag==='option'&&c.textContent==='仅保留定时任务对话'));}
(async () => {
  for (const d of [{kind:'dashboard'}, {kind:'silent'}, {kind:'feishu',legacy:true}, {kind:'wecom',label:'历史企业微信投递目标'}]) {
    const e=env(); e.ui.openTaskForm(task(d)); await tick();
    assertNoDeliveryNotes(e);
    assert(!/Dashboard 通知|仅保留定时任务对话|刷新接收会话|加载更多/.test(e.body.textContent));
    assert.equal(all(e.body).filter(c=>c.tag==='label'&&c.children[0]._text==='飞书IM接收会话').length,1);
    await e.submit(); assert(!('delivery_target' in e.saved()));
  }
  const dashboard=env();dashboard.ui.openTaskForm(task({kind:'dashboard',label:'Dashboard'}));await tick();assertNoDeliveryNotes(dashboard);assert(!dashboard.body.textContent.includes('Dashboard'));
  const dashboardDetail=env({getScheduledTask:async()=>task({kind:'dashboard',label:'Dashboard',available:true}),listScheduledTaskExecutions:async()=>[]});dashboardDetail.ui.openTaskDetail('t');await tick();assert(dashboardDetail.root.textContent.includes('不投递飞书IM'));assert(!dashboardDetail.root.textContent.includes('Dashboard'));
  const fresh=env(); fresh.ui.openTaskForm(); await tick(); assertNoDeliveryNotes(fresh); await fresh.submit(); assert.equal(fresh.saved().delivery_target,'silent');
  let calls=[];
  const e=env({listGatewaySessions:async(_,p)=>{calls.push(p.offset);return {items:p.offset?[row('good')]:[row('bad',false)],total:2,offset:p.offset,limit:20};}});
  e.ui.openTaskForm();await tick();assert.deepStrictEqual(calls,[0,1]);assert.deepStrictEqual(e.field('飞书IM接收会话').children.map(c=>c.value),['','good']);
  await choose(e,'good');await e.submit();assert.equal(e.saved().delivery_target,'feishu');assert.equal(e.saved().delivery_conversation_id,'good');
  for (const single of [async()=>row('saved',false),async()=>{throw Error('missing');}]) {
    const x=env({getGatewaySession:single});x.ui.openTaskForm(task({kind:'feishu',conversation_id:'saved',label:'原会话',available:false}));await tick();
    assert(!all(x.body).some(c=>c.tag==='option'&&c.value==='saved'));assert.equal(x.field('飞书IM接收会话').value,'__saved_unavailable__');
    await x.submit();assert(!('delivery_target' in x.saved()));
    x.ui.openTaskForm(task({kind:'feishu',conversation_id:'saved'}));await tick();await choose(x,'');await x.submit();assert.equal(x.saved().delivery_target,'silent');assert(!('delivery_conversation_id' in x.saved()));
  }
  const legacy=env();legacy.ui.openTaskForm(task({kind:'feishu',legacy:true,available:true,label:'默认通知会话'},{origin:{platform:'feishu'}}));await tick();assert.equal(legacy.field('飞书IM接收会话').value,'__saved_unavailable__');assert.equal(legacy.field('飞书IM接收会话').children.find(c=>c.value==='__saved_unavailable__').textContent,'请选择接收会话');assert(!legacy.body.textContent.includes('保留原配置'));assertNoDeliveryNotes(legacy);await legacy.submit();assert(!('delivery_target' in legacy.saved()));
  const empty=env({listGatewaySessions:async(_,p)=>({items:[row('bad',false)],total:1,limit:20,offset:p.offset})});empty.ui.openTaskForm();await tick();assert(empty.body.textContent.includes('暂无可用飞书会话'));
  const echo=env({getGatewaySession:async()=>({...row('saved'),delivery_chat_name:'报告群'})});echo.ui.openTaskForm(task({kind:'feishu',conversation_id:'saved'}));await tick();assert.equal(echo.field('飞书IM接收会话').value,'saved');assert(echo.body.textContent.includes('（报告群）'));assertNoDeliveryNotes(echo);
  for(const broken of ['duplicate','offset','total','empty']) {
    const x=env({listGatewaySessions:async(_,p)=>({items:p.offset?(broken==='empty'?[]:[row('a')]):[row('a')],total:p.offset&&broken==='total'?9:2,limit:20,offset:p.offset&&broken==='offset'?9:p.offset})});
    x.ui.openTaskForm(task({kind:'feishu',conversation_id:'original'}));await tick();assert(x.body.textContent.includes('刷新浏览器'));await x.submit();assert(!('delivery_target' in x.saved()));
  }
  const fail=env({listGatewaySessions:async(_,p)=>({items:[row('chosen')],total:1,limit:20,offset:p.offset}),updateScheduledTask:async()=>{throw Error('save_failed');}});fail.ui.openTaskForm(task({kind:'dashboard'}));await tick();
  for(const label of ['名称','Prompt','Cron 表达式','时区','允许的工具','Session ID']) {const f=fail.field(label);f.value='draft '+label;await f.fire('input');}
  await choose(fail,'chosen');await fail.submit();assert.equal(fail.field('飞书IM接收会话').value,'chosen');
  for(const label of ['名称','Prompt','Cron 表达式','时区','允许的工具','Session ID']) assert.equal(fail.field(label).value,'draft '+label);
  let resolve;const late=env({listGatewaySessions:()=>new Promise(r=>{resolve=r;})});late.ui.openTaskForm();const oldResolve=resolve;await late.click('取消');late.ui.openTaskForm();oldResolve({items:[row('late')],total:1,limit:20,offset:0});await tick();assert(!late.body.textContent.includes('聊天 late'));
  let resolveSingle;const lateSingle=env({getGatewaySession:()=>new Promise(r=>{resolveSingle=r;})});lateSingle.ui.openTaskForm(task({kind:'feishu',conversation_id:'old'}));await choose(lateSingle,'');resolveSingle(row('old'));await tick();assert(!lateSingle.body.textContent.includes('聊天 old'));
  const distinct=env({listGatewaySessions:async(_,p)=>({items:['alpha','beta'].map(id=>({...row(id),delivery_label:'飞书聊天',delivery_parent_chat:'chat-opaque-'+id,delivery_chat_name:'群 <img onerror=evil()>',receive_id:'RAW'})),total:2,limit:20,offset:p.offset})});distinct.ui.openTaskForm();await tick();
  const labels=distinct.field('飞书IM接收会话').children.filter(c=>c.value).map(c=>c.textContent);assert.equal(new Set(labels).size,2);assert(labels.every(s=>s.includes('chat-opaque-')&&s.includes('（群 <img onerror=evil()>）')));assert(!distinct.body.textContent.includes('RAW'));
  const detail=env({getScheduledTask:async()=>task({kind:'feishu',label:'安全位置',available:false,reason:'snapshot_mismatch'},{origin:{secret:'DO_NOT_RENDER'},delivery_context:{secret:'CONTEXT_SECRET'}}),listScheduledTaskExecutions:async()=>[]});
  detail.ui.openTaskDetail('t');await tick();assert(detail.root.textContent.includes('安全位置'));assert(!/Origin|Delivery Context|DO_NOT_RENDER|CONTEXT_SECRET|snapshot_mismatch/.test(detail.root.textContent));
  const urls=[]; const c={fetch:async url=>{urls.push(url);return {ok:true,status:200,json:async()=>({})};},NAGENT:{}};c.window=c;vm.runInNewContext(fs.readFileSync(path.join(staticDir,'management-api.js'),'utf8'),c);
  await c.NAGENT.api.listGatewaySessions('fei/shu',{limit:7,offset:9});await c.NAGENT.api.getGatewaySession('feishu','a/b');assert(urls[0].includes('fei%2Fshu/sessions?limit=7&offset=9'));assert(urls[1].endsWith('/a%2Fb'));
  console.log('OK scheduled tasks delivery frontend harness passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
