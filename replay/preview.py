"""Serve deterministic synthetic records through the real application API.

No sources are started. This program never reads the host's metrics or folders.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from machine_audit.collector import Collector
from machine_audit.model import Clock
from machine_audit.query import Query
from machine_audit.serve import make_server
from machine_audit.store import Store
from replay.scenarios import BASE_NS, SECOND, all_components, crowded_dashboard
from replay.storage_scenario import seed_storage


def seed(store, crowded=False):
    end_ns = time.time_ns()
    offset = end_ns - 60 * SECOND - BASE_NS
    collector = Collector(store, boot_id="synthetic-boot", run_id="synthetic",
                          clock=lambda: Clock(end_ns, 60 * SECOND, 60 * SECOND))
    for record in sorted(crowded_dashboard() if crowded else all_components(), key=lambda record: record.reading.end.wall_ns):
        reading = record.reading
        shift = lambda clock: replace(clock, wall_ns=clock.wall_ns + offset)
        fields = tuple(replace(field,
                               start=shift(field.start) if field.start else None,
                               end=shift(field.end) if field.end else None)
                       for field in reading.fields)
        collector.ingest(replace(reading, start=shift(reading.start), end=shift(reading.end), fields=fields))
    if not collector.flush():
        raise RuntimeError("synthetic fixture save failed")
    return collector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Unused loopback port; 0 chooses one")
    parser.add_argument("--ready-file", type=Path, required=True,
                        help="Private scratch file for the test browser's temporary URL")
    parser.add_argument("--storage-state", choices=("shared", "invalid", "unsupported", "paged", "growth"), default="shared")
    parser.add_argument("--dashboard-state", choices=("normal", "crowded"), default="normal")
    args = parser.parse_args()
    root = Path(os.environ.get("MACHINE_AUDIT_TEST_ROOT", Path(__file__).parent / ".scratch"))
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preview-", dir=root) as directory:
        store = Store(Path(directory) / "history.sqlite")
        collector = seed(store, crowded=args.dashboard_state == "crowded")
        token = secrets.token_urlsafe(32)
        storage_end = int(time.time())*SECOND
        storage = seed_storage(directory, storage_end, args.storage_state, start_ns=storage_end-7200*SECOND)
        server = make_server(Query(store, collector, storage), token, port=args.port,
                             dashboard=Path(__file__).resolve().parent.parent / "dashboard")
        receipt = {"origin": f"http://127.0.0.1:{server.server_port}", "token": token,
                   "synthetic": True, "pid": os.getpid()}
        if args.storage_state == "growth":
            receipt["storage_interval"] = {"start": storage_end/SECOND-7200, "end": storage_end/SECOND-30}
        fd = os.open(args.ready_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream)
        print("Synthetic replay is ready; browser connection details are in the private ready file.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            collector.close()
            store.close()


if __name__ == "__main__":
    main()
