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
from .events import FSEvents, next_action
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
        # Private descendants have no stored names. Revisit the nearest
        # visible parent, keeping their names out of the persistent map.
        event_target=path
        found=None
        while path.startswith(volume.mount):
            found=store.find_path(path)
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
            if recursive or path!=event_target:recursive_roots.add(path)
    # The deepest event under an already selected ancestor needs no second walk.
    paths=sorted(roots_to_update,key=len)
    chosen=[]
    for path in paths:
        if any(path==p or (p in recursive_roots and path.startswith(p+'/')) for p in chosen):
            continue
        chosen.append(path)
        previous=roots_to_update[path]
        recursive=path in recursive_roots
        if recursive:store.clear_subtree(previous['id'])
        walker.walk(path,previous['parent'],previous['kind']=='private',recursive=recursive)
        if previous['id'] in walker.listed:
            with store.write():store.db.execute('DELETE FROM dirty WHERE volume=? AND id=?',(volume.id,previous['id']))


def run_scan(data_dir, *, full_disk_access=False, full=False, grain='files', roots=None, access=None, exclusions=()):
    if grain not in ('files','folders'):
        raise ValueError('invalid_scanner_grain')
    if sys.platform != 'darwin':
        # Linux has no privacy-prompt boundary, so protected-area access does not apply.
        from .linux import run_scan as linux
        return linux(data_dir,full=full,grain=grain,roots=roots,exclusions=exclusions)
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
        store.written_bytes=lambda:native.process(os.getpid()).diskio_byteswritten
        store.process_start=str(native.process(os.getpid()).proc_start_abstime)
        store.start('requested_full' if full else 'scheduled')
        processed=0
        root_failed=False
        published=0
        results=[]
        try:
            volumes=list(fs.volumes())
            if roots:
                selected=[v for v in volumes if v.mount in roots]
                if len(selected)!=len(roots):
                    raise ValueError('only_discovered_volume_roots_are_allowed')
                volumes=selected
            for volume in volumes:
                old=store.db.execute('SELECT * FROM volumes WHERE id=?',(volume.id,)).fetchone()
                if old and old['generation'] and not old['lean'] and not full_disk_access:
                    # Missing proof is not grant loss. Preserve the last committed
                    # map and its history; no lean replacement is constructed.
                    status='access_unverified'
                    with store.write():store.db.execute('UPDATE volumes SET status=? WHERE id=?',(status,volume.id))
                    results.append({'volume_id':volume.id,'status':status})
                    continue
                if volume.external and not full_disk_access:
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
                action=next_action(now_ns=now,full_ns=old['full_ns'] if has_root else None,
                    update_ns=old['observed_ns'] if old else None,bookmark=old['bookmark'] if old else None,
                    no_progress=old['no_progress'] if old else 0,requested_full=full)
                if action=='wait':
                    results.append({'volume_id':volume.id,'status':'current'})
                    continue
                event_uuid=events.uuid(volume.device)
                if old and old['event_uuid']!=event_uuid:action='full'
                replay=None
                if action=='update':
                    replay=events.replay(volume.device,old['bookmark'])
                    if replay.lost:
                        action='full'
                bookmark=events.current_id() if action=='full' else replay.up_to or replay.last_good or old['bookmark']
                store.prepare(volume,full=action=='full',lean=not full_disk_access)
                blocks=Blocks()
                walker=Walker(volume,policy,store,blocks,fs,grain)
                if action=='full':
                    walker.walk()
                    # A full walk starts with one root; descendants are queued
                    # only after its listing completes. Empty is still listed.
                    if not walker.listed:
                        root_failed=True
                        processed+=walker.entries
                        store.buffer.clear();store.buffer_bytes=0
                        results.append({'volume_id':volume.id,'status':'root_unreadable','action':action,
                                        'entries':walker.entries,'gaps':sorted(walker.gaps)})
                        continue
                else:
                    relist(store,walker,policy,volume,now,
                           ((os.path.normpath(os.path.join(volume.mount,relative.lstrip('/'))),recursive)
                            for relative,recursive in replay.paths.items()))
                store.finish_volume(volume,blocks,walker.gaps,full=action=='full',lean=not full_disk_access,
                    bookmark=bookmark,event_uuid=event_uuid,complete=walker.sharing_complete and not (replay and replay.interrupted))
                published+=1
                processed+=walker.entries
                store.progress(processed)
                results.append({'volume_id':volume.id,'status':'observed','action':action,'entries':walker.entries,
                                'gaps':sorted(walker.gaps)})
            with store.write():
                current={v.id for v in volumes}
                for row in store.db.execute('SELECT id FROM volumes'):
                    if row[0] not in current:
                        store.db.execute('UPDATE volumes SET mounted=0 WHERE id=?',(row[0],))
            run_status=('partial' if published else 'root_unreadable') if root_failed else 'complete'
            store.progress(processed,run_status)
            return {'status':run_status if root_failed else 'observed','volumes':results,'entries':processed}
        except (BudgetExceeded,sqlite3.Error) as exc:
            # The last published generation is untouched. Persist failure if even
            # the small pre-existing status page still has write headroom.
            try:
                store.mark_failed('budget_blocked' if isinstance(exc,BudgetExceeded) else 'write_failed')
            except (BudgetExceeded,sqlite3.Error):
                pass
            return {'status':'budget_blocked' if isinstance(exc,BudgetExceeded) else 'write_failed','entries':processed}
        finally:
            store.close()
