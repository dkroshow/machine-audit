import {format,number,time,label,statusText,measured,rowValue,rowUnit,seriesGroups,chartRuns,finite} from './model.mjs';
const NS='http://www.w3.org/2000/svg';
export function el(tag, attrs={}, ...children) {
  const svg=['svg','path','line','rect','circle','text','title'].includes(tag);
  const node=svg ? document.createElementNS(NS,tag) : document.createElement(tag);
  for(const [key,value] of Object.entries(attrs)) if(value !== null && value !== undefined) node.setAttribute(key,String(value));
  for(const child of children.flat(Infinity)) if(child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
export const badge = status => el('span',{class:`status ${status ?? 'unobserved'}`},statusText(status));
export function card(title, ...children) {return el('section',{class:'card'},el('div',{class:'card-head'},el('h2',{},title)),...children);}
export function table(headings, rows, caption) {
  return el('div',{class:'table-wrap'},el('table',{},caption ? el('caption',{},caption) : null,el('thead',{},el('tr',{},headings.map(h=>el('th',{scope:'col'},h)))),el('tbody',{},rows.map(cells=>el('tr',{},cells.map((cell,index)=>el(index === 0 ? 'th':'td', index === 0 ? {scope:'row'} : {},cell)))))));
}
export const empty = message => el('p',{class:'empty'},message);
export function metric(name, value, status, note) {
  return el('div',{class:'metric'},el('p',{class:'metric-label'},name),el('p',{class:'metric-value'},value),badge(status),note ? el('p',{class:'note'},note):null);
}
function facts(values) {return el('dl',{class:'source-facts'},Object.entries(values).flatMap(([k,v])=>v == null ? []:[el('dt',{},k),el('dd',{},String(v))]));}
export function chart(group, onAround) {
  const rows=group.rows, valid=rows.filter(r=>finite(rowValue(r)));
  if (!valid.length) return empty(`No numeric measurements for ${label(group.signal)} in this interval. ${[...new Set(rows.map(r=>statusText(r.status)))].join(', ')}.`);
  const W=650,H=180,L=64,R=12,T=12,B=27;
  const start=rows[0].start_ns,end=rows.at(-1).end_ns, values=valid.map(rowValue);
  const low=Math.min(0,...values),high=Math.max(...values,low+1);
  const x=t=>L+(t-start)/Math.max(1,end-start)*(W-L-R),y=v=>T+(high-v)/(high-low)*(H-T-B);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':`${label(group.signal)} over time, ${rowUnit(group)}. Gaps are not measurements.`});
  for(const r of rows.filter(r=>!finite(rowValue(r)))) svg.append(el('rect',{class:'gap',x:x(r.start_ns),y:T,width:Math.max(1,x(r.end_ns)-x(r.start_ns)),height:H-T-B},el('title',{},statusText(r.status))));
  for(const v of [low,(high+low)/2,high]) svg.append(el('line',{class:'axis',x1:L,x2:W-R,y1:y(v),y2:y(v)}),el('text',{x:L-7,y:y(v)+4,'text-anchor':'end'},number(v)));
  for(const run of chartRuns(rows)) {
    svg.append(el('path',{class:'trace',d:run.map((p,i)=>`${i?'L':'M'}${x(p.t).toFixed(2)},${y(p.value).toFixed(2)}`).join(' ')}));
    for(const p of run) {
      const circle=el('circle',{class:'dot',cx:x(p.t),cy:y(p.value),r:2.6},el('title',{},`${format(p.value,rowUnit(p.row))} · ${time(p.t,true)} · ${statusText(p.row.status)}`));
      svg.append(circle);
    }
  }
  svg.append(el('text',{x:L,y:H-3},time(start)),el('text',{x:W-R,y:H-3,'text-anchor':'end'},time(end)));
  const summary=el('figcaption',{},`${rowUnit(group)} · ${group.kind === 'counter' ? 'Rate over each covered window' : 'Last measured value in each window'} · gaps break the line. `);
  const peakRow=valid.reduce((a,b)=>(finite(b.max)?b.max:rowValue(b))>(finite(a.max)?a.max:rowValue(a)) ? b:a);
  const peakAt=peakRow.max_ns ?? peakRow.peak_ns;
  if (onAround && finite(peakAt)) { const b=el('button',{class:'link',type:'button'},`Inspect peak at ${time(peakAt)}`); b.addEventListener('click',()=>onAround(peakAt));summary.append(b); }
  return el('figure',{class:'chart'},svg,summary);
}
export function signalCard(group, onAround) {
  const latest = group.rows.at(-1);
  const shown = group.rows.findLast(row => rowValue(row) !== null) ?? latest;
  const identity = group.label ?? (group.entity !== 'host' ? group.entity : null);
  const title = identity ? `${label(group.signal)} · ${identity}` : label(group.signal);
  const unit = rowUnit(shown);
  const value = rowValue(shown);
  const head = metric(title, value == null ? '—' : format(value,unit), latest.status,
    `${shown.kind === 'counter' ? 'Last measured rate' : 'Last measured value'} · ${time(shown.end_ns)}`);
  const detail = el('details', {class:'disclosure'}, el('summary', {}, 'Source and interval evidence'),
    facts({Signal:group.signal, Entity:group.entity, Scope:group.scope, Unit:group.unit,
      Kind:group.kind, Method:shown.method, Source:shown.source, 'Source version':shown.version}),
    table(['From','To','Rate / value','Original-unit total','Status','Samples'], group.rows.slice(-20).map(row => [
      time(row.start_ns), time(row.end_ns), format(rowValue(row),rowUnit(row)), format(row.total,row.unit), badge(row.status), row.count ?? '—',
    ]), 'Latest 20 returned windows. The query defines their bounds.'));
  const stats = el('p', {class:'note'},
    `That window: mean ${format(shown.mean_rate ?? shown.mean,unit)} · peak ${format(shown.max,unit)}${finite(shown.max_ns) ? ' at '+time(shown.max_ns) : ''}.`);
  const missing = shown !== latest ? el('p', {class:'note'},
    `No newer measurement through ${time(latest.end_ns)}: ${statusText(latest.status)}. The chart keeps that gap.`) : null;
  return el('section', {class:'card'}, head, missing, stats,
    group.kind === 'state' || group.kind === 'event' ? table(['Observed at','State','Status'], group.rows.map(row => [
      time(row.end_ns,true), rowValue(row) ?? '—', badge(row.status),
    ])) : chart(group,onAround), detail);
}
export function signalsView(answer,onAround,filter=()=>true) {
  const groups=seriesGroups(answer?.rows ?? []).filter(filter);
  return groups.length ? el('div',{class:'grid'},groups.map(g=>signalCard(g,onAround))) : empty('No measurements were returned for this component. Check Coverage & status for source access and retained history.');
}
export function liveView(answer, onAround) {
  if (!answer?.sources?.length) {
    return empty('No live readings are available. Live requires a running collector with a recent-read cache. Stored history remains available.');
  }
  const root = el('div');
  if (answer.sources.some(source => source.stale)) {
    root.append(el('p', {class:'notice'}, 'Some latest readings are stale. Check the observation time beside each source; they do not describe the present moment.'));
  }
  const displayRows = answer.trail_rows ?? answer.rows ?? [];
  const hostRows = displayRows.filter(row =>
    !row.entity?.startsWith('program:') && (row.component === 'self' || !row.entity?.startsWith('process:')));
  const perCore = hostRows.filter(row => row.entity?.startsWith('cpu:'));
  root.append(signalsView({rows:hostRows.filter(row => !row.entity?.startsWith('cpu:'))}, onAround));
  if (perCore.length) root.append(el('details', {class:'disclosure'},
    el('summary', {}, 'Per-core source measurements'), signalsView({rows:perCore}, onAround)));
  const programs = (answer.rows ?? []).filter(row => row.component !== 'self' && row.entity?.startsWith('program:'));
  if (programs.length) {
    const body = table(['Program','Measurement','Current window','Observed at'], programs.slice(0,100).map(row => [
      row.label ?? row.entity, label(row.signal), format(rowValue(row),rowUnit(row)), time(row.end_ns),
    ]), 'Latest source-pass rates. These are not instantaneous program measurements.');
    root.append(card('Programs in the latest pass', body,
      programs.length > 100 ? el('p', {class:'note'}, 'Showing the first 100 returned rows. Select a resource in History for its complete ranked workload table.') : null));
  }
  root.append(el('h2', {class:'group-separator'}, 'Original source readings'));
  const sources = el('div', {class:'grid'});
  for (const source of answer.sources) {
    const reading = source.latest?.reading;
    const fields = reading?.fields ?? [];
    const disclosure = el('details', {class:'disclosure'}, el('summary', {}, `${fields.length} of ${source.field_count ?? fields.length} original fields`));
    const rows = el('div');
    function renderFields(count) {
      rows.replaceChildren(table(['Signal / scope','Source value','Status'], fields.slice(0,count).map(field => [
        el('span', {}, label(field.name), el('span', {class:'note'}, `${field.label ?? field.entity} · ${field.scope} · ${field.kind}`)),
        format(field.value,field.unit), badge(field.status),
      ]), 'Original source values. Counters are cumulative snapshots, not rates.'));
      if (count < fields.length) {
        const more = el('button', {type:'button'}, `Show next ${Math.min(100,fields.length-count)} fields`);
        more.addEventListener('click', () => renderFields(count + 100));
        rows.append(more);
      }
    }
    disclosure.addEventListener('toggle', () => {if (disclosure.open && !rows.children.length) renderFields(100);});
    disclosure.append(rows);
    if (source.truncated) disclosure.append(el('p', {class:'note'}, 'This live response reached its field limit. Use the selected resource history or API for additional evidence.'));
    sources.append(card(label(source.source), el('p', {class:'sub'}, badge(source.status),
      source.stale ? ' · Stale reading' : '', reading ? ` · ${time(reading.end?.wall_ns,true)}` : ''),
      fields.length ? disclosure : empty('This source returned no fields.')));
  }
  root.append(sources);
  return root;
}
export function whoView(answer, onAround) {
  const rows=answer?.rows ?? [];
  if(!rows.length)return empty('No workload attribution is available for this signal and interval.');
  const result=table(['Program / scope','Total / mean','Covered time','Mean rate','Peak rate','Time above level','Instances'],rows.map(r=>{
    const peak=r.peak_rate == null ? '—':el('button',{class:'link',type:'button'},format(r.peak_rate,r.rate_unit ?? ''));
    if (peak instanceof Node && finite(r.peak_ns)) peak.addEventListener('click',()=>onAround(r.peak_ns));
    return [el('span',{},r.label ?? r.entity ?? 'Unnamed',el('span',{class:'note'},`${r.scope ?? ''}${r.status ? ' · '+statusText(r.status):''}${r.partial_members ? ' · Some instances missing' : ''}`)),r.total == null ? format(r.mean,r.unit):format(r.total,r.unit),format(r.covered_s,'s'),format(r.mean_rate,r.rate_unit ?? ''),peak, r.above_s ? Object.entries(r.above_s).map(([threshold,seconds])=>`${format(seconds,'s')} above ${threshold} ${r.rate_unit ?? r.unit ?? ''}`).join('; ') : '—',r.instance_count ?? '—'];
  }),'Programs grouped by recorded name where values can add. Overlapping memory stays per instance. Blank fields were not measured.');
  return el('div',{},result,answer.remainder ? el('p',{class:'note'},`Remainder: ${format(answer.remainder.value ?? answer.remainder.total,answer.remainder.unit)} · ${answer.remainder.reason ?? ''}`):null,el('p',{class:'note'},answer.accounting_note ?? 'Mean and peak rates use the covered awake time. A peak describes a source window, not an instantaneous reading.'));
}
export function coverageView(answer) {
  const coverage=answer?.coverage ?? answer ?? {};
  const counts=coverage.sources ?? {};
  const sources=Array.isArray(counts) ? counts : Object.entries(counts).map(([source,counts])=>({source,counts,status:Object.keys(counts).find(k=>!measured.has(k)) ?? 'observed'}));
  const gaps=answer?.gaps ?? coverage.gaps ?? [];
  const sourceRows=sources.map(s=>[s.source ?? s.signal ?? 'Source',badge(s.status),s.reads ?? s.done ?? s.count ?? s.counts?.observed ?? '—',s.failed ?? s.counts?.failed ?? '—',s.denied ?? s.counts?.denied ?? '—',s.reason ?? s.note ?? '—']);
  return el('div',{},sources.length ? table(['Source','Status','Readings','Failed','Denied','Coverage note'],sourceRows) : empty('No source coverage records were returned.'),
    gaps.length ? table(['From','To','Gap','Source / signal'],gaps.map(g=>[time(g.start_ns,true),time(g.end_ns,true),statusText(g.status ?? g.kind),g.source ?? g.signal ?? 'All requested sources'])) : el('p',{class:'note'},'No additional gap spans were returned. This does not prove that every source was enabled.'));
}
export function statusView(status) {
  const health=status?.collector?.sources ?? [];
  const budget=status?.budget;
  return el('div',{class:'grid'},card('Collector',facts({State:status?.collector ? (status.collector.save_error ?? 'Collector status available') : 'No live collector',Version:status?.version ?? 'Not reported','Build identity':status?.build_id ?? 'Not reported','Configuration identity':status?.config_id ?? 'Not reported','Last saved':status?.collector?.last_save_ns ? time(status.collector.last_save_ns,true):'Not reported'}),
    health.length ? table(['Source','State','Read duration','Late by'],health.map(s=>[s.name ?? s.source,badge(s.status),format(s.duration_s,'s'),format(s.lateness_s,'s')])) : el('p',{class:'note'},'No collector source-status records are available.')),
    card('Managed storage budget',budget ? facts({'Managed size':format(budget.bytes,'bytes'),'Warning at':format(budget.warning_bytes ?? 5e9,'bytes'),'Limit':format(budget.limit_bytes ?? 10e9,'bytes'),State:budget.warning ? 'Warning threshold reached' : 'Below warning threshold','Reserved peak':format(budget.reserved_bytes,'bytes')}) : empty('Storage budget measurements are unavailable.'),el('p',{class:'note'},'Includes recording stores, scanner map and managed sidecars. The 5 GB warning and 10 GB limit apply together, not per file.')),
    card('Query and response work',facts({'Queries':status?.collector?.operations?.query_count ?? 'Not reported','Query elapsed time':format(status?.collector?.operations?.query_elapsed_s,'s'),'Bytes served':format(status?.collector?.operations?.http_sent_bytes,'bytes'),'Save elapsed time':format(status?.collector?.operations?.flush_elapsed_s,'s')}),el('p',{class:'note'},'Operation timers measure elapsed time. They are not additional CPU and are not added to process CPU.')),
    card('Cost coverage',el('p',{class:'note'},'Collector, scanner, helper, query and transfer costs appear only where their sources report them. No row means no evidence for that component, not zero cost. Browser rendering and indirect kernel or device work are not measured.')));
}
export function intervalEvidence(answer) {
  if (!answer) return '';
  const covered=answer.covered;
  return [answer.resolution ? `Resolution: ${answer.resolution}`:'',answer.row_width_ns ? `Row width: ${format(answer.row_width_ns/1e9,'s')}`:'',covered?.start_ns ? `Covered ${time(covered.start_ns,true)} – ${time(covered.end_ns,true)}`:'',answer.truncated ? 'More results exist; narrow the interval or use the API continuation.':'',answer.method ? `Method: ${answer.method}`:''].filter(Boolean).join(' · ');
}
