"""Scanner privacy boundary. Decisions use paths in memory, never in exports."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import PurePosixPath
import os
import pwd

REVISION = 1
PRIVATE = {'library/mail', 'library/messages', 'library/safari', 'music/music', 'movies/tv'}
CLOUD = {'library/cloudstorage', 'library/mobile documents'}
ASKS = {'desktop', 'documents', 'downloads', 'library/containers', 'library/group containers',
        'library/calendars', 'library/reminders', 'library/application support/addressbook'}
BUNDLES = ('.photoslibrary', '.musiclibrary', '.tvlibrary', '.imovielibrary', '.fcpbundle')


@dataclass(frozen=True)
class Decision:
    descend: bool = True
    names: bool = True
    reason: str | None = None


class Policy:
    def __init__(self, *, full_disk_access=False, home=None, homes=None, exclusions=(), data_root=None, mounts=()):
        self.full_disk_access = full_disk_access
        self.mounts = tuple(os.path.normpath(p) for p in mounts)
        self.home = os.path.normpath(home or os.path.expanduser('~'))
        self.homes = homes if homes is not None else [p.pw_dir for p in pwd.getpwall() if p.pw_uid >= 501]
        self.exclusions = tuple(os.path.normpath(p) for p in exclusions)
        self.data_root = os.path.normpath(data_root) if data_root else None

    def decide(self, path, inherited_private=False):
        p = os.path.normpath(path)
        low = p.lower()
        parts = PurePosixPath(low).parts
        rel, root = None, None
        # Nested backup/copy homes get the same boundary as live homes.
        for i in range(len(parts) - 1):
            if parts[i] == 'users' and parts[i + 1] != 'shared':
                root = str(PurePosixPath(*parts[:i + 2]))
                rel = '/'.join(parts[i + 2:])
        for h in self.homes:
            h = os.path.normpath(h).lower()
            if low == h or low.startswith(h + '/'):
                if root is None or len(h) > len(root):
                    root, rel = h, low[len(h):].lstrip('/')
        if any(p == e or p.startswith(e + '/') for e in self.exclusions):
            return Decision(False, False, 'excluded')
        if self.data_root and (p == self.data_root or p.startswith(self.data_root + '/')):
            return Decision(False, False, 'own_store')
        # A second mount of the walked filesystem would count its contents twice.
        if any(p == m or p.startswith(m + '/') for m in self.mounts):
            return Decision(False, True, 'bind_mount')
        if root and rel == '' and PurePosixPath(root).name != PurePosixPath(self.home.lower()).name:
            return Decision(False, True, 'other_account')
        if rel in CLOUD or any(rel and rel.startswith(c + '/') for c in CLOUD):
            return Decision(False, False, 'cloud')
        if parts[-1:] == ('backups.backupdb',):
            return Decision(False, False, 'backup')
        if not self.full_disk_access and (rel in ASKS or low.endswith('.photoslibrary')):
            return Decision(False, False, 'needs_full_disk_access')
        private = inherited_private or rel in PRIVATE or low.endswith(BUNDLES)
        if rel:
            rp = rel.split('/')
            private |= len(rp) >= 3 and '/'.join(rp[:2]) in ('library/containers', 'library/group containers')
        return Decision(True, not private)

    @staticmethod
    def contents_private(names):
        names = {n.lower() for n in names}
        return bool({'local state', 'places.sqlite'} & names)
