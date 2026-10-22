import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync,mkdtempSync,readFileSync,writeFileSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=fileURLToPath(new URL('../..',import.meta.url));
const chromePath='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(read, predicate, message) {
  for(let attempt=0;attempt<80;attempt++) {const value=await read();if(predicate(value))return value;await pause(100);}
  throw new Error(message);
}
async function stop(child) {
  if(child.exitCode !== null)return;
  const exit=new Promise(resolve=>child.once('exit',resolve));
  child.kill('SIGTERM');
  await exit;
}
async function browser(t,directory) {
  const profile=join(directory,'chrome');
  const child=spawn(chromePath,['--headless=new','--remote-debugging-port=0',`--user-data-dir=${profile}`,
    '--no-first-run','--disable-background-networking','--disable-component-update','--window-size=1400,1000','about:blank'],{stdio:'ignore'});
  t.after(()=>stop(child));
  const port=await until(()=>{try{return readFileSync(join(profile,'DevToolsActivePort'),'utf8').split('\n')[0];}catch{return null;}},Boolean,'Chrome did not start');
  const pages=await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const socket=new WebSocket(pages.find(page=>page.type==='page').webSocketDebuggerUrl);
  await new Promise(resolve=>socket.addEventListener('open',resolve,{once:true}));
  t.after(()=>socket.close());
  let sequence=0;const pending=new Map(),errors=[],listeners=new Map();
  socket.addEventListener('message',event=>{
    const message=JSON.parse(event.data);
    if(pending.has(message.id)) {const promise=pending.get(message.id);pending.delete(message.id);message.error ? promise.reject(new Error(message.error.message)):promise.resolve(message.result);}
    if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
    if(message.method==='Log.entryAdded'&&message.params.entry.level==='error')errors.push(message.params.entry.text);
    if(listeners.has(message.method))Promise.resolve(listeners.get(message.method)(message.params)).catch(error=>errors.push(error.message));
  });
  const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
  const run=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});assert.equal(result.exceptionDetails,undefined,JSON.stringify(result.exceptionDetails));return result.result?.value;};
  await send('Page.enable');await send('Runtime.enable');await send('Log.enable');
  return {send,run,errors,on:(method,handler)=>listeners.set(method,handler)};
}

