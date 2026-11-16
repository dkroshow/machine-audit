"""Scanner process orchestration; hourly log replay and weekly full walks."""
from __future__ import annotations
from contextlib import contextmanager
import errno
import fcntl
import gc
import json
import os
import stat
from pathlib import Path
import sqlite3
import sys
import time
import traceback

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.sources.native import Darwin
from machine_audit.sources.iokit import IOKit
from .native import MacFilesystem
from .events import FSEvents, next_decision
from .encoding import source_id
from .errors import ScanError
from .pace import (LARGE_FOLDER_ENTRIES, LIST_NS_PER_ENTRY, PIECE_WAIT_NS, RELIST_NS_PER_ENTRY, SHARING_NS_PER_HOLDER,
                   TOTALS_NS_PER_FOLDER, Share, allowance)
from .policy import Policy
from .store import MapStore, MergePending
from .walker import Walker, shown_path

# Refusals that more managed storage would admit; reader contention and the
# filesystem free-space margin are retried as before.
CAPACITY_REFUSALS=('managed_storage_limit','sqlite_checkpoint_headroom','sqlite_page_limit')


@contextmanager
def scan_lock(root):
    fd=os.open(Path(root)/'.scanner.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def resolve_mapped(store,walker,volume,path):
    """The nearest mapped directory at or above `path`, as (path, row), without
    exporting event paths. Private directories intentionally have no stored path;
    their inode is resolved through the same no-follow open used by the walk."""
    found=None
    while path.startswith(volume.mount):
        found=store.find_path(path)
        if not found:
            identity=walker.directory_identity(path)
            private=store.known_directory(identity) if identity is not None else None
            if private and private['kind']=='private':
                parent=walker.directory_identity(os.path.dirname(path)) if path!=volume.mount else None
                if parent==private['parent']:found=private
        if found or path==volume.mount:break
        path=os.path.dirname(path)
    return path,found


def live_parent(store,walker,volume,path,stored):
    """The identity a re-listed directory hangs from now. Its stored parent can be a
    row this same run removed: when a directory and its parent were both replaced
    (same paths, new inodes) and each had an event, the parent's new row is staged
    first, and the child must attach to it, not to the removed one."""
    if path==volume.mount:return stored
    identity=walker.directory_identity(os.path.dirname(path))
    return identity if identity is not None and identity!=stored and store.known_directory(identity) else stored


def reallocated(store,walker):
    """Folders to re-list because a file in them now has a different allocation than
    when they were listed: it was still being written then, and the space it had
    reserved was released later with no event. A file gone is left to its folder's
    own change event."""
    folders=set();changes=[]
    for folder,identity,allocated in store.recently_written():
        if folder in folders:continue
        # Any one path of the file gives its allocation; each folder whose row holds the
        # old one is re-listed (a hard-linked copy has a row in every folder linking it).
        path=walker.path_of(source_id(identity))
        try:info=os.stat(path,follow_symlinks=False) if path else None
        except OSError:info=None
        if info is None or str(info.st_ino)!=source_id(identity) or info.st_blocks*512==allocated:continue
        folders.add(folder)
        saved=store.known_directory(source_id(folder))
        folder_path=saved and (saved['path'] or walker.path_of(source_id(folder)))
        if folder_path:changes.append((folder_path,False))
    return changes


def relist(store,walker,policy,volume,now,changes,pace=None,walking=False,parts=None):
    """Re-list changed folders, given as (absolute path, recursive), plus deferred ones now due.
    `pace` is the volume's share of the run: a folder, or a recursive subtree, whose size
    in the map exceeds what is left of it waits for a run whose share covers it; a single
    listing cannot pause part-way, and the weekly walk lists what never fits.
    While the volume `walking`, nothing is queued: its walk lists every folder afresh, and
    the replay after it publishes re-lists every change since it began, so what this
    update leaves is superseded (counted in `store.superseded`, gap walk_supersedes).
    `parts`, when given, receives this re-list's CPU by part (ns): listing folders,
    resolving changes, and looking up and writing deferrals; the rest is its checks."""
    parts=parts if parts is not None else {}
    for key in ('list_ns','events_ns','deferrals_ns'):parts.setdefault(key,0)
    clock=time.process_time_ns
    def fits(identity,recursive):
        return pace is None or pace.covers(store.mapped_entries(identity,recursive)*RELIST_NS_PER_ENTRY)
    def supersede(count=1):
        store.superseded+=count;walker.gaps.add('walk_supersedes')
    unfinished=set()
    def list_paced(path,parent,private,recursive):
        # A new folder has no size in the map to admit it by, and its subtree is listed
        # with it, so the listing pauses between folders once the share is spent. The
        # parents of the folders left are re-listed by a later run, which finds them again.
        first=True;left=[]
        def bound(pending,identity):
            nonlocal first
            if first or pace.covers(0):
                first=False;return 'go'
            left.extend(pending());return 'stop'
        mark=clock()
        try:listed=walker.walk(path,parent,private,recursive=recursive,pace=None if pace is None else bound)
        finally:parts['list_ns']+=clock()-mark
        parents={parent for _,parent,_ in left}
        if parents and walking:supersede(len(parents))
        elif parents:unfinished.update(parents);walker.gaps.add('update_deferred')
        return listed
    mark=clock()
    if walking:
        dropped=store.drop_deferred()
        if dropped:supersede(dropped)
    # Due deferrals, oldest first, read as ids only: each is resolved to its path when
    # reached, so a backlog costs what this run's share re-lists, not its size (Big Mac,
    # Nov 14: resolving 39 k due rows up front spent 31 s before any listing).
    due=store.db.execute('SELECT id,recursive FROM dirty WHERE volume=? AND due_ns<=? ORDER BY due_ns',
                         (volume.id,now)).fetchall()
    due_scope={identity:recursive is None or bool(recursive) for identity,recursive in due}
    parts['deferrals_ns']+=clock()-mark;mark=clock()
    decisions={}  # An event's ancestors are shared with the hour's other events.
    def decide(path):
        if path not in decisions:decisions[path]=policy.decide(shown_path(volume,path))
        return decisions[path]
    events={};recursive_roots=set()
    changes=list(changes)+reallocated(store,walker)
    for path,recursive in changes:
        if os.path.commonpath([path,volume.mount])!=volume.mount:
            continue
        # Changes inside excluded folders or this store must not re-walk their parent.
        if decide(path).reason in ('excluded','own_store'):
            continue
        # A descendant can look ordinary while an ancestor requires access.
        # Check the complete path policy before any inode-resolution open.
        ancestor=path
        while decide(ancestor).descend:
            if ancestor==volume.mount:break
            ancestor=os.path.dirname(ancestor)
        else:continue
        path,found=resolve_mapped(store,walker,volume,path)
        if found:
            # A >20k-entry directory is visited at most daily. One already due is
            # re-listed with its deferral, this event's scope merged in: its listing
            # discharges the deferral, so it must cover what the event needs.
            listing=store.listing(found['id'])
            if found['id'] not in due_scope and listing and listing['count']>LARGE_FOLDER_ENTRIES and now-listing['listed_ns']<86400*10**9:
                if walking:supersede();continue
                store.defer(found['id'],listing['listed_ns']+86400*10**9,recursive)
                walker.gaps.add('large_directory_deferred')
                continue
            events[path]=found
            if recursive:recursive_roots.add(path)
    parts['events_ns']+=clock()-mark
    later=[]  # Left for the next run: the hour's allowance is spent.
    # Chosen folders, and those of them re-listed recursively: a path is covered when
    # chosen itself or below a recursive one, found by walking up its ancestors (the
    # backlog makes thousands of paths, so not by comparing every pair).
    chosen=set();covering=set()
    def covered(path):
        if path in chosen:return True
        while (parent:=os.path.dirname(path))!=path:
            if parent in covering:return True
            path=parent
        return False
    complete=True
    def update(path,previous,recursive,queued):
        """Re-list one folder, or defer it when its share cannot cover it. `queued`: its
        deferral already holds this scope, so a deferral is not written again."""
        nonlocal complete
        if covered(path):return
        chosen.add(path)
        if recursive:covering.add(path)
        if not fits(previous['id'],recursive):
            if walking:supersede();return
            # Its share cannot cover it: a later run re-lists it first.
            if not queued:later.append((previous['id'],now,recursive))
            walker.gaps.add('update_deferred');return
        # Open it before anything is cleared: a walk lists nothing when the open is
        # denied, and a cleared, unwalked directory orphans whatever is staged beneath.
        if walker.directory_identity(path) is None:
            try:present=stat.S_ISDIR(os.stat(path,follow_symlinks=False).st_mode)
            except OSError as exc:present=exc.errno not in (errno.ENOENT,errno.ENOTDIR)
            if present:
                # Still there but not listable (the walker named why): its last map
                # stands and it is retried next hour.
                store.defer(previous['id'],now+3600*10**9,recursive);return
        else:present=True
        if not present:
            # Gone, or no longer a directory: drop its subtree; its parent's listing
            # records the removal or replacement. A parent gone too is recorded by
            # its own ancestor's listing; only an unlistable parent fails the update.
            store.clear_subtree(previous['id'])
            if path==volume.mount:complete=False;return
            parent,above=resolve_mapped(store,walker,volume,os.path.dirname(path))
            # Each way this can fail names itself: the run record is the only evidence.
            if not above:walker.gaps.add('parent_unresolved');complete=False
            elif not list_paced(parent,live_parent(store,walker,volume,parent,above['parent']),above['kind']=='private',False):
                if os.path.lexists(parent):walker.gaps.add('parent_unlistable');complete=False
            return
        if recursive:store.clear_subtree(previous['id'])
        if not list_paced(path,live_parent(store,walker,volume,path,previous['parent']),previous['kind']=='private',recursive):complete=False
    # Deferred folders first, oldest first, so a backlog drains even when each run's
    # share covers little more than the new changes. Those not reached stay queued.
    gone=[]
    for identity,_ in due:
        if pace is not None and not pace.covers(0):break
        mark=clock()
        saved=store.known_directory(identity)
        # A name-withheld folder has no stored path; its inode finds it, in memory.
        path=saved and (saved['path'] or walker.path_of(identity))
        parts['deferrals_ns']+=clock()-mark
        if saved is None:
            # No longer in the map: removed with its parent's listing, which covers it.
            gone.append(identity);continue
        if path:update(path,saved,due_scope[identity] or path in recursive_roots,True)
    mark=clock()
    if gone:store.forget_deferred(gone)
    parts['deferrals_ns']+=clock()-mark
    # Then the changes, by depth, so an ancestor covers its subtree.
    for path in sorted(events,key=len):
        found=events[path];recursive=path in recursive_roots
        # A due deferral not reached this run already queues it, unless the event widens it.
        update(path,found,recursive,found['id'] in due_scope and (due_scope[found['id']] or not recursive))
    # A file hard-linked since a folder holding it was listed is a plain file there and
    # a holder in the re-listed folder, so its bytes would count twice. Re-list the
    # folders that still hold it as a plain file: every link is then a holder and the
    # file counts once.
    # They are paced like any listing: the rest are deferred to the next run.
    for folder in store.plain_links():
        previous=store.known_directory(folder)
        path=previous and (previous['path'] or walker.path_of(folder))
        if not path or path in chosen:continue
        if not fits(folder,False):
            if walking:supersede();continue
            later.append((folder,now,False));walker.gaps.add('update_deferred');continue
        if walker.directory_identity(path) is None:
            store.defer(previous['id'],now+3600*10**9,False);continue
        if not list_paced(path,live_parent(store,walker,volume,path,previous['parent']),previous['kind']=='private',False):complete=False
    mark=clock()
    if later:store.defer_all(later)
    if unfinished:store.defer_unfinished(unfinished,now)
    parts['deferrals_ns']+=clock()-mark
    return complete


CHECKPOINT_NS=30*10**9
# Walks open at once: the one under way, and one listing while it waits for a piece.
# A third waits its turn, which bounds the staged rows beside a walk's publication.
OPEN_WALKS=2


def paced_walk(store,events,fs,policy,grain,volume,trigger,event_uuid,pace,*,lean):
    """One run's share of a volume's full walk: start it, or resume it from its
    checkpoint, and go on through listing, sharing and totals while the allowance
    lasts (storage-approach.md#paced-full-walks). Sharing and totals each run whole,
    in a run whose allowance covers their estimate, or after a day of waiting.
    Returns the walk's state for the run record; `failure` names a failed attempt."""
    walker=None;listed_before=0
    try:
        if not pace.walk_open():
            walk=store.walks().get(volume.id)
            return {'state':'waiting_allowance','phase':walk['phase'] if walk else None}
        walk=store.walks().get(volume.id)
        if walk is None:
            store.phase('prepare',volume_id=volume.id,action='full',trigger=trigger)
            if not store.begin_walk(volume,lean=lean,bookmark=events.current_id(),event_uuid=event_uuid,
                                    trigger=trigger,pace=pace.walk_open):
                return {'state':'pruning'}
            fresh=True
        else:
            store.phase('prepare',volume_id=volume.id,action='full',trigger=walk['trigger'])
            store.resume_walk(volume);fresh=False
        walk=store.walk
        walker=Walker(volume,policy,store,fs,grain)
        # The walker counts this run's entries; the checkpoint keeps the walk's total.
        listed_before=walk['entries'];walker.gaps=set(json.loads(walk['gaps']))
        walker.sharing_complete=bool(walk['sharing_complete'])
        def save(phase=None,pending=None):
            store.checkpoint(pending,entries=listed_before+walker.entries,gaps=walker.gaps,
                             sharing_complete=walker.sharing_complete,phase=phase)
        def state():
            return dict(state='paused',phase=walk['phase'],entries=listed_before+walker.entries,expected=walk['expected'],
                        started_ns=walk['started_ns'],cpu_ns=walk['cpu_ns'],runs=walk['runs'],listed=walker.entries)
        if walk['phase'] in ('listing','large'):
            store.phase('walk')
            mark=pace.clock();later=0
            # Folders the last map holds as large are listed only when the run's share
            # covers them; meanwhile the walk goes on with the others. Once only large
            # folders are left (phase 'large') each waits a day at most, as pieces do.
            large=store.large_folders(LARGE_FOLDER_ENTRIES)
            def ticker(pending,identity):
                # Checkpoint every CHECKPOINT_NS of CPU, so a stop or crash loses little,
                # and when the allowance is spent, which pauses the walk.
                nonlocal mark,later
                if not pace.walk_open():
                    save(pending=pending());return 'stop'
                entries=large.get(identity)
                waited=walk['phase']=='large' and time.time_ns()-walk['phase_ns']>=PIECE_WAIT_NS
                if entries is not None and not waited and not pace.covers(entries*LIST_NS_PER_ENTRY):
                    left=pending();later+=1
                    if later<len(left):return 'later'
                    save('large',left);return 'stop'
                later=0
                if pace.clock()-mark>=CHECKPOINT_NS:save(pending=pending());mark=pace.clock()
                return 'go'
            # A walk refused before its first checkpoint starts again from its root.
            walker.walk(resume=None if fresh or walk['pending'] is None else store.pending(),pace=ticker)
            if walker.paused:return state()
            if not store.walk_root_listed():
                # The root could not be listed: nothing was walked, nothing is kept.
                store.drop_walk(volume.id)
                reason='root_unreadable'+''.join('_'+gap for gap in sorted(walker.gaps-{'root_unreadable'}))
                store.mark_failed(reason,end_run=False)
                return dict(state='failed',failure='root_unreadable',listed=walker.entries,
                            result=dict(reason=reason,gaps=sorted(walker.gaps)))
            save('sharing',[])
        waited=time.time_ns()-walk['phase_ns']>=PIECE_WAIT_NS
        reader=(lambda inode,device=volume.device:fs.extents(device,int(inode))) if volume.fs=='apfs' else None
        if walk['phase']=='sharing':
            if not (pace.covers(SHARING_NS_PER_HOLDER*store.staged_count('holders')) or waited):return dict(state(),state='waiting_allowance')
            gaps=store.place_sharing(reader)
            walker.gaps|=gaps;walker.sharing_complete=walker.sharing_complete and not gaps
            save('totals')
            waited=False
        if not (pace.covers(TOTALS_NS_PER_FOLDER*store.staged_count('nodes')) or waited):return dict(state(),state='waiting_allowance')
        after=next((v for v in fs.volumes() if v.id==volume.id),volume)
        result=state()
        store.finish_volume(after,reader,walker.gaps,full=True,lean=lean,bookmark=walk['bookmark'],event_uuid=walk['event_uuid'],
                            complete=walker.sharing_complete,shared=True)
        return dict(result,state='published',phase='published',result=dict(gaps=sorted(walker.gaps)))
    except BudgetExceeded as exc:
        # The walk pauses: its checkpoint and the rows it describes stand, and the next
        # run retries the refused write. Dropping it would restart days of walking.
        blocked=f'budget_blocked_{exc.code}'
        store.mark_failed(blocked,end_run=False,retry_bytes=exc.retry_bytes)
        return dict(state='failed',failure=blocked,listed=walker.entries if walker else 0,
                    result=dict(stage=store.run_context['stage'],**({'publication':exc.publication} if hasattr(exc,'publication') else {})))


def run_scan(data_dir, *, full_disk_access=False, full=False, grain='files', roots=None, access=None, exclusions=(), force=False):
    if grain not in ('files','folders'):
        raise ValueError('invalid_scanner_grain')
    if sys.platform != 'darwin':
        # Linux has no privacy-prompt boundary, so protected-area access does not apply.
        from .linux import run_scan as linux
        return linux(data_dir,full=full,grain=grain,roots=roots,exclusions=exclusions,force=force)
    # The Mac's operator request is its daily-limited requested full walk.
    full=full or force
    from .permissions import same_process_grant
    full_disk_access=bool(full_disk_access and same_process_grant(access))
    root=Path(data_dir)
    budget=Budget(root)
    # The owner supplies the stable launcher and coexistence gate. No helper,
    # privacy grant, launchd job or predecessor process is changed here.
    with scan_lock(root):
        os.nice(10)
        native=Darwin()
        fs=MacFilesystem(native)
        events=FSEvents(IOKit())
        policy=Policy(full_disk_access=full_disk_access,exclusions=exclusions,data_root=str(root))
        # The allowance counts from here; the operator's forced scan is unpaced.
        pace=allowance(root,force=force)
        store=MapStore(root/'scanner.sqlite',budget)
        store.allowance_ns,store.room_ns=pace.limit,pace.room
        processed=0
        root_failed=False
        published=0
        blocked=None
        results=[]
        failed=False
        try:
            store.written_bytes=lambda:native.process(os.getpid()).diskio_byteswritten
            store.process_start=str(native.process(os.getpid()).proc_start_abstime)
            store.start('requested_full' if full else 'scheduled')
            # A published delta is merged before any scan, so nothing stages over it.
            pending=store.finish_merges(pace.exhausted)
            store.phase('discover')
            volumes=list(fs.volumes())
            if roots:
                selected=[v for v in volumes if v.mount in roots]
                if len(selected)!=len(roots):
                    raise ValueError('only_discovered_volume_roots_are_allowed')
                volumes=selected
            walks=store.walks()
            # Updates go smallest first by their last run's entries, each with an even
            # share of what is left, so one volume's churn cannot starve the others.
            # Walks take turns: those under way first, then in that order (OPEN_WALKS).
            outcomes=store.last_outcomes()
            # Each update's demand: what it re-listed last run and what is queued for it
            # now, at the re-list rate. A walking volume's queue is superseded by its walk.
            last={volume.id:RELIST_NS_PER_ENTRY*((outcomes.get(volume.id) or {}).get('entries') or 0)+
                  (0 if volume.id in walks else RELIST_NS_PER_ENTRY*store.queued_entries(volume.id,time.time_ns()))
                  for volume in volumes}
            listed_order={v.id:i for i,v in enumerate(volumes)}  # Results are reported in volume order.
            volumes=sorted(volumes,key=lambda v:last.get(v.id,0))
            walking=[]  # (volume, trigger, event uuid): paced walk work, after every update
            walk_ahead=bool(walks)  # A walk under way, or queued earlier in this run.
            for position,volume in enumerate(volumes):
                if volume.id in pending:
                    # Its update is published; it is not scanned until its delta merges.
                    # Its map stands. A merge the allowance paused is not a failure.
                    published+=1;root_failed|=pending[volume.id].code!='allowance_spent'
                    store.phase('complete',volume_id=volume.id,action='wait',trigger='merge_pending')
                    status=store.db.execute('SELECT status FROM volumes WHERE id=?',(volume.id,)).fetchone()[0]
                    results.append({'volume_id':volume.id,'status':status,'action':'wait','trigger':'merge_pending',
                                    'merge':pending[volume.id].code})
                    continue
                store.phase('decide',volume_id=volume.id,action='pending',trigger='pending')
                old=store.db.execute('SELECT * FROM volumes WHERE id=?',(volume.id,)).fetchone()
                if old and old['generation'] and not old['lean'] and not full_disk_access:
                    # Missing proof is not grant loss. Preserve the last committed
                    # map and its history; no lean replacement is constructed.
                    status='access_unverified'
                    store.phase('complete',action='skip',trigger=status)
                    with store.write(rows=1):store.db.execute('UPDATE volumes SET status=? WHERE id=?',(status,volume.id))
                    results.append({'volume_id':volume.id,'status':status})
                    continue
                if volume.external and not full_disk_access:
                    store.phase('complete',action='skip',trigger='external_access_not_granted')
                    store.observe_unscanned(volume,'needs_full_disk_access')
                    results.append({'volume_id':volume.id,'status':'not_enabled','reason':'external_volume_requires_full_disk_access'})
                    continue
                now=time.time_ns()
                # Legacy failed walks could publish a timestamp without a root.
                # Such a generation is not a completed scan for cooldown purposes.
                has_root=old and store.db.execute('''SELECT 1 FROM nodes
                    WHERE generation=? AND volume=? AND parent IS NULL LIMIT 1''',
                    (old['generation'],old['key'])).fetchone()
                action,trigger=next_decision(now_ns=now,full_ns=old['full_ns'] if has_root else None,
                    update_ns=old['observed_ns'] if old else None,bookmark=old['bookmark'] if old else None,
                    no_progress=old['no_progress'] if old else 0,requested_full=full)
                # CLI --full can retry immediately. The persistent full setting
                # retries a failed volume at most daily, from its last attempt
                # rather than its last successful publication.
                # Only a failed full walk waits for the daily cooldown; a failed update
                # (its last full published before the attempt) is tried again hourly.
                failed_full=old and old['status'] not in ('observed','partial','invalid','unavailable') and \
                    (old['full_attempt_ns'] is None or old['observed_ns'] is None or old['full_attempt_ns']>old['observed_ns'])
                if full and failed_full:
                    if force or old['full_attempt_ns'] is None or now-old['full_attempt_ns']>=86400*10**9:
                        action,trigger='full','failed_volume_retry'
                    else:action,trigger='wait','failed_full_retry_cooldown'
                # The cap refused this volume's last attempt. An update gets one
                # full walk: it prunes retired rows first and publishes by a small
                # pointer switch. A refused full waits until managed storage falls
                # to its retry level, or, when the refusal could not say how much
                # it lacked, for good; only an operator's forced scan skips that.
                capacity=old and old['status'].removeprefix('budget_blocked_') in CAPACITY_REFUSALS
                retry=old['budget_retry_bytes'] if capacity else None
                gate=capacity and failed_full and not force and (retry is None or not budget.retry_level_reached(retry))
                if capacity and action!='wait':
                    action,trigger=('wait','budget_retry_gate') if gate else ('full','budget_recovery')
                walk=walks.get(volume.id)
                # A walk the cap refused keeps its checkpoint and waits at the same gate;
                # one that only paused at its allowance goes on, or recovery could not finish.
                refused=((outcomes.get(volume.id) or {}).get('walk') or {}).get('state') in ('failed','gated')
                if walk and gate and refused and action!='wait':action,trigger='wait','budget_retry_gate'
                if action=='wait' and (not walk or (gate and refused)):
                    store.phase('complete',action=action,trigger=trigger)
                    status=old['status'] if capacity or trigger=='failed_full_retry_cooldown' else 'current'
                    if capacity:  # Blocked, not up to date, whichever wait held it.
                        root_failed=True;blocked=status
                    results.append({'volume_id':volume.id,'status':status,'action':action,'trigger':trigger,
                                    **({'walk':{'state':'gated'}} if walk else {})})
                    continue
                # A full walk is paced: it runs after every volume's update, in this
                # run's allowance, walks taking turns: one that stops to wait leaves
                # the rest of the allowance to the next volume's walk. Until it
                # publishes, whether under way or queued behind another volume's walk,
                # the hourly update keeps the published map current whenever its events
                # can (a weekly or requested walk). Only the walk starting now skips it.
                walk_trigger=walk['trigger'] if walk else None
                if walk and trigger=='budget_recovery':walk_trigger=trigger  # Reported as the recovery it is.
                if action=='full':
                    if not walk:walk_trigger=trigger
                    starts_now=not walk and not walk_ahead
                    walk_ahead=True
                    # A refused update's recovery is the walk under way: its volume
                    # keeps the hourly update meanwhile.
                    update_due=(not force and not starts_now and has_root and old['bookmark'] is not None
                                and trigger in ('weekly_full','requested_full','budget_recovery')
                                and (old['observed_ns'] is None or now-old['observed_ns']>=3600*10**9))
                    action,trigger=('update','hourly_update') if update_due else ('wait',trigger)
                store.phase('events',action=action,trigger=trigger)
                event_uuid=events.uuid(volume.device)
                if event_uuid is None and (old and old['event_uuid'] or walk):
                    # No answer is not a replaced history: the map, its bookmark and any
                    # walk's checkpoint stand, and the next run asks again.
                    store.phase('complete',action='skip',trigger='events_unavailable')
                    results.append({'volume_id':volume.id,'status':old['status'] if old else 'building',
                                    'action':'skip','trigger':'events_unavailable'})
                    continue
                stale=walk and store.walk_stale(walk,lean=not full_disk_access,event_uuid=event_uuid)
                if stale:  # The reason is recorded with the walk that starts over.
                    store.drop_walk(volume.id);walk=None;walk_trigger='walk_restarted_'+stale
                if action=='update' and old['event_uuid']!=event_uuid:action,trigger,walk_trigger='wait','event_uuid_mismatch',walk_trigger or 'event_uuid_mismatch'
                replay=None
                if action=='update':
                    # Big Mac, Nov 15, under load: fseventsd refused to start the replay
                    # stream three runs running, and each run failed whole. The map and
                    # its bookmark stand, so the next run replays the same window; after a
                    # day without one, the volume walks.
                    try:replay=events.replay(volume.device,old['bookmark'])
                    except OSError:replay=None
                    if replay is None and now-old['observed_ns']<86400*10**9:
                        if walk_trigger:walking.append((volume,walk_trigger,event_uuid))
                        store.phase('complete',action='skip',trigger='events_unavailable')
                        results.append({'volume_id':volume.id,'status':old['status'],'action':'skip',
                                        'trigger':'events_unavailable'})
                        continue
                    if replay is None:action,trigger='wait','events_unavailable'
                    elif replay.lost:
                        action,trigger='wait',replay.loss_reason or 'events_lost'
                    elif any(recursive and not os.path.normpath('/'+relative.lstrip('/')).strip('/')
                             for relative,recursive in replay.paths.items()):
                        # Replaying a recursive volume root is a full walk. Use
                        # the full staging/aggregation path, not subtree resets.
                        action,trigger='wait','event_root_must_scan'
                    elif any(replay.paths.values()):trigger='event_subtree_must_scan'
                    elif store.db.execute('SELECT 1 FROM dirty WHERE volume=? AND due_ns<=? LIMIT 1',
                                          (volume.id,now)).fetchone():trigger='deferred_folder_due'
                    if action=='update' and store.db.execute('''SELECT 1 FROM nodes n INDEXED BY node_parent
                        JOIN dirty d ON d.volume=? AND d.id=source_id(n.id)
                        WHERE n.generation=? AND n.volume=? AND n.parent IS NULL AND d.due_ns<=?
                        AND coalesce(d.recursive,1) LIMIT 1''',
                        (volume.id,old['generation'],old['key'],now)).fetchone():
                        action,trigger='wait','deferred_root_due'
                    if action=='wait':walk_trigger=walk_trigger or trigger
                if walk_trigger:walking.append((volume,walk_trigger,event_uuid))
                if action=='wait':
                    # Only the walk has work here; it reports in the walk pass.
                    store.phase('complete',action='walk',trigger=walk_trigger)
                    results.append({'volume_id':volume.id,'status':old['status'] if old else 'building','action':'full','trigger':walk_trigger})
                    continue
                bookmark=replay.up_to or replay.last_good or old['bookmark']
                walker=None;parts={}  # The re-list's CPU by part, beside the entries it listed.
                try:
                    store.phase('prepare',action=action,trigger=trigger)
                    store.prepare(volume,full=False,lean=not full_disk_access)
                    walker=Walker(volume,policy,store,fs,grain)
                    store.phase('relist')
                    share=Share(pace,last[volume.id],sum(last[v.id] for v in volumes[position:]))
                    store.superseded=0
                    complete=relist(store,walker,policy,volume,now,
                           ((os.path.normpath(os.path.join(volume.mount,relative.lstrip('/'))),recursive)
                            for relative,recursive in replay.paths.items()),share,walking=volume.id in walks,parts=parts)
                    if not complete:
                        root_failed=True
                        processed+=walker.entries
                        store.mark_failed('update_unreadable',end_run=False)
                        results.append({'volume_id':volume.id,'status':'update_unreadable','action':action,
                                        'trigger':trigger,'entries':walker.entries,'gaps':sorted(walker.gaps),**parts,
                                        **({'superseded':store.superseded} if store.superseded else {})})
                        continue
                    # Read used space again: the walk took time, and the reconciliation's
                    # tolerance is how much it changed meanwhile.
                    after=next((v for v in fs.volumes() if v.id==volume.id),volume)
                    reader=(lambda inode,device=volume.device:fs.extents(device,int(inode))) if volume.fs=='apfs' else None
                    store.finish_volume(after,reader,walker.gaps,full=False,lean=not full_disk_access,used_before=volume.used,
                        bookmark=bookmark,event_uuid=event_uuid,complete=walker.sharing_complete and not replay.interrupted,
                        event_progress=None if not replay.paths else bookmark!=old['bookmark'],stop=share.exhausted)
                except BudgetExceeded as exc:
                    # Only this attempt is dropped: its pending writes now, its
                    # staged generation at the next preparation. Other volumes go on.
                    blocked=f'budget_blocked_{exc.code}'
                    root_failed=True
                    entries=walker.entries if walker else 0
                    processed+=entries
                    store.mark_failed(blocked,end_run=False,retry_bytes=exc.retry_bytes)
                    results.append({'volume_id':volume.id,'status':blocked,'action':action,'trigger':trigger,
                                    'stage':store.run_context['stage'],'entries':entries,
                                    **({'publication':exc.publication} if hasattr(exc,'publication') else {})})
                    continue
                except MergePending as exc:
                    # Published; the next run merges the delta before scanning anything.
                    root_failed|=exc.code!='allowance_spent';published+=1;processed+=walker.entries
                    status=store.db.execute('SELECT status FROM volumes WHERE id=?',(volume.id,)).fetchone()[0]
                    results.append({'volume_id':volume.id,'status':status,'action':action,'trigger':trigger,
                                    'entries':walker.entries,'gaps':sorted(walker.gaps),'merge':exc.code,**parts,
                                    **({'superseded':store.superseded} if store.superseded else {})})
                    continue
                published+=1
                processed+=walker.entries
                store.progress(processed)
                results.append({'volume_id':volume.id,'status':'observed','action':action,'trigger':trigger,'entries':walker.entries,
                                'gaps':sorted(walker.gaps),**parts,**({'superseded':store.superseded} if store.superseded else {})})
            walking.sort(key=lambda item:item[0].id not in walks)
            for volume,trigger,event_uuid in walking:
                open_walks=store.walks()
                if volume.id not in open_walks and len(open_walks)>=OPEN_WALKS:outcome={'state':'waiting_turn'}
                else:outcome=paced_walk(store,events,fs,policy,grain,volume,trigger,event_uuid,pace,lean=not full_disk_access)
                store.walk=None  # Ended for this run; a later failure is not this walk's.
                listed=outcome.pop('listed',0);processed+=listed
                store.progress(processed)
                result=next((r for r in results if r['volume_id']==volume.id),None)
                if result is None:results.append(result:={'volume_id':volume.id,'action':'full','trigger':trigger})
                if result['action']=='full':result.update(entries=listed,**outcome.pop('result',{}))
                else:outcome.pop('result',None)
                failure=outcome.pop('failure',None)
                if failure:
                    root_failed=True
                    if failure.startswith('budget_blocked_'):blocked=failure
                    result.update(status=failure)
                elif outcome['state']=='published':
                    published+=1
                    if result['action']=='full':result.update(status='observed')
                result['walk']=outcome
            if pace.walk_open():
                # A replaced map is pruned in the time the hour has left, not in one burst.
                store.phase('prune')
                store.prune_generations(True,pace.walk_open)
            current={v.id for v in volumes}
            missing=[row[0] for row in store.db.execute('SELECT id FROM volumes') if row[0] not in current]
            if missing:
                with store.write(rows=len(missing)):
                    store.db.executemany('UPDATE volumes SET mounted=0 WHERE id=?',((identity,) for identity in missing))
            run_status=('partial' if published else blocked or 'root_unreadable') if root_failed else 'complete'
            store.phase('complete')
            results.sort(key=lambda r:listed_order[r['volume_id']])
            store.record_volumes(results)
            store.progress(processed,run_status)
            return {'status':run_status if root_failed else 'observed','volumes':results,'entries':processed,**store.run_context}
        except Exception as exc:
            # A failed attempt, unlike a stop or crash, does not resume: its staged
            # rows may be what failed. The retry rules decide the next walk.
            failed_walk=store.walk['volume'] if store.walk else None
            exception_type=type(exc).__name__;retry_bytes=getattr(exc,'retry_bytes',None)
            if isinstance(exc,BudgetExceeded):status,reason='budget_blocked',f'budget_blocked_{exc.code}'
            else:
                if isinstance(exc,sqlite3.Error):status=reason='write_failed'
                else:
                    status='scan_failed'
                    reason=exc.code if isinstance(exc,ScanError) else 'os_error' if isinstance(exc,OSError) else 'invalid_scan_state' if isinstance(exc,ValueError) else 'unexpected_error'
                reason=f'{reason}_{exception_type}'
            # Never print exception text: native/file errors can contain paths. The
            # code locations of the failure are this program's own, never user data.
            at=[f'{Path(frame.filename).stem}:{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
            failed=True
        finally:
            if not failed:store.close()
        # The failure is recorded only after its handler ends: until then its
        # traceback keeps the failing frames, and any statement they left open
        # holds a read lock that refuses every counted write, even on a new
        # connection. Frames in reference cycles go with a collection.
        gc.collect()
        recorded=False
        try:
            store.reopen()
            if failed_walk:store.drop_walk(failed_walk)
            store.record_volumes(results)
            store.mark_failed(reason,retry_bytes=retry_bytes)
        except Exception:
            pass
        try:
            recorded=bool(store.db.execute('SELECT 1 FROM runs WHERE id=? AND status=? AND ended_ns IS NOT NULL',
                                           (store.run,reason)).fetchone())
        except Exception:
            pass
        finally:
            store.close()
        # A separate failure to persist the reason is visible in the JSON.
        return {'status':status,'reason':reason,'exception_type':exception_type,'at':at,
                'failure_recorded':recorded,'entries':store.entries,'volumes':results,**store.run_context}
