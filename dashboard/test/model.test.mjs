import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readToken,makeApi,queryPool,componentEmptyState,chartRuns,rowValue,format,seriesGroups,ranksInWho,cardSignals} from '../model.mjs';

test('query pool delivers each result and reuses a free slot before the slow peer finishes',async()=>{
  const pending=new Map(),painted=[];let active=0,peak=0;
  const query=queryPool(name=>new Promise(resolve=>{
    active++;peak=Math.max(peak,active);
    pending.set(name,()=>{active--;resolve(name);});
  }));
  const done=['cpu','memory','gpu','storage'].map(name=>query(name).then(value=>painted.push(value)));
  assert.deepEqual([...pending.keys()],['cpu','memory','gpu']);
  pending.get('cpu')();await new Promise(setImmediate);
  assert.deepEqual(painted,['cpu']);
  assert.ok(pending.has('storage'),'The fourth query starts while memory and GPU are still pending');
  pending.get('storage')();pending.get('gpu')();pending.get('memory')();await Promise.all(done);
  assert.equal(peak,3);
  assert.deepEqual(painted,['cpu','storage','gpu','memory']);
});

test('query pool does not start queued work after its view is aborted',async()=>{
  const pending=[];let started=0;
  const query=queryPool(()=>new Promise(resolve=>{started++;pending.push(resolve);}));
  const controller=new AbortController();
  const done=Promise.allSettled([0,1,2,3].map(()=>query('series',{},controller.signal)));
  controller.abort();pending.forEach(resolve=>resolve());
  const results=await done;
  assert.equal(started,3);
  assert.deepEqual(results.map(result=>result.status),['fulfilled','fulfilled','fulfilled','rejected']);
  assert.equal(results[3].reason.name,'AbortError');
});

test('permanent component gaps explain unavailable devices and disabled collection',()=>{
  for(const [status,reason,message] of [
    ['unavailable','no_devices','No supported graphics device on this machine.'],
    ['not_enabled','source_disabled','Graphics collection is not enabled.'],
    ['not_enabled','exclusive_gpu_reader_required','Graphics collection is not enabled. Choose it in machine-audit setup.'],
    ['not_enabled','shipped_helper_required','Graphics detail needs the optional helper, which is not shipped yet.'],
  ]) {
    const coverage={sources:{'synthetic.gpu':{[status]:2}},gaps:[{source:'synthetic.gpu',status,reason}]};
    assert.deepEqual(componentEmptyState('gpu',coverage),{message,sources:[{source:'synthetic.gpu',status,reason}]});
  }
  const sources=[{source:'synthetic.gpu',status:'unavailable',reason:'no_devices'},
    {source:'synthetic.clients',status:'not_enabled',reason:'source_disabled'}];
  assert.deepEqual(componentEmptyState('gpu',{sources:{'synthetic.gpu':{unavailable:1},'synthetic.clients':{not_enabled:1}},gaps:sources}),
    {message:'Graphics is not collected on this machine.',sources});
});

test('an ordinary source among permanent gaps retains the data outage explanation',()=>{
  const coverage={sources:{'synthetic.gpu':{unavailable:2},'synthetic.clients':{observed:1}},
    gaps:[{source:'synthetic.gpu',status:'unavailable',reason:'no_devices'}]};
  assert.deepEqual(componentEmptyState('gpu',coverage),{
    message:'No host or device readings cover this interval. Workload readings and coverage are shown separately.',sources:[],
  });
});

test('fragment bootstrap removes the secret before navigation and API uses a header',async()=>{
  const location={hash:'#token=synthetic%2Bsecret',pathname:'/',search:''};
  let replaced;
  const token=readToken(location,{replaceState(...args){replaced=args;}});
  assert.equal(token,'synthetic+secret');assert.equal(replaced[2],'/#overview');
  assert.equal(readToken({...location,hash:'#token=synthetic&view=storage'},{replaceState(...args){replaced=args;}}),'synthetic');
  assert.equal(replaced[2],'/#storage');
  const calls=[];const api=makeApi(token,async(...args)=>{calls.push(args);return {ok:true,status:200,json:async()=>({rows:[]})};});
  await api('series',{start:1,end:2,component:'memory'});
  assert.equal(calls[0][0],'/api/series?start=1&end=2&component=memory');
  assert.equal(calls[0][1].headers.Authorization,'Bearer synthetic+secret');
  assert.equal(calls[0][1].credentials,'omit');
  assert.ok(!calls[0][0].includes('secret'));
});

