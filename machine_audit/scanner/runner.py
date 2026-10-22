"""Scanner process orchestration; hourly log replay and weekly full walks."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import time

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.sources.native import Darwin
from machine_audit.sources.iokit import IOKit
from .native import MacFilesystem
from .events import FSEvents, next_decision
from .errors import ScanError
from .policy import Policy
from .accounting import Blocks
from .store import MapStore
from .walker import Walker, shown_path


@contextmanager
def scan_lock(root):
    fd=os.open(Path(root)/'.scanner.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def relist(store,walker,policy,volume,now,changes):
    """Re-list changed folders, given as (absolute path, recursive), plus deferred ones now due."""
    roots_to_update={}
    recursive_roots=set()
    for due in store.db.execute('SELECT id FROM dirty WHERE volume=? AND due_ns<=?',(volume.id,now)):
        saved=store.known_directory(due[0])
        if saved and saved['path']:
            roots_to_update[saved['path']]=saved
            recursive_roots.add(saved['path'])
    for path,recursive in changes:
        if os.path.commonpath([path,volume.mount])!=volume.mount:
            continue
        # Changes inside excluded folders or this store must not re-walk their parent.
        if policy.decide(shown_path(volume,path)).reason in ('excluded','own_store'):
            continue
        # A descendant can look ordinary while an ancestor requires access.
        # Check the complete path policy before any inode-resolution open.
        ancestor=path
        while policy.decide(shown_path(volume,ancestor)).descend:
            if ancestor==volume.mount:break
            ancestor=os.path.dirname(ancestor)
        else:continue
        # Resolve the nearest mapped directory without exporting event paths.
        found=None
        while path.startswith(volume.mount):
            found=store.find_path(path)
            if not found:
                # Private directories intentionally have no stored path. Resolve
                # their inode through the same no-follow open used by the walk.
                identity=walker.directory_identity(path)
                private=store.known_directory(identity) if identity is not None else None
                if private and private['kind']=='private':
                    parent=walker.directory_identity(os.path.dirname(path)) if path!=volume.mount else None
                    if parent==private['parent']:found=private
            if found:
                break
            if path==volume.mount:
                break
            path=os.path.dirname(path)
        if found:
            # A >20k-entry directory is visited at most daily.
            listing=store.listing(found['id'])
            if listing and listing['count']>20_000 and now-listing['listed_ns']<86400*10**9:
                store.defer(found['id'],listing['listed_ns']+86400*10**9)
                walker.gaps.add('large_directory_deferred')
                continue
            roots_to_update[path]=found
            if recursive:recursive_roots.add(path)
    # The deepest event under an already selected ancestor needs no second walk.
    paths=sorted(roots_to_update,key=len)
    chosen=[]
    complete=True
    for path in paths:
        if any(path==p or (p in recursive_roots and path.startswith(p+'/')) for p in chosen):
            continue
        chosen.append(path)
        previous=roots_to_update[path]
        recursive=path in recursive_roots
        if recursive:store.clear_subtree(previous['id'])
        if not walker.walk(path,previous['parent'],previous['kind']=='private',recursive=recursive):complete=False
    return complete


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
        store=MapStore(root/'scanner.sqlite',budget)
        processed=0
        root_failed=False
        published=0
        results=[]
        try:
            store.written_bytes=lambda:native.process(os.getpid()).diskio_byteswritten
            store.process_start=str(native.process(os.getpid()).proc_start_abstime)
            store.start('requested_full' if full else 'scheduled')
            store.phase('discover')
            volumes=list(fs.volumes())
            if roots:
                selected=[v for v in volumes if v.mount in roots]
                if len(selected)!=len(roots):
                    raise ValueError('only_discovered_volume_roots_are_allowed')
                volumes=selected
            for volume in volumes:
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
                old=store.db.execute('SELECT * FROM volumes WHERE id=?',(volume.id,)).fetchone()
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
                if full and old and old['status'] not in ('observed','partial','invalid','unavailable'):
                    if force or old['full_attempt_ns'] is None or now-old['full_attempt_ns']>=86400*10**9:
                        action,trigger='full','failed_volume_retry'
                    else:action,trigger='wait','failed_full_retry_cooldown'
                if action=='wait':
                    store.phase('complete',action=action,trigger=trigger)
                    status=old['status'] if trigger=='failed_full_retry_cooldown' else 'current'
                    results.append({'volume_id':volume.id,'status':status,'action':action,'trigger':trigger})
                    continue
                store.phase('events',action=action,trigger=trigger)
                event_uuid=events.uuid(volume.device)
                if old and old['event_uuid']!=event_uuid:action,trigger='full','event_uuid_mismatch'
                replay=None
                if action=='update':
                    replay=events.replay(volume.device,old['bookmark'])
                    if replay.lost:
                        action,trigger='full',replay.loss_reason or 'events_lost'
                    elif any(recursive and not os.path.normpath('/'+relative.lstrip('/')).strip('/')
                             for relative,recursive in replay.paths.items()):
                        # Replaying a recursive volume root is a full walk. Use
                        # the full staging/aggregation path, not subtree resets.
                        action,trigger='full','event_root_must_scan'
                    elif any(replay.paths.values()):trigger='event_subtree_must_scan'
                    elif store.db.execute('SELECT 1 FROM dirty WHERE volume=? AND due_ns<=? LIMIT 1',
                                          (volume.id,now)).fetchone():trigger='deferred_folder_due'
                    if action=='update' and store.db.execute('''SELECT 1 FROM nodes n INDEXED BY node_parent
                        JOIN dirty d ON d.volume=? AND d.id=source_id(n.id)
                        WHERE n.generation=? AND n.volume=? AND n.parent IS NULL AND d.due_ns<=? LIMIT 1''',
                        (volume.id,old['generation'],old['key'],now)).fetchone():
                        action,trigger='full','deferred_root_due'
                bookmark=events.current_id() if action=='full' else replay.up_to or replay.last_good or old['bookmark']
                store.phase('prepare',action=action,trigger=trigger)
                store.prepare(volume,full=action=='full',lean=not full_disk_access)
                blocks=Blocks()
                walker=Walker(volume,policy,store,blocks,fs,grain)
                store.phase('walk')
                if action=='full':
                    walker.walk()
                    # A full walk starts with one root; descendants are queued
                    # only after its listing completes. Empty is still listed.
                    if not walker.listed:
                        root_failed=True
                        processed+=walker.entries
                        reason='root_unreadable'+''.join('_'+gap for gap in sorted(walker.gaps-{'root_unreadable'}))
                        store.mark_failed(reason,end_run=False)
                        results.append({'volume_id':volume.id,'status':'root_unreadable','action':action,'trigger':trigger,
                                        'reason':reason,'entries':walker.entries,'gaps':sorted(walker.gaps)})
                        continue
                else:
                    complete=relist(store,walker,policy,volume,now,
                           ((os.path.normpath(os.path.join(volume.mount,relative.lstrip('/'))),recursive)
                            for relative,recursive in replay.paths.items()))
                    if not complete:
                        root_failed=True
                        processed+=walker.entries
                        store.mark_failed('update_unreadable',end_run=False)
                        results.append({'volume_id':volume.id,'status':'update_unreadable','action':action,
                                        'trigger':trigger,'entries':walker.entries,'gaps':sorted(walker.gaps)})
                        continue
                store.finish_volume(volume,blocks,walker.gaps,full=action=='full',lean=not full_disk_access,
                    bookmark=bookmark,event_uuid=event_uuid,complete=walker.sharing_complete and (action=='full' or not replay.interrupted),
                    event_progress=None if not replay or not replay.paths else bookmark!=old['bookmark'])
                published+=1
                processed+=walker.entries
                store.progress(processed)
                results.append({'volume_id':volume.id,'status':'observed','action':action,'trigger':trigger,'entries':walker.entries,
                                'gaps':sorted(walker.gaps)})
            current={v.id for v in volumes}
            missing=[row[0] for row in store.db.execute('SELECT id FROM volumes') if row[0] not in current]
            if missing:
                with store.write(rows=len(missing)):
                    store.db.executemany('UPDATE volumes SET mounted=0 WHERE id=?',((identity,) for identity in missing))
            run_status=('partial' if published else 'root_unreadable') if root_failed else 'complete'
            store.phase('complete')
            store.progress(processed,run_status)
            return {'status':run_status if root_failed else 'observed','volumes':results,'entries':processed,**store.run_context}
        except Exception as exc:
            if isinstance(exc,BudgetExceeded):status=reason='budget_blocked'
            elif isinstance(exc,sqlite3.Error):status=reason='write_failed'
            else:
                status='scan_failed'
                reason=exc.code if isinstance(exc,ScanError) else 'os_error' if isinstance(exc,OSError) else 'invalid_scan_state' if isinstance(exc,ValueError) else 'unexpected_error'
            exception_type=type(exc).__name__
            reason=f'{reason}_{exception_type}'
            # Never print exception text: native/file errors can contain paths.
            # A separate failure to persist the reason is visible in the JSON.
            recorded=False
            try:
                store.mark_failed(reason)
            except Exception:
                pass
            try:
                recorded=bool(store.db.execute('SELECT 1 FROM runs WHERE id=? AND status=? AND ended_ns IS NOT NULL',
                                               (store.run,reason)).fetchone())
            except Exception:
                pass
            return {'status':status,'reason':reason,'exception_type':exception_type,
                    'failure_recorded':recorded,'entries':store.entries,'volumes':results,**store.run_context}
        finally:
            store.close()