test('all dashboard views render real stored replay without leaked tokens or fake zero gaps',{timeout:120000},async t=>{
  if(!existsSync(chromePath))return t.skip('Google Chrome is required for the browser acceptance check');
  const scratch=process.env.MACHINE_AUDIT_TEST_ROOT ?? join(root,'replay/.scratch');
  const {mkdirSync}=await import('node:fs');mkdirSync(scratch,{recursive:true});
  const directory=mkdtempSync(join(scratch,'browser-'));
  const ready=join(directory,'ready.json');
  const python=process.env.MACHINE_AUDIT_PYTHON ?? 'python3.12';
  const preview=spawn(python,['-m','replay.preview','--ready-file',ready],{cwd:root,env:{...process.env,MACHINE_AUDIT_TEST_ROOT:directory},stdio:['ignore','ignore','pipe']});
  let startupError='';preview.stderr.on('data',chunk=>{startupError+=chunk;});
  t.after(()=>stop(preview));
  const connection=await until(()=>{try{return JSON.parse(readFileSync(ready,'utf8'));}catch{return null;}},Boolean,`Preview did not start: ${startupError}`);
  const {send,run,errors}=await browser(t,directory);
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const settled=()=>until(()=>run("document.querySelector('#view').getAttribute('aria-busy')"),value=>value==='false','Page did not finish its query');
  await send('Page.navigate',{url:connection.origin});
  await until(()=>run('document.querySelectorAll("[data-view]").length'),count=>count===10,'Page did not load');
  await settled();
  assert.equal(await run("document.querySelector('#error').hidden"),false,'A missing token must show the authorization message');
  assert.equal(await run("document.querySelector('#error').textContent"),'This session is not authorized. Open the dashboard link again with its #token=... fragment; the operator finds the current link with machine-audit open on the machine.');
  await run(`location.hash='#token=${encodeURIComponent(connection.token)}'`);await pause(100);await settled();
  assert.equal(await run("document.querySelector('#error').hidden"),true,'A token supplied after load must authorize the dashboard');
  assert.ok((await run("document.querySelector('#view').innerText")).length>50,'A token supplied after load must load data');
  assert.equal(await run('location.hash'),'#overview');
  assert.equal(await run('localStorage.length'),0);
  assert.deepEqual(await run('Object.keys(sessionStorage)'),['machine-audit.token']);
  assert.equal(await run("sessionStorage.getItem('machine-audit.token')"),connection.token);
  await send('Page.reload',{ignoreCache:true});
  await until(()=>run("performance.getEntriesByType('navigation')[0]?.type"),value=>value==='reload','The page did not reload');
  await settled();
  assert.equal(await run("document.querySelector('#error').hidden"),true,'A reload without a fragment must restore this tab token');
  assert.ok((await run("document.querySelector('#view').innerText")).length>50);
  assert.equal(await run('location.hash'),'#overview');
  assert.equal(await run('localStorage.length'),0);
  assert.deepEqual(await run('Object.keys(sessionStorage)'),['machine-audit.token']);
  const authErrorStart=errors.length;
  await run("location.hash='#token=synthetic-invalid'");await pause(100);await settled();
  assert.equal(await run("document.querySelector('#error').hidden"),false);
  assert.match(await run("document.querySelector('#error').textContent"),/This session is not authorized/);
  assert.equal(await run('sessionStorage.length'),0,'A 401 must clear the per-tab token');
  assert.ok(!(await run('location.hash')).includes('token'));
  await pause(100);
  assert.ok(errors.splice(authErrorStart).every(error=>error.includes('status of 401')),'Only the intentional authorization failures may be logged');
  await run(`location.hash='#token=${encodeURIComponent(connection.token)}'`);await pause(100);await settled();
  assert.match(await run("document.querySelector('#notice').textContent"),/Synthetic replay/);
  await run("document.querySelector('#span').value='300';document.querySelector('#span').dispatchEvent(new Event('change'))");await settled();
  for(const view of ['overview','cpu','memory','gpu','storage','network','power','heat','cost','coverage']) {
    await run(`location.hash='#${view}'`);await pause(100);await settled();
    assert.equal(await run("document.querySelector('#error').hidden"),true,view);
    assert.ok((await run("document.querySelector('#view').innerText")).length>50,view);
    assert.equal(await run('document.documentElement.scrollWidth<=innerWidth'),true,`${view} overflows desktop`);
    if(view==='cpu') {
      assert.match(await run("document.querySelector('#view').innerText"),/Example worker/);
      assert.match(await run("document.querySelector('#view').innerText"),/Time above level/);
      const button=await run("!![...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Inspect peak'))");
      assert.ok(button,'CPU peak has an interval inspection');
      await run("[...document.querySelectorAll('button')].find(b=>b.textContent.startsWith('Inspect peak')).click()");
      await until(()=>run("document.querySelector('#around-content').innerText"),text=>text.includes('Programs in this interval'),'Around query did not render');
      await run("document.querySelector('#close-around').click()");
    }
    if(view==='storage') {
      assert.match(await run("document.querySelector('#view').innerText"),/Attributed 7 GB \+ not attributed 4 GB = used 11 GB/);
      assert.match(await run("document.querySelector('#view').innerText"),/Example data/);
      await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Example projects').click()");
      await until(()=>run("[...document.querySelectorAll('button')].some(button=>button.textContent==='Example data')"),Boolean,'Folder drilldown did not render');
      await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Example data').click()");
      await until(()=>run("document.querySelector('#view').innerText"),text=>text.includes('Example shared data'),'File drilldown did not render');
      await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Volume root').click()");
      await until(()=>run("[...document.querySelectorAll('button')].some(button=>button.textContent==='Example projects')"),Boolean,'Root breadcrumb did not render');
    }
    if(view==='memory') {
      const text=await run("document.querySelector('#view').innerText");
      assert.match(text,/normal/);assert.match(text,/Unavailable/);
      assert.doesNotMatch(text,/remainder:.*GB/i);
    }
    if(process.env.MACHINE_AUDIT_SCREENSHOTS&&['overview','cpu','cost','storage'].includes(view)) {
      const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(join(resolve(process.env.MACHINE_AUDIT_SCREENSHOTS),`${view}.png`),Buffer.from(image.data,'base64'));
    }
  }
  await run("location.hash='#cpu'");await pause(100);await settled();
  await run("document.querySelector('#live-mode').click()");await settled();
  assert.match(await run("document.querySelector('#view').innerText"),/Original source readings/);
  assert.ok(await run("document.querySelectorAll('.chart').length>0"),'Live uses cache-derived chart data');
  await run("document.querySelector('#history-mode').click()");await settled();
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await run('document.documentElement.scrollWidth<=innerWidth'),true,'Mobile viewport overflows');
  const pagedReady=join(directory,'paged-ready.json');
  const pagedPreview=spawn(python,['-m','replay.preview','--ready-file',pagedReady,'--storage-state','paged'],{cwd:root,env:{...process.env,MACHINE_AUDIT_TEST_ROOT:directory},stdio:'ignore'});
  t.after(()=>stop(pagedPreview));
  const paged=await until(()=>{try{return JSON.parse(readFileSync(pagedReady,'utf8'));}catch{return null;}},Boolean,'Paged-map replay did not start');
  await send('Page.navigate',{url:`${paged.origin}/#token=${encodeURIComponent(paged.token)}`});await settled();
  await run("location.hash='#storage'");await pause(100);await settled();
  const folderCount=()=>run("[...document.querySelectorAll('#view button')].filter(button=>button.textContent.startsWith('Page folder')).length");
  const before=await folderCount();
  assert.ok(before>0&&before<201,'First child page is bounded');
  assert.match(await run("document.querySelector('#view').innerText"),/Includes subfolders/);
  await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Show more folders').click()");
  await until(folderCount,count=>count===201,'Opaque cursor did not load remaining child rows');
  await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Next growth page').click()");
  await until(()=>run("document.querySelector('#view').innerText"),text=>text.includes('Growth page 2'),'Growth cursor did not reach page two');
  await run("[...document.querySelectorAll('button')].find(button=>button.textContent==='Previous growth page').click()");
  await until(()=>run("document.querySelector('#view').innerText"),text=>text.includes('Growth page 1'),'Growth cursor did not return to page one');
  const invalidReady=join(directory,'invalid-ready.json');
  const invalidPreview=spawn(python,['-m','replay.preview','--ready-file',invalidReady,'--storage-state','invalid'],{cwd:root,env:{...process.env,MACHINE_AUDIT_TEST_ROOT:directory},stdio:'ignore'});
  t.after(()=>stop(invalidPreview));
  const invalid=await until(()=>{try{return JSON.parse(readFileSync(invalidReady,'utf8'));}catch{return null;}},Boolean,'Invalid-map replay did not start');
  await send('Page.navigate',{url:`${invalid.origin}/#token=${encodeURIComponent(invalid.token)}`});await settled();
  await run("location.hash='#storage'");await pause(100);await settled();
  assert.match(await run("document.querySelector('#view').innerText"),/Reconciliation is invalid/);
  assert.equal(await run("document.querySelectorAll('.storage-bar').length"),0);
  await run(`sessionStorage.setItem('machine-audit.token','synthetic-stale');history.replaceState(null,'','#token=${encodeURIComponent(invalid.token)}&view=storage')`);
  await send('Page.reload');
  await until(()=>run("performance.getEntriesByType('navigation')[0]?.type"),value=>value==='reload','The page did not reload');
  await settled();
  assert.equal(await run("document.querySelector('#error').hidden"),true,'A fragment must take precedence over a stored token');
  assert.equal(await run('location.hash'),'#storage');
  assert.equal(await run("sessionStorage.getItem('machine-audit.token')"),invalid.token);
  assert.deepEqual(await run('Object.keys(sessionStorage)'),['machine-audit.token']);
  assert.equal(await run('localStorage.length'),0);
  assert.match(await run("document.querySelector('#view').innerText"),/Reconciliation is invalid/);
  assert.ok(errors.length===0,errors.join('\n'));
});

