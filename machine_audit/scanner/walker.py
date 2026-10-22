"""One metadata walk. The store, not Python objects, holds the growing map."""
from __future__ import annotations
from dataclasses import dataclass
import errno
import os
import stat
from .accounting import Stream


@dataclass(frozen=True)
class Node:
    id: str
    parent_id: str | None
    label: str | None
    kind: str
    listed: int
    uses: int
    status: str
    path: str | None
    mtime_ns: int


def shown_path(volume, path):
    """Policy sees the Data volume through its firmlinked root paths."""
    return '/' + os.path.relpath(path, volume.mount) if volume.mount == '/System/Volumes/Data' else path


class Walker:
    def __init__(self, volume, policy, sink, blocks, fs=None, grain='files', large_file_bytes=100_000_000):
        self.volume, self.policy, self.sink, self.blocks, self.fs = volume, policy, sink, blocks, fs
        self.grain, self.large_file_bytes = grain, large_file_bytes
        self.entries = 0
        self.listed=set()
        self.gaps = set()
        self.sharing_complete = True

    def _open_directory(self,path):
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC
        # macOS O_NOFOLLOW_ANY rejects intermediate links and cannot combine
        # with O_NOFOLLOW. Use the identical boundary for replay identity reads.
        flags|=0x20000000 if self.fs else os.O_NOFOLLOW
        return os.open(path,flags)

    def directory_identity(self,path):
        try:fd=self._open_directory(path)
        except PermissionError:
            self.gaps.add('denied');return None
        except OSError as exc:
            if exc.errno not in (errno.ENOENT,errno.ENOTDIR,errno.ELOOP):
                self.gaps.add('directory_unreadable')
            return None
        try:
            info=os.fstat(fd)
            root_device=os.stat(self.volume.mount,follow_symlinks=False).st_dev
            return str(info.st_ino) if info.st_dev==root_device else None
        except OSError:
            self.gaps.add('directory_unreadable');return None
        finally:os.close(fd)

    def walk(self, root=None, parent_id=None, inherited_private=False, recursive=True):
        root = root or self.volume.mount
        try:
            rootstat = os.stat(root, follow_symlinks=False)
        except OSError:
            self.gaps.add('root_unreadable')
            return
        if not stat.S_ISDIR(rootstat.st_mode):
            raise ValueError('scan_root_must_be_directory')
        stack = [(root, parent_id, inherited_private,recursive)]
        while stack:
            path, parent, hidden, descend_all = stack.pop()
            try:
                st = os.stat(path, follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode) or st.st_dev != rootstat.st_dev:
                    continue
                identity = str(st.st_ino)
                decision = self.policy.decide(shown_path(self.volume, path), hidden)
                names = decision.names and not hidden
                label = os.path.basename(path) if names else None
                if path == self.volume.mount:
                    label = self.volume.label
                if not decision.descend:
                    self.gaps.add(decision.reason)
                    self.sink.directory(Node(identity, parent, label, 'directory', 0, 0, decision.reason, path if names else None, st.st_mtime_ns))
                    continue
                fd = self._open_directory(path)
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                        self.gaps.add('directory_moved')
                        continue
                    # Inspect fixed marker names before any content name reaches the sink.
                    private_marker = False
                    for marker in ('Local State', 'places.sqlite'):
                        try:
                            os.stat(marker, dir_fd=fd, follow_symlinks=False)
                            private_marker = True
                        except FileNotFoundError:
                            pass
                        except OSError:
                            private_marker = True
                    names &= not private_marker
                    self.sink.begin_directory(identity)
                    own_listed = own_uses = st.st_blocks * 512
                    children = []
                    outcome = 'observed'
                    seen_links = set()
                    direct_count=0
                    with os.scandir(fd) as listing:
                        for entry in listing:
                            self.entries += 1
                            direct_count+=1
                            if self.entries % 1000 == 0:
                                self.sink.progress(self.entries)
                            try:
                                child = entry.stat(follow_symlinks=False)
                                if child.st_dev != rootstat.st_dev:
                                    self.gaps.add('other_volume')
                                    continue
                                if getattr(child, 'st_flags', 0) & 0x40000000:
                                    self.gaps.add('cloud_placeholder')
                                    continue
                                if stat.S_ISDIR(child.st_mode):
                                    children.append((os.path.join(path, entry.name), identity, not names,True))
                                    continue
                                alloc = child.st_blocks * 512
                                # Use one global inode home for hard links, chosen deterministically.
                                shared = None
                                if stat.S_ISREG(child.st_mode) and self.fs:
                                    try:
                                        shared = self.fs.sharing(fd, entry.name, child)
                                    except OSError:
                                        self.sharing_complete = False
                                        self.gaps.add('sharing_unreadable')
                                uses = alloc
                                if child.st_nlink > 1 and child.st_ino in seen_links:
                                    continue
                                if child.st_nlink > 1:
                                    seen_links.add(child.st_ino)
                                if shared is not None or child.st_nlink > 1:
                                    stream, private, partial = shared or (child.st_ino, None, False)
                                    self.sink.holder(child.st_ino, identity, stream, alloc, private, partial)
                                    own_listed += alloc
                                    uses = 0
                                    if shared and self.fs:
                                        self.blocks.add(Stream(stream, identity, alloc, private, partial), self.fs.extents(fd, entry.name, child))
                                else:
                                    own_listed += alloc
                                own_uses += uses
                                if self.grain == 'files' or alloc >= self.large_file_bytes or shared or child.st_nlink > 1:
                                    self.sink.file(str(child.st_ino), identity, entry.name if names else None, alloc, child.st_mtime_ns)
                            except OSError:
                                self.gaps.add('entry_unreadable')
                                outcome = 'partial'
                    if os.stat(path, follow_symlinks=False).st_ino != st.st_ino:
                        self.gaps.add('directory_moved')
                        outcome = 'partial'
                    self.sink.directory(Node(identity, parent, label, 'private' if not names else 'directory',
                                             own_listed, own_uses, outcome, path if names else None, st.st_mtime_ns))
                    self.sink.listed(identity,direct_count)
                    self.listed.add(identity)
                    # An hourly update re-lists one changed folder. New folders
                    # still need their initial recursive map; unchanged descendants
                    # retain their existing sizes without a filesystem walk.
                    if descend_all:
                        stack.extend(children)
                    else:
                        self.sink.flush()
                        present=set()
                        for child_path,child_parent,private,_ in children:
                            child_stat=os.stat(child_path,follow_symlinks=False)
                            child_id=str(child_stat.st_ino)
                            present.add(child_id)
                            decision=self.policy.decide(child_path,private)
                            known=self.sink.relocate(child_id,child_parent,os.path.basename(child_path),child_path,
                                                      private or not decision.names)
                            if not known:stack.append((child_path,child_parent,private,True))
                        if outcome=='observed':self.sink.forget_children(identity,present)
                finally:
                    os.close(fd)
            except PermissionError:
                self.gaps.add('denied')
                self.sink.directory(Node(str(st.st_ino), parent, None if hidden else os.path.basename(path),
                                         'directory', 0, 0, 'denied', None if hidden else path, st.st_mtime_ns))
            except OSError:
                self.gaps.add('directory_unreadable')
