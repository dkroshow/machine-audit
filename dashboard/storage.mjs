import {el,empty,table,badge} from './render.mjs';
import {format,time,finite,statusText} from './model.mjs';

function historicalParent(row) {
  let label = 'Placement not observed';
  if (row.hierarchy_status === 'observed') {
    label = row.parent_id === null ? 'No parent (volume root)' : row.parent_label ?? '(name withheld)';
  } else if (row.hierarchy_status === 'not_observed') {
    label = 'Folder not observed';
  } else if (row.hierarchy_status === 'parent_unobserved') {
    label = 'Parent name not observed';
  }
  return el('span', {}, label,
    el('span', {class:'note'}, `Placement as of ${time(row.hierarchy_observed_ns, true)}`));
}

function observation(bytes, measure, at) {
  const label = measure === 'uses' ? 'Uses' : measure === 'listed' ? 'Listed' : measure ?? 'Unknown measure';
  return el('span', {}, format(bytes, 'bytes'), el('span', {class:'note'}, label), el('span', {class:'note'}, time(at, true)));
}

// The scanner owns reconciliation and growth. This file only presents its answers.
export async function storageView(api, interval, abort) {
  const answer = await api('storage', {action:'volumes'}, abort);
  const root = el('div', {}, el('h2', {}, 'Storage map'));
  const volumes = answer.rows ?? [];
  if (!volumes.length) {
    root.append(empty('No saved volume map is available. A scan has not completed, or access is not enabled.'));
    return root;
  }
  const select = el('select', {'aria-label':'Storage volume'}, volumes.map(v =>
    el('option', {value:v.id}, v.label ?? v.id)));
  root.append(el('p', {}, el('label', {}, 'Volume ', select)));
  const body = el('div');
  root.append(body);
  const history = [];
  let requestVersion = 0;

  async function show(parent, cursor=null, previousRows=[]) {
    const request = ++requestVersion;
    body.replaceChildren(empty('Loading saved folder measurements…'));
    try {
      const volume = volumes.find(v => v.id === select.value);
      const [children, growth] = await Promise.all([
        api('storage', {action:'children', volume_id:select.value, parent_id:parent, cursor}, abort),
        api('storage', {action:'growth', volume_id:select.value, start:interval.start, end:interval.end}, abort),
      ]);
      if (request !== requestVersion || abort?.aborted) return;
      const breadcrumbs = el('div', {class:'breadcrumbs'});
      for (const [i, item] of history.entries()) {
        const button = el('button', {type:'button'}, item.name);
        button.addEventListener('click', () => {history.splice(i + 1); show(item.id);});
        breadcrumbs.append(button);
      }
      const rows = previousRows.concat(children.rows ?? []);
      const details = el('p', {class:'note'}, badge(children.status),
        ` · Map as of ${time(volume.observed_ns, true)}`);
      const blocks = [breadcrumbs, details, el('p', {class:'note'}, `At this map observation: capacity ${format(volume.capacity,'bytes')} · free ${format(volume.free,'bytes')} · available to programs ${format(volume.available,'bytes')}.`)];
      if (volume.gaps?.length) blocks.push(el('p', {class:'notice'}, `Map coverage gaps: ${volume.gaps.map(statusText).join(', ')}.`));
      if (volume.valid === true) {
        blocks.push(el('p', {class:'banner'},
          `Attributed ${format(volume.uses,'bytes')} + not attributed ${format(volume.not_attributed,'bytes')} = used ${format(volume.volume_used,'bytes')}`));
        if (finite(volume.volume_used) && volume.volume_used > 0) {
          const bar = el('svg', {class:'storage-bar', viewBox:'0 0 100 8', preserveAspectRatio:'none', role:'img', 'aria-label':
            `Attributed ${format(volume.uses,'bytes')}; not attributed ${format(volume.not_attributed,'bytes')}`} );
          let x = 0;
          for (const [name, value] of [['Attributed',volume.uses], ['Not attributed',volume.not_attributed]]) {
            if (finite(value) && value >= 0) {
              const width = value / volume.volume_used * 100;
              bar.append(el('rect', {class:name === 'Not attributed' ? 'residual' : '', x, y:0, width, height:8},
                el('title', {}, `${name}: ${format(value,'bytes')}`)));
              x += width;
            }
          }
          blocks.push(bar);
        }
      } else {
        blocks.push(el('p', {class:'notice'},
          volume.status === 'invalid' ? 'Reconciliation is invalid. Folder uses are withheld; they have not been scaled to fit the volume.' :
            'Physical-space reconciliation is unavailable. Listed bytes are not a physical-space total.'));
      }
      blocks.push(el('p', {class:'note'},
        `Volume used: ${format(volume.volume_used,'bytes')} · Listed in map: ${format(volume.listed,'bytes')}. Shared copies count in full in listed bytes.`));
      if (children.truncated) blocks.push(el('p', {class:'note'}, 'More folders are available below this page.'));
      blocks.push(rows.length ? table(['Folder / file','Uses','Listed','Coverage'], rows.map(row => {
        const button = el('button', {type:'button',class:'link'}, row.label ?? '(name withheld)');
        if (row.kind === 'file' || row.is_dir === false) button.disabled = true;
        else button.addEventListener('click', () => {history.push({id:row.id,name:row.label ?? 'Folder'}); show(row.id);});
        return [button, format(volume.valid === false ? null : row.uses,'bytes'), format(row.listed,'bytes'), badge(row.status)];
      }), 'Sizes are from the saved scan. A withheld name can mark a privacy boundary. A missing size is not zero.') :
        empty('No visible children in this saved map.'));
      if (children.truncated && children.next_cursor) {
        const more = el('button', {type:'button'}, 'Show more folders');
        more.addEventListener('click', () => show(parent, children.next_cursor, rows));
        blocks.push(more);
      }
      const growthPanel = el('div');
      const growthCursors = [null];
      let growthRequest = 0;
      function renderGrowth(page, pageIndex) {
        const changes = page.rows ?? [];
        const parts = [el('h3', {class:'group-separator'}, 'Growth in the selected interval'),
          el('p', {class:'notice'}, 'Includes subfolders. Parent totals contain child totals. Do not add nested rows or pages.')];
        parts.push(changes.length ? table(['Folder','Parent at interval end','Growth','From observation','To observation','Status'], changes.map(row => [
          row.label ?? '(name withheld)', historicalParent(row), format(row.change,'bytes'),
          observation(row.start_bytes,row.start_metric,row.start_ns), observation(row.end_bytes,row.end_metric,row.end_ns), badge(row.status),
        ]), 'Original folder totals, including subfolders. Each size shows its measure and actual observation time. Placement uses saved history at or before the interval end. Missing or incompatible measurements have no growth comparison.') :
          empty('No tracked folders were returned on this page. Missing observations are not zero growth.'));
        const controls = el('div', {class:'sort-controls'}, `Growth page ${pageIndex + 1}`);
        async function changePage(index, nextCursor) {
          const pending = ++growthRequest;
          growthPanel.setAttribute('aria-busy', 'true');
          try {
            const next = await api('storage', {action:'growth', volume_id:volume.id, start:interval.start, end:interval.end, cursor:nextCursor}, abort);
            if (pending === growthRequest && request === requestVersion && !abort?.aborted) {
              growthCursors[index] = nextCursor;
              renderGrowth(next, index);
            }
          } catch (error) {
            if (error.name !== 'AbortError' && pending === growthRequest) growthPanel.append(empty(error.message));
          } finally {
            if (pending === growthRequest) growthPanel.setAttribute('aria-busy', 'false');
          }
        }
        if (pageIndex > 0) {
          const previous = el('button', {type:'button'}, 'Previous growth page');
          previous.addEventListener('click', () => changePage(pageIndex - 1, growthCursors[pageIndex - 1]));
          controls.append(previous);
        }
        if (page.truncated && page.next_cursor) {
          const next = el('button', {type:'button'}, 'Next growth page');
          next.addEventListener('click', () => changePage(pageIndex + 1, page.next_cursor));
          controls.append(next);
        }
        parts.push(controls);
        growthPanel.replaceChildren(...parts);
      }
      renderGrowth(growth, 0);
      blocks.push(growthPanel);
      body.replaceChildren(...blocks);
    } catch (error) {
      if (error.name !== 'AbortError' && request === requestVersion) body.replaceChildren(empty(error.message));
    }
  }
  select.addEventListener('change', () => {history.splice(0); history.push({id:null,name:'Volume root'}); show();});
  history.push({id:null,name:'Volume root'});
  await show();
  return root;
}