test('original folder growth stays visible through real HTTP and cursor pages',{timeout:90000},async t=>{
  if(!existsSync(chromePath))return t.skip('Google Chrome is required for the browser acceptance check');
  const scratch=process.env.MACHINE_AUDIT_TEST_ROOT ?? join(root,'replay/.scratch');
  const {mkdirSync}=await import('node:fs');mkdirSync(scratch,{recursive:true});
  const directory=mkdtempSync(join(scratch,'growth-browser-'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const ready=join(directory,'ready.json');
  const python=process.env.MACHINE_AUDIT_PYTHON ?? 'python3.12';
  const preview=spawn(python,['-m','replay.preview','--ready-file',ready,'--storage-state','growth'],
    {cwd:root,env:{...process.env,MACHINE_AUDIT_TEST_ROOT:directory},stdio:['ignore','ignore','pipe']});
  let startupError='';preview.stderr.on('data',chunk=>{startupError+=chunk;});
  t.after(()=>stop(preview));
  const connection=await until(()=>{try{return JSON.parse(readFileSync(ready,'utf8'));}catch{return null;}},Boolean,`Growth replay did not start: ${startupError}`);
  const {send,run,errors,on}=await browser(t,directory);
  await send('Emulation.setTimezoneOverride',{timezoneId:'UTC'});
  const requests=[];
  // Use tiny real API pages without changing the application or manufacturing answers.
  on('Fetch.requestPaused',async ({requestId,request})=>{
    const url=new URL(request.url);
    if(url.pathname==='/api/storage'&&url.searchParams.get('action')==='growth') {
      url.searchParams.set('limit','2');requests.push(url.searchParams.get('cursor'));
    }
    await send('Fetch.continueRequest',{requestId,url:url.href});
  });
  await send('Fetch.enable',{patterns:[{urlPattern:'*/api/storage*'}]});
  await send('Page.navigate',{url:`${connection.origin}/#token=${encodeURIComponent(connection.token)}`});
  const settled=()=>until(()=>run("document.querySelector('#view')?.getAttribute('aria-busy')"),value=>value==='false','Page did not finish its query');
  await settled();
  const range=connection.storage_interval;
  const selectInterval=`
    document.querySelector('#span').value='custom';
    document.querySelector('#span').dispatchEvent(new Event('change'));
    for(const [id,seconds] of Object.entries(${JSON.stringify({from:range.start,to:range.end})})) {
      document.getElementById(id).value=new Date(seconds*1000).toISOString().slice(0,19);
    }
    document.querySelector('#interval').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));`;
  await run(selectInterval);await settled();
  await run("location.hash='#storage'");await pause(100);await settled();
  const readRows=()=>run(`(() => {
    const table=[...document.querySelectorAll('#view table')].find(t=>t.caption?.textContent.startsWith('Original folder totals'));
    return table ? [...table.tBodies[0].rows].map(row=>[...row.cells].map(cell=>cell.innerText)) : [];
  })()`);
  const first=await readRows();assert.equal(first.length,2,'Browser must receive a bounded page');
  const rows=[...first];let page=1;
  while(await run("[...document.querySelectorAll('button')].some(b=>b.textContent==='Next growth page')")) {
    await run("[...document.querySelectorAll('button')].find(b=>b.textContent==='Next growth page').click()");
    page++;
    await until(()=>run("document.querySelector('#view').innerText"),text=>text.includes(`Growth page ${page}`),'Next growth page did not render');
    rows.push(...await readRows());
    assert.ok(page<=5,'Nine tracked identities require at most five two-row pages');
  }
  assert.equal(rows.length,9,'Every tracked folder must remain visible, including zero and unknown change');
  assert.equal(new Set(rows.map(row=>row[0])).size,9,'Pages must not repeat rows');
  const byName=new Map(rows.map(row=>[row[0],row]));
  // These expectations are stated scenario inputs, not values derived by the code under test.
  for(const [name,growth,start,end,parent] of [
    ['Small changes','50 MB','2 GB','2.05 GB','Synthetic volume'],
    ['Growing parent','10 GB','30 GB','40 GB','Synthetic volume'],
    ['Growing child','1 GB','10 GB','11 GB','Growing parent'],
    ['Parent with shrinking child','10 GB','30 GB','40 GB','Synthetic volume'],
    ['Shrinking child','−1 GB','10 GB','9 GB','Parent with shrinking child'],
    ['Unchanged parent','0 B','30 GB','30 GB','Synthetic volume'],
    ['Other shrinking child','−1 GB','10 GB','9 GB','Unchanged parent'],
  ]) {
    const row=byName.get(name);assert.ok(row,`${name} was omitted`);
    assert.equal(row[2],growth,name);
    assert.equal(row[3].split('\n')[0],start,name);assert.equal(row[4].split('\n')[0],end,name);
    assert.equal(row[3].split('\n')[1],'Uses',name);assert.equal(row[4].split('\n')[1],'Uses',name);
    assert.equal(row[1].split('\n')[0],parent,`${name}: use historical, not current, parent`);
    assert.equal(row[5],'Observed',name);
  }
  const missing=byName.get('Missing folder');assert.ok(missing);
  assert.equal(missing[1].split('\n')[0],'Folder not observed');
  assert.equal(missing[2],'—');assert.equal(missing[4].split('\n')[0],'—');assert.equal(missing[5],'Not observed');
  const small=byName.get('Small changes');
  const observationTime=await run(`new Date(${(range.end-10)*1000}).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'medium'})`);
  assert.ok(small[4].includes(observationTime),'Use actual observation time, not the requested end');
  assert.ok(small[1].includes(`Placement as of ${observationTime}`));
  await run("[...document.querySelectorAll('button')].find(b=>b.textContent==='Previous growth page').click()");
  page--;
  await until(()=>run("document.querySelector('#view').innerText"),text=>text.includes(`Growth page ${page}`),'Previous growth page did not render');
  assert.deepEqual(await readRows(),rows.slice((page-1)*2,page*2),'Previous page retains the original rows');
  assert.ok(requests.some(Boolean),'Growth continuations must reach the real HTTP route');
  await send('Fetch.disable');
  await run(selectInterval);await settled();
  assert.equal((await readRows()).length,9);
  const text=await run("document.querySelector('#view').innerText");
  assert.match(text,/Includes subfolders\. Parent totals contain child totals\. Do not add nested rows or pages\./);
  assert.doesNotMatch(text,/suppressed|deepest|deduplicat|Parent and child changes can appear on different pages/i);
  assert.equal(await run('document.documentElement.scrollWidth<=innerWidth'),true,'Growth table overflows desktop');
  async function screenshot(name) {
    if(!process.env.MACHINE_AUDIT_SCREENSHOTS)return;
    const layout=await send('Page.getLayoutMetrics');
    const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,
      clip:{x:0,y:0,width:1400,height:Math.ceil(layout.cssContentSize.height),scale:1}});
    writeFileSync(join(resolve(process.env.MACHINE_AUDIT_SCREENSHOTS),name),Buffer.from(image.data,'base64'));
  }
  await screenshot('original-growth.png');
  await run(`
    const select=document.querySelector('select[aria-label="Storage volume"]');
    select.value=[...select.options].find(option=>option.textContent==='Uses to Listed').value;
    select.dispatchEvent(new Event('change'));`);
  await until(readRows,rows=>rows[0]?.[0]==='Accounting change','Accounting-change volume did not render');
  const mismatch=await run(`(() => {
    const table=[...document.querySelectorAll('#view table')].find(t=>t.caption?.textContent.startsWith('Original folder totals'));
    return Object.fromEntries([...table.tHead.rows[0].cells].map((header,i)=>[header.textContent,table.tBodies[0].rows[0].cells[i].innerText]));
  })()`);
  assert.equal(mismatch.Growth,'—','Different measures cannot be subtracted');
  assert.match(mismatch['From observation'],/^4 GB\nUses\n/,'Earlier bytes must retain their own measure');
  assert.match(mismatch['To observation'],/^6 GB\nListed\n/,'Later bytes must retain their own measure');
  assert.equal(mismatch.Status,'Not observed');
  assert.equal(mismatch.Measure,undefined,'No shared measure can mislabel an endpoint');
  await screenshot('growth-measure-change.png');
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.equal(await run('document.documentElement.scrollWidth<=innerWidth'),true,'Growth table overflows mobile');
  assert.deepEqual(errors,[]);
});