test('missing authentication does not make an unauthenticated API request',async()=>{
  let calls=0;const api=makeApi(null,()=>{calls++;});
  const message='This session is not authorized. Open the dashboard link again with its #token=... fragment; the operator finds the current link with machine-audit open on the machine.';
  await assert.rejects(api('now'),{message});assert.equal(calls,0);
  for(const status of [401,403])await assert.rejects(makeApi('synthetic',async()=>({status}))('now'),{message});
});

function tokenStorage() {
  const values=new Map([['machine-audit.token','synthetic']]);
  return {getItem:key=>values.get(key) ?? null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};
}

test('query capacity retries recover without retrying unrelated failures',async()=>{
  let calls=0;
  const api=makeApi('synthetic',async()=>++calls===1 ? {status:503,json:async()=>({error:'query_capacity'})} : {ok:true,status:200,json:async()=>({rows:['ready']})});
  assert.deepEqual(await api('now'),{rows:['ready']});
  assert.equal(calls,2);
  calls=0;
  const other=makeApi('synthetic',async()=>{calls++;return {status:503,json:async()=>({error:'other_failure'})};});
  await assert.rejects(other('now'),/HTTP 503/);
  assert.equal(calls,1);
});

test('query capacity stops after four total attempts',async()=>{
  let calls=0;
  const api=makeApi('synthetic',async()=>{calls++;return {status:503,json:async()=>({error:'query_capacity'})};});
  await assert.rejects(api('now'),{message:'The now request failed (HTTP 503). No missing values have been replaced with zero.'});
  assert.equal(calls,4);
});

test('aborting a capacity backoff prevents the next request',async()=>{
  const controller=new AbortController();let calls=0;
  const api=makeApi('synthetic',async()=>{calls++;return {status:503,json:async()=>({error:'query_capacity'})};});
  const timer=setTimeout(()=>controller.abort(),25);
  try {await assert.rejects(api('now',{},controller.signal),{name:'AbortError'});}
  finally {clearTimeout(timer);}
  assert.equal(calls,1);
});

test('authorization failures clear the per-tab token and stop further requests',async()=>{
  for(const status of [401,403]) {
    const storage=tokenStorage();let calls=0;
    const api=makeApi('synthetic',async()=>{calls++;return {status};},storage);
    await assert.rejects(api('now'),/This session is not authorized/);
    assert.equal(storage.getItem('machine-audit.token'),null);
    await assert.rejects(api('now'),/This session is not authorized/);
    assert.equal(calls,1,'The rejected token must also be cleared from memory');
  }
});

test('a changed collector run clears authorization at the status response boundary',async()=>{
  const storage=tokenStorage();let calls=0;
  const api=makeApi('synthetic',async()=>{
    calls++;
    return {ok:true,status:200,json:async()=>({collector:{run_id:calls<3 ? 'synthetic-run-a':'synthetic-run-b'}})};
  },storage);
  await api('status');await api('status');
  assert.equal(storage.getItem('machine-audit.token'),'synthetic');
  await assert.rejects(api('status'),/This session is not authorized/);
  assert.equal(storage.getItem('machine-audit.token'),null);
  await assert.rejects(api('now'),/This session is not authorized/);
  assert.equal(calls,3,'A run change must also clear the token from memory');
});

test('chart splits denied and absent intervals but preserves a measured zero',()=>{
  const row=(a,b,value,status='observed')=>({start_ns:a,end_ns:b,kind:'gauge',status,last:value});
  const rows=[row(0,1,5),row(1,2,0,'measured_zero'),row(2,3,20,'denied'),row(3,4,7),row(6,7,9)];
  assert.deepEqual(chartRuns(rows).map(r=>r.map(p=>p.value)),[[5,0],[7],[9]]);
  assert.equal(rowValue(rows[2]),null);
});

test('counter charts use only the server-derived rate, preserving original totals',()=>{
  assert.equal(rowValue({kind:'counter',status:'observed',total:20,covered_s:10,mean_rate:2}),2);
  assert.equal(rowValue({kind:'counter',status:'observed',total:20,covered_s:10}),null);
  assert.equal(format(20,'pages'),'20 pages');
  assert.equal(format(null,'bytes'),'—');
});


