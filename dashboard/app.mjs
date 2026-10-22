import {views,label,time,format,readToken,makeApi,queryPool,tokenKey,seriesGroups,rowValue,rowUnit,statusText,ranksInWho} from './model.mjs';
import {el,card,empty,metric,signalsView,liveView,whoView,coverageView,statusView,intervalEvidence} from './render.mjs';
import {storageView} from './storage.mjs';
const $=id=>document.getElementById(id);
let api=makeApi(readToken(window.location,window.history) ?? sessionStorage.getItem(tokenKey));
const state={mode:'history',view:'overview',span:3600,range:null,abort:null,timer:null,catalog:null,whoSignal:null};
const sections={
  cpu:[['Usage and capacity',/busy|capacity|cpu_count|user|system|idle|steal|logical|physical/],['Demand and constraints',/runnable|wait|pressure|thrott|quota|limit/]],
  memory:[['Capacity and page classes',/available|capacity|total|free|active|inactive|wired|speculative|purgeable|used/],['Movement',/swap|pagein|pageout|compress|fault|reclaim/],['Pressure and limits',/pressure|stall|limit|oom/]],
  gpu:[['Activity and clients',/utilization|busy|activity|time|engine/],['Memory',/memory|vram|gart|gtt/],['Capacity and limits',/limit|clock|power|state/]],
  network:[['Latency · where the path responds',/latency|rtt|lookup|connect|dns/],['Speed · traffic and capacity',/receive|send|bytes|download|upload|speed|capacity|link_rate/],['Reliability · late, lost and offline',/loss|lost|late|silence|dropout|path|portal|signal|noise|reliab/]],
  power:[['Consumption by source domain',/watt|power|energy|rail/],['Supply and battery',/supply|battery|charge|ac_online/],['Reported limits',/limit|cap/]],
  heat:[['Temperature by sensor',/temperature|temp/],['Cooling',/fan|rpm|cooling/],['Limits and reported action',/limit|pressure|thrott|frequency|speed/]],
  self:[['CPU and memory',/cpu|memory|rss|footprint/],['Retained storage and I/O',/storage|bytes|disk|read|write|size/],['Network and operations',/network|send|receive|transfer|duration|query|scan/]],
};
function route() { const key=location.hash.slice(1).split('/')[0];return Object.hasOwn(views,key) ? key : 'overview'; }
function interval() {const end=Date.now()/1000;return state.range ?? {start:end-state.span,end};}
function bounds() {return {...interval(),limit:600};}
let busyTimer;
function setBusy(busy) {
  clearTimeout(busyTimer);busyTimer=null;
  $('refresh').disabled=busy;
  $('view').setAttribute('aria-busy',String(busy));
  $('view').classList.remove('loading');
  $('loading').hidden=true;
  if(!busy)return;
  const started=performance.now(),title=views[state.view].title;
  const update=()=>{
    const seconds=Math.floor((performance.now()-started)/1000);
    $('loading-text').textContent=`${seconds>=15 ? 'Still running:':'Loading'} ${title}… ${seconds} s`;
  };
  busyTimer=setTimeout(()=>{
    update();$('loading').hidden=false;$('view').classList.add('loading');
    busyTimer=setInterval(update,1000);
  },300);
}
function message(id,text){$(id).textContent=text??'';$(id).hidden=!text;}
function contentGroups(answer,component,onAround) {
  const remaining=seriesGroups(answer.rows ?? []).filter(g=>component==='self' ? !g.entity?.startsWith('program:') : !g.entity?.startsWith('process:'));
  const perCore=component==='cpu' ? remaining.filter(g=>g.entity?.startsWith('cpu:')) : [];
  for(const g of perCore)remaining.splice(remaining.indexOf(g),1);
  const root=el('div');
  for(const [title,re] of sections[component] ?? []) {
    const selected=remaining.filter(g=>re.test(g.signal));
    if(!selected.length)continue;
    for(const g of selected)remaining.splice(remaining.indexOf(g),1);
    root.append(el('h2',{class:'group-separator'},title),signalsView({...answer,rows:selected.flatMap(g=>g.rows)},onAround));
  }
  if(remaining.length)root.append(el('h2',{class:'group-separator'},'Additional source evidence'),signalsView({...answer,rows:remaining.flatMap(g=>g.rows)},onAround));
  if(perCore.length)root.append(el('details',{class:'disclosure'},el('summary',{},`Per-core source readings (${perCore.length} signals)`),signalsView({...answer,rows:perCore.flatMap(g=>g.rows)},onAround)));
  if(!root.children.length)root.append(empty('No host or device readings cover this interval. Workload readings and coverage are shown separately.'));
  return root;
}
function loading() {
  return el('span',{class:'loading-indicator',role:'progressbar','aria-label':'Loading measurements'},el('span',{'aria-hidden':'true'},'Loading…'));
}
async function fill(target,read,abort,publish) {
  try {
    const content=await read();
    if(abort.aborted)return;
    target.replaceChildren(content);
  } catch(error) {
    if(abort.aborted)return;
    if(error.status===401 || error.status===403)throw error;
    target.replaceChildren(empty(error.message));
  }
  publish?.();
}
async function overview(params,abort,query) {
  const keys=['cpu','memory','gpu','storage','network','power','heat','cost'];
  const root=el('div');
  root.append(el('div',{class:'banner'},el('p',{},'Follow a resource from the whole machine to the programs using it.'),el('p',{class:'sub'},'The selected interval applies throughout. Usage, constraints and missing coverage stay separate.')));
  const grid=el('div',{class:'grid'});
  const bodies=keys.map(key=>{
    const spec=views[key],body=el('div',{},loading());
    const heading=el('div',{class:'card-head'},el('h2',{},spec.title),el('a',{href:`#${key}`},'Explore →'));
    grid.append(el('section',{class:'card'},heading,el('p',{class:'sub'},spec.description),body));
    return body;
  });
  root.append(grid);$('view').replaceChildren(root);
  // Eight component allowances still total 600 rows.
  await Promise.all(keys.map((key,index)=>fill(bodies[index],async()=>{
    const spec=views[key],component=spec.component ?? key;
    const answer=await query('series',{...params,component,limit:75},abort);
    const rows=seriesGroups(answer.rows ?? []).filter(g=>(component==='self' ? !g.entity?.startsWith('program:') : !g.entity?.startsWith('process:')) && !g.entity?.startsWith('cpu:')).slice(0,3);
    return el('div',{},rows.length ? el('div',{class:'signal-list'},rows.map(g=>{const r=g.rows.at(-1);return metric(label(g.signal),format(rowValue(r),rowUnit(r)),r.status);})) : empty(answer.truncated ? 'No host or device measurements were returned in this partial sample.' : 'No host or device measurements were returned for this interval.'),
      answer.truncated ? el('p',{class:'note'},'Partial sample. Explore this component for more detail.') : null);
  },abort)));
}
async function workloadSection(component,params,abort,query) {
  const wanted=(state.catalog.signals ?? []).filter(s=>s.component===component && ranksInWho(s));
  const names=[...new Set(wanted.map(s=>s.signal))];
  if(!names.length)return card('Programs and affected workloads',empty('No workload fields are available for this component. No per-program remainder is inferred.'));
  const preferred={cpu:'process.cpu',memory:'process.phys_footprint',storage:'process.diskio_byteswritten',gpu:'gpu.client_time'}[component];
  const signal=names.includes(state.whoSignal) ? state.whoSignal : names.includes(preferred) ? preferred : names[0];
  const select=el('select',{'aria-label':'Workload measurement'},names.map(n=>el('option',{value:n,...(n===signal ? {selected:''}:{})},label(n))));
  const body=el('div',{},empty('Loading workload evidence…'));
  const block=card('Programs and affected workloads',el('div',{class:'sort-controls'},el('label',{},'Rank by ',select)),body);
  let version=0;
  const update=async()=>{
    const request=++version;
    try {const answer=await query('who',{...params,component,signal:select.value},abort);if(request===version&&!abort?.aborted)body.replaceChildren(whoView(answer,at=>around(at,component)));}
    catch(e){if(e.name!=='AbortError'&&request===version)body.replaceChildren(empty(e.message));}
  };
  select.addEventListener('change',()=>{state.whoSignal=select.value;update();});
  await update();return block;
}
async function around(at,component) {
  const dialog=$('around-dialog');$('around-content').replaceChildren(empty('Loading the surrounding interval…'));
  if(!dialog.open)dialog.showModal();
  try {
    const answer=await api('around',{component,at:at/1e9,span:60,limit:600});
    $('around-title').textContent=`Around ${time(at,true)}`;
    $('around-content').replaceChildren(el('p',{class:'note'},intervalEvidence(answer.series)),signalsView(answer.series),card('Programs in this interval',whoView(answer.who ?? {},()=>{})),card('Coverage',coverageView(answer.coverage)),card('Events',answer.events?.length ? el('ul',{class:'event-list'},answer.events.map(event=>el('li',{},`${time(event.wall_ns ?? event.start_ns,true)} · ${label(event.kind ?? event.signal)} · ${event.value ?? event.status ?? ''}`))) : el('p',{class:'note'},'No events returned for this interval.')));
  } catch(e){$('around-content').replaceChildren(empty(e.message));}
}
async function load() {
  clearTimeout(state.timer);state.abort?.abort();const controller=new AbortController();state.abort=controller;
  const query=queryPool(api);
  state.view=route();const spec=views[state.view],component=spec.component ?? (state.view==='overview'||state.view==='coverage' ? undefined:state.view);
  $('view').classList.toggle('cost-view',state.view==='cost');
  $('view-title').textContent=spec.title;$('view-description').textContent=spec.description;document.title=`${spec.title} · Machine-audit`;
  document.querySelectorAll('[data-view]').forEach(a=>a.setAttribute('aria-current',a.dataset.view===state.view?'page':'false'));
  message('error');message('notice');setBusy(true);
  const params=bounds();$('interval-caption').textContent=state.mode==='live' ? 'Recent source readings · refresh every 5 seconds while visible':`${time(params.start*1e9)} – ${time(params.end*1e9)} · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
  try {
    state.catalog ??= await query('catalog',{},controller.signal);
    if(controller.signal.aborted)return;
    const synthetic=(state.catalog.signals ?? []).some(s=>s.source?.startsWith('synthetic.'));
    if(synthetic)message('notice','Synthetic replay — invented scenario inputs, not measurements from this machine.');
    let answer;
    if(state.view==='coverage') {
      const [coverage,status]=await Promise.all([query('coverage',params,controller.signal),query('status',{},controller.signal)]);
      if(controller.signal.aborted)return;
      answer=coverage;$('view').replaceChildren(statusView(status),card('Interval coverage',coverageView(coverage)));
    } else if(state.mode==='live') {
      answer=await query('now',{component},controller.signal);
      if(controller.signal.aborted)return;
      $('view').replaceChildren(spec.note ? el('p',{class:'intro'},spec.note):el('p',{class:'intro'},'Recent source readings, with their actual observation times.'),liveView(answer,at=>around(at,component)));
    } else if(state.view==='overview') {
      await overview(params,controller.signal,query);
    } else {
      const series=el('div',{},loading()),extra=el('div',{},loading());
      const root=el('div',{},spec.note ? el('p',{class:'intro'},spec.note):null,series,extra);
      const publish=()=>{if(!root.isConnected)$('view').replaceChildren(root);};
      const pending=[fill(series,async()=>{
        answer=await query('series',{...params,component},controller.signal);
        return contentGroups(answer,component,at=>around(at,component));
      },controller.signal,publish)];
      if(state.view==='storage') {
        const block=card('Storage map',loading());root.prepend(block);
        pending.push(fill(block,()=>storageView(query,params,controller.signal),controller.signal,publish));
      }
      pending.push(fill(extra,async()=>state.view==='cost' ? statusView(await query('status',{},controller.signal)) : await workloadSection(component,params,controller.signal,query),controller.signal,publish));
      await Promise.all(pending);
    }
    if(controller.signal.aborted)return;
    $('evidence-footer').textContent=state.view==='overview'&&state.mode==='history' ? 'Each component has its own sample. Open a component for more detail.' : intervalEvidence(answer);
    if(answer?.truncated)message('notice',(synthetic ? 'Synthetic replay. ' : '')+'This answer reaches the result limit. Narrow the interval for detail; the API supplies a continuation for the remaining rows.');
    $('connection').textContent=`Last answer ${new Date().toLocaleTimeString()}`;
  } catch(e) {
    if(e.name!=='AbortError'&&!controller.signal.aborted) {controller.abort();setBusy(false);message('error',e.message);$('view').replaceChildren(empty('Measurements could not be loaded. Refresh after checking that Machine-audit is running.'));$('connection').textContent='Connection needs attention';}
  } finally {
    if(!controller.signal.aborted){setBusy(false);if(state.mode==='live'&&!document.hidden)state.timer=setTimeout(load,5000);}
  }
}
$('refresh').addEventListener('click',()=>{state.catalog=null;load();});
window.addEventListener('hashchange',()=>{
  const token=readToken(window.location,window.history);
  if(token!==null)api=makeApi(token);
  state.whoSignal=null;load();
});
$('span').addEventListener('change',()=>{const custom=$('span').value==='custom';$('custom-range').hidden=!custom;if(!custom){state.span=Number($('span').value);state.range=null;load();}});
$('interval').addEventListener('submit',event=>{event.preventDefault();const start=Date.parse($('from').value)/1000,end=Date.parse($('to').value)/1000;if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start){message('error','Choose a start and end time, with the end after the start.');return;}state.range={start,end};load();});
for(const mode of ['history','live'])$(mode+'-mode').addEventListener('click',()=>{state.mode=mode;for(const m of ['history','live'])$(m+'-mode').setAttribute('aria-pressed',String(m===mode));$('span').disabled=mode==='live';$('custom-range').hidden=mode==='live'||$('span').value!=='custom';load();});
$('close-around').addEventListener('click',()=>$('around-dialog').close());
$('theme').addEventListener('change',()=>{document.documentElement.dataset.theme=$('theme').value;});
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(state.timer);state.abort?.abort();setBusy(false);}else if(state.mode==='live')load();});
const now=new Date();const local=date=>new Date(date-date.getTimezoneOffset()*60000).toISOString().slice(0,19);
$('to').value=local(now);$('from').value=local(new Date(now-3600000));
load();
