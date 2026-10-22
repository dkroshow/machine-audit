// Presentation only: resource meaning and interval derivation belong to the query API.
export const measured = new Set(['observed', 'measured_zero']);
export const views = {
  overview: {title:'Overview', description:'A shared view of machine use, constraints and what was actually observed.'},
  cpu: {title:'CPU', description:'Usage and runnable demand are different signals. A busy CPU does not by itself show contention.', note:'Runnable demand is sampled. Excess runnable is an estimate; zero does not exclude waiting under affinity, quotas or policy. Process peaks describe the source pass, not an instantaneous peak.'},
  memory: {title:'Memory', description:'Capacity, page classes, movement and pressure, kept in their original accounting scopes.', note:'Footprints and resident memory can overlap. They are not summed or subtracted from host use. macOS pressure is a state; Linux pressure-stall time measures stalls. Movement alone does not prove harm.'},
  gpu: {title:'Graphics', description:'Activity, memory and clients for each graphics device.', note:'Driver activity is not execution-unit occupancy. GPU memory may overlap host RAM. Queue waiting is unavailable unless a source measures it; activity alone does not prove contention.'},
  storage: {title:'Storage', description:'Where space is used, what grew, and which reads and writes were observed.', note:'Uses counts shared storage once where supported. Listed counts each file in full. A residual is valid only against a comparable total from the same scan; folder sizes are never scaled to fit.'},
  network: {title:'Network', description:'Latency, speed and reliability, with local-link and internet measurements kept separate.', note:'Interface traffic is use; a transfer test measures capacity. A probe is not workload performance. Late replies are distinct from lost replies, and gaps shorter than the probe cadence may be missed.'},
  power: {title:'Power', description:'Source-reported power, supply, batteries and limits, each with its own domain.', note:'Power domains overlap and are not added. SMC system and rail readings are undocumented source estimates, not validated wall power. A configured power cap does not prove it acted. No per-program energy ranking is inferred.'},
  heat: {title:'Heat', description:'Temperatures, cooling and reported capacity limits.', note:'High temperature alone does not prove throttling. Fan speed shows cooling activity. Requested limits, reported throttling and observed frequency stay distinct; coincident heat is not a causal verdict.'},
  cost: {title:'Monitoring cost', component:'self', description:'What running Machine-audit costs, live and over the selected interval.', note:'Shared-process CPU is counted once. Memory scopes may overlap and are not added. Read duration is elapsed time, not CPU time. Browser rendering and indirect kernel/device work remain outside the total unless explicitly measured. Cost evidence helps judge value; it does not establish the benefit alone.'},
  coverage: {title:'Coverage & status', description:'Measured, missing, denied and pruned evidence, including collection and retention status.'},
};
export const statusText = code => ({measured_zero:'Measured zero',not_enabled:'Not enabled',unobserved:'Not observed',unavailable:'Unavailable',observed:'Observed',invalid:'Invalid window',pruned:'Pruned',denied:'Access denied',failed:'Read failed'}[code] ?? String(code ?? 'Not observed').replaceAll('_',' '));
const signalLabels = {
  'cpu.busy':'CPU used', 'cpu.capacity':'Usable CPUs', 'cpu.usable':'Usable CPUs',
  'cpu.runnable':'Runnable demand', 'cpu.excess_runnable':'Excess runnable demand', 'cpu.wait':'CPU waiting',
  'cpu.user':'User CPU time', 'cpu.system':'System CPU time', 'cpu.idle':'Idle CPU time',
  'process.cpu':'CPU use', 'process.memory':'Memory footprint', 'process.phys_footprint':'Memory footprint',
  'process.resident_size':'Resident memory', 'process.ledger_swapins':'Swap-ins', 'process.decompressions':'Decompressions',
  'process.user_time':'User CPU time', 'process.system_time':'System CPU time', 'process.runnable_time':'Runnable time',
  'memory.available':'Available memory', 'memory.pressure':'Memory pressure', 'memory.pressure_level':'Memory pressure level',
  'memory.swapins':'Swap-in pages', 'memory.swap_used':'Swap used', 'memory.swap_total':'Swap capacity',
  'memory.psi.some':'Memory stalls · some work', 'memory.macos_stall':'Memory stall time',
  'gpu.utilization':'Driver-reported activity', 'gpu.activity':'Graphics activity', 'gpu.memory':'Graphics memory',
  'gpu.vram_used':'Dedicated graphics memory', 'gpu.gart_used':'Graphics use of host RAM', 'gpu.wait':'Graphics queue waiting',
  'disk.write':'Disk written', 'disk.read':'Disk read', 'storage.available':'Available storage',
  'network.receive':'Traffic received', 'network.send':'Traffic sent', 'network.router.rtt':'Router round trip',
  'network.internet.rtt':'Internet round trip', 'network.internet.lost':'Lost internet probes',
  'network.capacity.download':'Download capacity', 'power.smc_system':'SMC reported system power',
  'power.watts':'Source-reported power', 'power.supply':'Power supply', 'power.battery':'Battery present',
  'power.wall':'Wall power', 'heat.temperature':'Temperature', 'heat.fan':'Fan speed', 'heat.fan_rpm':'Fan speed',
  'heat.speed_limit':'CPU speed limit', 'self.cpu':'Monitor CPU use', 'self.memory':'Monitor memory',
  'self.storage':'Retained storage', 'self.network':'Monitor network traffic', 'self.query.duration':'Query elapsed time',
};
export const label = value => signalLabels[value] ?? String(value ?? '').replaceAll('_',' ').replaceAll('.',' · ');
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export function number(value, digits=2) {
  if (!finite(value)) return '—';
  return value !== 0 && Math.abs(value) < 0.01 ? value.toLocaleString(undefined,{maximumSignificantDigits:3}) : value.toLocaleString(undefined,{maximumFractionDigits:digits});
}
export function format(value, unit='') {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value !== 'number') return String(value);
  if (!finite(value)) return '—';
  if (unit === 'bytes' || unit === 'B' || unit === 'bytes/s' || unit === 'B/s') {
    const sign=value < 0 ? '−' : '', n=Math.abs(value), places=['B','kB','MB','GB','TB'];
    const index=n ? Math.min(4, Math.max(0,Math.floor(Math.log10(n)/3))) : 0;
    return `${sign}${number(n / 1000 ** index, index || unit.endsWith('/s') ? 2 : 0)} ${places[index]}${unit.endsWith('/s') ? '/s' : ''}`;
  }
  return `${number(value)}${unit ? ' '+unit : ''}`;
}
export function time(ns, full=false) {
  if (!finite(ns)) return 'Unknown time';
  return new Date(ns / 1e6).toLocaleString(undefined, full ? {dateStyle:'medium',timeStyle:'medium'} : {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
export function rowValue(row) {
  if (!measured.has(row?.status)) return null;
  if (row.kind === 'counter') return row.mean_rate ?? row.value ?? null;
  return row.last ?? row.mean ?? row.value ?? null;
}
export function rowUnit(row) {
  return row.kind === 'counter' ? (row.rate_unit ?? row.unit ?? '') : (row.unit ?? '');
}
export function seriesGroups(rows=[]) {
  const groups=new Map();
  for (const row of rows) {
    const key=`${row.source}\0${row.signal}\0${row.entity ?? 'host'}\0${row.scope ?? ''}\0${row.unit}`;
    if (!groups.has(key)) groups.set(key,{...row,rows:[]});
    groups.get(key).rows.push(row);
  }
  for (const group of groups.values()) group.rows.sort((a,b)=>a.start_ns-b.start_ns);
  return [...groups.values()];
}
export function chartRuns(rows) {
  const runs=[]; let run=[]; let previousEnd=null;
  for (const row of rows) {
    const value=rowValue(row), valid=finite(value);
    if (!valid || (previousEnd !== null && row.start_ns > previousEnd)) {
      if (run.length) runs.push(run);
      run=[];
    }
    if (valid) run.push({t:(row.start_ns+row.end_ns)/2, value, row});
    previousEnd=row.end_ns;
  }
  if (run.length) runs.push(run);
  return runs;
}
export function readToken(location, history) {
  const fragment=location.hash.slice(1), params=new URLSearchParams(fragment);
  const token=params.get('token');
  if (token !== null) history.replaceState(null,'',location.pathname+location.search+'#overview');
  return token || null;
}
export function makeApi(token, fetcher=fetch) {
  return async (shape, parameters={}, signal) => {
    if (!token) throw new Error('Open this page with machine-audit open to start an authenticated session.');
    const query=new URLSearchParams();
    for (const [key,value] of Object.entries(parameters)) if (value !== undefined && value !== null) query.set(key,Array.isArray(value) ? value.join(',') : String(value));
    const response=await fetcher(`/api/${shape}${query.size ? '?'+query : ''}`, {headers:{Authorization:`Bearer ${token}`},credentials:'omit',cache:'no-store',signal});
    if (response.status === 401 || response.status === 403) throw new Error('This session is not authorized. Run machine-audit open for a fresh link.');
    if (!response.ok) throw new Error(`The ${shape} request failed (HTTP ${response.status}). No missing values have been replaced with zero.`);
    return response.json();
  };
}