test('overview coverage and cost containment survive a crowded catalog',{timeout:90000},async t=>{
  if(!existsSync(chromePath))return t.skip('Google Chrome is required for the browser acceptance check');
  const scratch=process.env.MACHINE_AUDIT_TEST_ROOT ?? join(root,'replay/.scratch');
  const {mkdirSync}=await import('node:fs');mkdirSync(scratch,{recursive:true});
  const directory=mkdtempSync(join(scratch,'coverage-browser-'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const ready=join(directory,'ready.json');
  const preview=spawn(process.env.MACHINE_AUDIT_PYTHON ?? 'python3.12',
    ['-m','replay.preview','--ready-file',ready,'--dashboard-state','crowded'],
    {cwd:root,env:{...process.env,MACHINE_AUDIT_TEST_ROOT:directory},stdio:['ignore','ignore','pipe']});
  let startupError='';preview.stderr.on('data',chunk=>{startupError+=chunk;});
  t.after(()=>stop(preview));
  const connection=await until(()=>{try{return JSON.parse(readFileSync(ready,'utf8'));}catch{return null;}},Boolean,`Crowded replay did not start: ${startupError}`);
  const end=Date.now()/1000;
  const generic=await (await fetch(`${connection.origin}/api/series?start=${end-3600}&end=${end}&limit=600`,
    {headers:{Authorization:`Bearer ${connection.token}`}})).json();
  assert.equal(generic.truncated,true,'Fixture must reach the real global result cap');
  assert.ok(!generic.rows.some(row=>row.component==='memory'),'Fixture must reproduce missing memory in the generic answer');
  const {send,run,errors,on}=await browser(t,directory);
  const requests=[],active=new Set();let peak=0;
  on('Network.requestWillBeSent',({requestId,request})=>{
    const url=new URL(request.url);
    if(url.pathname!=='/api/series')return;
    requests.push(Object.fromEntries(url.searchParams));active.add(requestId);peak=Math.max(peak,active.size);
  });
  on('Network.loadingFinished',({requestId})=>active.delete(requestId));
  on('Network.loadingFailed',({requestId})=>active.delete(requestId));
  await send('Network.enable');
  await send('Page.navigate',{url:`${connection.origin}/#token=${encodeURIComponent(connection.token)}`});
  const settled=()=>until(()=>run("document.querySelector('#view')?.getAttribute('aria-busy')"),value=>value==='false','Page did not finish its query');
  await settled();
  const cards=await run("Object.fromEntries([...document.querySelectorAll('#view .card')].map(card=>[card.querySelector('h2')?.textContent,card.innerText]))");
  const issues=[];
  if(!cards.Memory?.includes('7 GB'))issues.push('Overview hides the observed 7 GB memory reading');
  if(cards.Memory?.includes('No measurements'))issues.push('Overview turns global truncation into missing memory');
  if(!cards.CPU?.includes('Partial sample'))issues.push('The capped CPU card does not disclose its partial answer');
  if(process.env.MACHINE_AUDIT_SCREENSHOTS) {
    const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    writeFileSync(join(resolve(process.env.MACHINE_AUDIT_SCREENSHOTS),'overview-crowded.png'),Buffer.from(image.data,'base64'));
  }
  const overviewRequests=[...requests],overviewPeak=peak;
  await run("location.hash='#cost'");await pause(100);await settled();
  assert.ok(await run("document.querySelector('#view').innerText.includes('synthetic-build-')"),'Long synthetic identity must reach the actual cost view');
  if(!await run('document.documentElement.scrollWidth<=innerWidth'))issues.push('Long cost identity overflows desktop');
  if(process.env.MACHINE_AUDIT_SCREENSHOTS) {
    await run("[...document.querySelectorAll('#view .metric-value')].find(el=>el.textContent.includes('synthetic-build-')).scrollIntoView({block:'center'})");
    const image=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    writeFileSync(join(resolve(process.env.MACHINE_AUDIT_SCREENSHOTS),'cost-long-identity.png'),Buffer.from(image.data,'base64'));
  }
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  if(!await run('document.documentElement.scrollWidth<=innerWidth'))issues.push('Long cost identity overflows mobile');
  assert.deepEqual(issues,[]);
  assert.deepEqual(overviewRequests.map(request=>request.component).sort(),['cpu','gpu','heat','memory','network','power','self','storage']);
  assert.ok(overviewRequests.reduce((sum,request)=>sum+Number(request.limit),0)<=600,'Overview keeps its existing total row allowance');
  assert.ok(overviewPeak<=2,'Overview leaves server capacity for other clients');
  assert.deepEqual(errors,[]);
});
