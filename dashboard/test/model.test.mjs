import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readToken,makeApi,chartRuns,rowValue,format,seriesGroups} from '../model.mjs';

test('fragment bootstrap removes the secret before navigation and API uses a header',async()=>{
  const location={hash:'#token=synthetic%2Bsecret',pathname:'/',search:''};
  let replaced;
  const token=readToken(location,{replaceState(...args){replaced=args;}});
  assert.equal(token,'synthetic+secret');assert.equal(replaced[2],'/#overview');
  const calls=[];const api=makeApi(token,async(...args)=>{calls.push(args);return {ok:true,status:200,json:async()=>({rows:[]})};});
  await api('series',{start:1,end:2,component:'memory'});
  assert.equal(calls[0][0],'/api/series?start=1&end=2&component=memory');
  assert.equal(calls[0][1].headers.Authorization,'Bearer synthetic+secret');
  assert.equal(calls[0][1].credentials,'omit');
  assert.ok(!calls[0][0].includes('secret'));
});

test('missing authentication does not make an unauthenticated API request',async()=>{
  let calls=0;const api=makeApi(null,()=>{calls++;});
  await assert.rejects(api('now'),/machine-audit open/);assert.equal(calls,0);
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
