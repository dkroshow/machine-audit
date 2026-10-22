"""Synthetic scanner observations through the ordinary MapStore boundary.

The extent addresses below are invented small coordinates. No physical address,
real folder name or file content is read or retained.
"""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from machine_audit.scanner.accounting import Blocks, Stream
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.scanner.walker import Node
from replay.scenarios import BASE_NS, SECOND

GB = 1_000_000_000
VOLUME_ID = "00000000-0000-4000-8000-000000000001"


def write_snapshot(store, volume, at_ns, *, growth=0, complete=True, extra_folders=0):
    store.prepare(volume, full=True, lean=True)
    # Two four-GB files share two GB of blocks. Both the child and its ancestor
    # retain the unshared GB gained in the second snapshot.
    store.directory(Node("1", None, "Synthetic volume", "directory", 0, 0, "observed", None, at_ns))
    store.directory(Node("2", "1", "Example projects", "directory", 0, 0, "observed", None, at_ns))
    store.directory(Node("3", "2", "Example data", "directory", 4*GB+growth, 4*GB+growth, "observed", None, at_ns))
    store.directory(Node("4", "1", "Shared copy", "directory", 4*GB, 4*GB, "observed", None, at_ns))
    for index in range(extra_folders):
        store.directory(Node(str(1000+index), "1", f"Page folder {index+1:03d}", "directory",
                             GB, GB, "observed", None, at_ns))
    store.file("file101", "3", "Example shared data", 4*GB, at_ns)
    store.holder(101, "3", 10, 4*GB, None, False)
    store.holder(102, "4", 20, 4*GB, 2*GB, True)
    blocks = Blocks()
    blocks.add(Stream(10, "3", 4*GB, None, False), [(0, 4*GB)])
    blocks.add(Stream(20, "4", 4*GB, 2*GB, True), [(2*GB, 4*GB)])
    with patch("machine_audit.scanner.store.time.time_ns", return_value=at_ns):
        return store.finish_volume(volume, blocks, [] if complete else ["permission_denied"],
                                   full=True, lean=True, complete=complete)


def seed_storage(directory, end_ns=BASE_NS + 60*SECOND, state="shared", start_ns=None):
    store = MapStore(Path(directory) / "scanner.sqlite")
    if state == "growth":
        try:
            seed_original_growth(store, end_ns, start_ns)
        finally:
            store.close()
        return MapQuery(Path(directory) / "scanner.sqlite")
    volume = Volume(VOLUME_ID, "Synthetic volume", "", "hfs", 1, 20*GB, 10*GB, 10*GB, 10*GB)
    extra_folders = 201 if state == "paged" else 0
    if extra_folders:
        volume = replace(volume, capacity=500*GB, used=(10+extra_folders)*GB,
                         free=(490-extra_folders)*GB, available=(490-extra_folders)*GB)
    write_snapshot(store, volume, end_ns-60*SECOND if start_ns is None else start_ns, extra_folders=extra_folders)
    write_snapshot(store, replace(volume, used=volume.used+GB, free=volume.free-GB, available=volume.available-GB),
                   end_ns-20*SECOND, growth=GB, extra_folders=extra_folders)
    if state == "invalid":
        write_snapshot(store, replace(volume, used=5*GB), end_ns-10*SECOND, growth=GB)
    elif state == "unsupported":
        write_snapshot(store, replace(volume, fs="ext4", used=11*GB), end_ns-10*SECOND, growth=GB)
    store.close()
    return MapQuery(Path(directory) / "scanner.sqlite")


def seed_original_growth(store, end_ns, start_ns):
    """Small changes, opposing changes and missing evidence, never GB files.

    The last snapshot moves a child after the historical test interval. It
    must not change the parent shown for that earlier interval.
    """
    start_ns = end_ns-7200*SECOND if start_ns is None else start_ns
    for phase, at_ns in enumerate((start_ns, end_ns-40*SECOND, end_ns-20*SECOND)):
        later = phase > 0
        nodes = [
            ("1", None, "Synthetic volume", 0),
            ("10", "1", "Small changes", 2*GB + (50_000_000 if later else 0)),
            ("20", "1", "Growing parent", (29 if later else 20)*GB),
            ("21", "30" if phase == 2 else "20", "Growing child", (11 if later else 10)*GB),
            ("30", "1", "Parent with shrinking child", (31 if later else 20)*GB),
            ("31", "30", "Shrinking child", (9 if later else 10)*GB),
            ("40", "1", "Unchanged parent", (21 if later else 20)*GB),
            ("41", "40", "Other shrinking child", (9 if later else 10)*GB),
        ]
        if phase == 0:
            nodes.append(("50", "1", "Missing folder", 2*GB))
        total = sum(size for _, _, _, size in nodes)
        volume = Volume(VOLUME_ID, "Synthetic volume", "", "hfs", 1,
                        200*GB, 190*GB-total, 190*GB-total, total+10*GB)
        store.prepare(volume, full=True, lean=True)
        for identity, parent, label, size in nodes:
            store.directory(Node(identity, parent, label, "directory", size, size,
                                 "observed", None, at_ns))
        with patch("machine_audit.scanner.store.time.time_ns", return_value=at_ns):
            store.finish_volume(volume, Blocks(), [], full=True, lean=True, complete=True)

    # A separate volume keeps the legitimate-change examples comparable while
    # exposing a real accounting transition through the same stored API.
    for at_ns, fs, size in ((start_ns, "hfs", 4*GB), (end_ns-40*SECOND, "ext4", 6*GB)):
        volume = Volume("00000000-0000-4000-8000-000000000002", "Uses to Listed", "", fs, 2,
                        20*GB, 10*GB, 10*GB, 10*GB)
        store.prepare(volume, full=True, lean=True)
        store.directory(Node("1", None, "Accounting change", "directory", size, size,
                             "observed", None, at_ns))
        with patch("machine_audit.scanner.store.time.time_ns", return_value=at_ns):
            store.finish_volume(volume, Blocks(), [], full=True, lean=True, complete=True)
