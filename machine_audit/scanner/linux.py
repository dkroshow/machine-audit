"""Linux filesystem map: a nightly full walk of each local disk filesystem, and hourly
re-lists of the hot folders the collector's watcher saw change. Names and paths stay in
the scanner store; the dirty file between the two holds volume and inode numbers only."""
from __future__ import annotations
import json
import os
from pathlib import Path
import pwd
import sqlite3
import time

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.config import Config
from machine_audit.sources.linux.devices import mounts, volumes as local_filesystems, within
from machine_audit.sources.linux.files import keyed, salted
from machine_audit.sources.linux.host import Stat
from machine_audit.sources.linux.watcher import take_dirty
from .accounting import Blocks
from .native import Volume
from .policy import Policy
from .runner import relist, scan_lock
from .store import MapStore
from .walker import Walker

# System areas a walk never enters. Most are separate filesystems the walk skips anyway.
EXCLUDED = ('/proc', '/sys', '/dev', '/run', '/tmp')
# The hourly timer's night runs walk in full; 20 h keeps it to one walk a night.
NIGHTLY_NS = 20 * 3600 * 10**9
NIGHT_HOURS = range(1, 5)


def volumes(root, salt):
    """(volume, its mountinfo line) per local disk filesystem, identified as the capacity
    source identifies it."""
    seen = set()
    for mount in local_filesystems(root):
        path = under(root, mount.mount)
        try:
            s, device = os.statvfs(path), os.stat(path).st_dev
        except OSError:
            continue  # not reachable by the service user; its capacity reading shows the failure
        identity = salted(salt, f'{s.f_fsid:x}')
        if identity not in seen:
            seen.add(identity)
            yield Volume(identity, os.path.basename(mount.mount) or 'Root', path, mount.filesystem, device,
                         s.f_blocks * s.f_frsize, s.f_bfree * s.f_frsize, s.f_bavail * s.f_frsize, None), mount


def run_scan(data_dir, *, full=False, grain='files', roots=None, exclusions=(), root='/'):
    data, root = Path(data_dir), Path(root)
    budget = Budget(data)
    with scan_lock(data):
        os.nice(10)
        found = list(volumes(root, Config(data_dir=str(data)).identity_salt()))
        if roots:
            found = [(v, m) for v, m in found if v.mount in roots]
            if len(found) != len(roots):
                raise ValueError('only_discovered_volume_roots_are_allowed')
        everything = list(mounts(root))
        homes = [p.pw_dir for p in pwd.getpwall() if 1000 <= p.pw_uid < 65534]
        changed, taken = take_dirty(data)
        store = MapStore(data / 'scanner.sqlite', budget)
        store.written_bytes = lambda: int(keyed((root / 'proc/self/io').read_text(), ':')['write_bytes'][0])
        store.process_start = str(Stat.parse('self', (root / 'proc/self/stat').read_text()).start)
        store.start('requested_full' if full else 'scheduled')
        processed, published, root_failed, results = 0, 0, False, []
        try:
            for volume, entry in found:
                old = store.db.execute('SELECT * FROM volumes WHERE id=?', (volume.id,)).fetchone()
                has_root = old and store.db.execute('SELECT 1 FROM nodes WHERE generation=? AND volume=? AND parent IS NULL LIMIT 1',
                                                    (old['generation'], old['key'])).fetchone()
                if has_root and old['status'] == 'unavailable' and not old['valid']:
                    # Published before Linux maps were observed without a residual; same facts, no re-walk.
                    gaps = sorted(set(json.loads(old['gaps'])) | {'physical_residual_not_computed'})
                    with store.write():
                        store.db.execute("UPDATE volumes SET status='observed',valid=1,gaps=? WHERE id=?", (json.dumps(gaps), volume.id))
                now = time.time_ns()
                age = now - old['full_ns'] if has_root else None
                # Full when there is no map, at night or on request once a day, or when nights were missed.
                night = time.localtime(now / 1e9).tm_hour in NIGHT_HOURS
                action = ('full' if age is None or age >= 2 * NIGHTLY_NS or ((full or night) and age >= NIGHTLY_NS) else
                          'update' if changed.get(volume.id) or store.db.execute(
                              'SELECT 1 FROM dirty WHERE volume=? AND due_ns<=? LIMIT 1', (volume.id, now)).fetchone() else
                          'wait')
                if action == 'wait':
                    results.append({'volume_id': volume.id, 'status': 'current'})
                    continue
                policy = Policy(full_disk_access=True, homes=homes,
                                exclusions=tuple(exclusions) + tuple(under(root, e) for e in EXCLUDED),
                                data_root=str(data), mounts=[under(root, m.mount) for m in everything
                                                             if m.device == entry.device and m.mount != entry.mount
                                                             and within(m.root, entry.root)])
                store.prepare(volume, full=action == 'full', lean=False)
                blocks = Blocks()
                walker = Walker(volume, policy, store, blocks, None, grain)
                if action == 'full':
                    walker.walk()
                    if not walker.listed and 'denied' in walker.gaps:
                        # A root the service user may not read (an EFI partition) is a labelled
                        # gap on that volume, not a failed run.
                        store.buffer.clear()
                        store.buffer_bytes = 0
                        store.observe_unscanned(volume, 'denied', lean=False)
                        results.append({'volume_id': volume.id, 'status': 'denied', 'action': action})
                        continue
                    if not walker.listed:
                        # The previous map stays published; nothing replaces it.
                        root_failed = True
                        processed += walker.entries
                        store.buffer.clear()
                        store.buffer_bytes = 0
                        results.append({'volume_id': volume.id, 'status': 'root_unreadable', 'action': action,
                                        'entries': walker.entries, 'gaps': sorted(walker.gaps)})
                        continue
                else:
                    folders = (store.known_directory(i) for i in sorted(changed.get(volume.id, ())))
                    # A folder replaced since the watch began is left to its parent and the nightly walk.
                    relist(store, walker, policy, volume, now,
                           ((f['path'], False) for f in folders if f and f['path'] and same_folder(f['path'], f['id'])))
                store.finish_volume(volume, blocks, walker.gaps, full=action == 'full', lean=False,
                                    complete=walker.sharing_complete, physical=False)
                published += 1
                processed += walker.entries
                store.progress(processed)
                results.append({'volume_id': volume.id, 'status': 'observed', 'action': action,
                                'entries': walker.entries, 'gaps': sorted(walker.gaps)})
            with store.write():
                current = {v.id for v, _ in found}
                for row in store.db.execute('SELECT id FROM volumes'):
                    if row[0] not in current:
                        store.db.execute('UPDATE volumes SET mounted=0 WHERE id=?', (row[0],))
            for path in taken:
                path.unlink()
            run_status = ('partial' if published else 'root_unreadable') if root_failed else 'complete'
            store.progress(processed, run_status)
            return {'status': run_status if root_failed else 'observed', 'volumes': results, 'entries': processed}
        except (BudgetExceeded, sqlite3.Error) as exc:
            try:
                store.mark_failed('budget_blocked' if isinstance(exc, BudgetExceeded) else 'write_failed')
            except (BudgetExceeded, sqlite3.Error):
                pass
            return {'status': 'budget_blocked' if isinstance(exc, BudgetExceeded) else 'write_failed', 'entries': processed}
        finally:
            store.close()


def under(root, path):
    """A host path below the source root: '/' in service, a synthetic tree in tests."""
    return os.path.normpath(os.path.join(root, path.lstrip('/')))


def same_folder(path, identity):
    try:
        return str(os.stat(path, follow_symlinks=False).st_ino) == identity
    except OSError:
        return False