test('different source scopes stay separate and small measured rates stay nonzero',()=>{
  const row={signal:'power.watts',entity:'host',scope:'system',unit:'W',start_ns:0,end_ns:1};
  assert.equal(seriesGroups([{...row,source:'smc'},{...row,source:'helper'}]).length,2);
  assert.notEqual(format(0.0002,'CPUs'),'0 CPUs');
});

test('who ranks Linux unit consumption but not unit detail',()=>{
  // Review of the Linux adapter: unit signals never reached the Rank-by list.
  assert.ok(ranksInWho({signal:'unit.cpu.stat.usage_usec',scope:'unit'}));
  assert.ok(!ranksInWho({signal:'unit.cpu.weight',scope:'unit_detail'}));
  assert.ok(ranksInWho({signal:'process.cpu',scope:'process'}));
  assert.ok(!ranksInWho({signal:'cpu.busy',scope:'host'}));
});

test('without preferred names an overview card falls back to the order series lists lanes',()=>{
  // Byte order by source then name, as the server sorts lanes; workload scopes never chart.
  const host=(source,signal,scope='host')=>({source,signal,component:'cpu',scope});
  const signals=[host('b','cpu.a'),host('a','cpu.z'),host('a','process.cpu','process'),host('a','cpu.B','unit_detail'),
                 host('a','cpu.Y'),host('c','cpu.fan','fan'),{...host('a','memory.x'),component:'memory'}];
  assert.deepEqual(cardSignals(signals,'cpu'),['cpu.Y','cpu.z','cpu.a']);
});

test('overview cards pick the names a glance wants on Linux and on the Mac',()=>{
  // Alphabetical picks showed Active_anon, Active_file and Buffers instead of MemAvailable.
  const catalog=(source,component,names,scope='host')=>names.map(signal=>({source,signal,component,scope}));
  const linux=[...catalog('linux.cpu','cpu',['cpu.blocked','cpu.busy','cpu.capacity','cpu.load_1m','cpu.pressure.some','cpu.user']),
    ...catalog('linux.memory','memory',['memory.Active_anon','memory.Active_file','memory.Buffers','memory.MemAvailable','memory.SwapFree','memory.pressure.some']),
    ...catalog('linux.disk','storage',['disk.read','disk.write','disk.busy_time','io.pressure.some'],'physical_disk'),
    ...catalog('linux.network','network',['network.receive','network.receive_errors','network.send'],'interface'),
    ...catalog('linux.temperatures','heat',['heat.temperature','heat.temperature_max'],'sensor'),
    ...catalog('self.process','self',['self.build_version','self.cpu','self.flush_count','self.memory','self.storage','self.storage_warning'],'managed_process')];
  const mac=[...catalog('macos.cpu','cpu',['cpu.busy','cpu.capacity','cpu.excess_runnable','cpu.idle','cpu.runnable']),
    ...catalog('macos.memory','memory',['memory.active_count','memory.compressions','memory.free_count','memory.pressure_level','memory.swap_used']),
    ...catalog('macos.disk','storage',['disk.pressure','disk.read','disk.read_latency','disk.write'],'physical_disk'),
    ...catalog('macos.network','network',['network.connect','network.ping_rtt','network.receive','network.send'],'interface'),
    ...catalog('macos.heat','heat',['heat.cpu_available_cpus','heat.fan_cadence','heat.temperature','heat.thermal_pressure'])];
  const picks=(signals,components)=>Object.fromEntries(components.map(c=>[c,cardSignals(signals,c)]));
  assert.deepEqual(picks(linux,['cpu','memory','storage','network','heat','self']),{
    cpu:['cpu.busy','cpu.load_1m','cpu.pressure.some'],memory:['memory.MemAvailable','memory.pressure.some','memory.SwapFree'],
    storage:['disk.read','disk.write','io.pressure.some'],network:['network.receive','network.send','network.receive_errors'],
    heat:['heat.temperature','heat.temperature_max'],self:['self.cpu','self.memory','self.storage']});
  assert.deepEqual(picks(mac,['cpu','memory','storage','network','heat']),{
    cpu:['cpu.busy','cpu.runnable','cpu.capacity'],memory:['memory.free_count','memory.pressure_level','memory.swap_used'],
    storage:['disk.read','disk.write','disk.read_latency'],network:['network.receive','network.send','network.ping_rtt'],
    heat:['heat.temperature','heat.thermal_pressure','heat.cpu_available_cpus']});
});
