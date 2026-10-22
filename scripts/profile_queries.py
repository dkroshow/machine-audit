"""Profile the dashboard queries over the last hour of a store, read-only.

Prints wall time, answer size and the top cumulative functions per query. The
output names functions and package files only, never row content, so it is safe
to share. Run on the box that holds the store:

    python3 scripts/profile_queries.py [path/to/readings.sqlite] [limit]
"""
import cProfile
import io
import json
from pathlib import Path
import pstats
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from machine_audit.config import Config  # noqa: E402
from machine_audit.query import Query  # noqa: E402
from machine_audit.store import Store  # noqa: E402

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Config.load().store_path
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 600
query = Query(Store(path, read_only=True))
end = time.time_ns()
start = end - 3600 * 10**9
# `now` answers from the collector's memory, so it has no offline shape here.
cases = {
    "coverage 1h": lambda: query.coverage(start, end, limit=limit),
    "series 1h cpu": lambda: query.series(start, end, component="cpu", limit=limit),
    "who 1h cpu": lambda: query.who(start, end, component="cpu", limit=limit),
    "who 1h memory": lambda: query.who(start, end, component="memory", limit=limit),
}
for name, call in cases.items():
    call()  # warm the page cache once; the profiled run is the warm one
    profile = cProfile.Profile()
    t0 = time.perf_counter()
    answer = profile.runcall(call)
    seconds = time.perf_counter() - t0
    size = len(json.dumps(answer, separators=(",", ":")))
    print(f"\n== {name}: {seconds:.2f} s, {size/1024:.0f} KB, rows {len(answer.get('rows', []))}, "
          f"gaps {len(answer.get('gaps', []))}, truncated {answer.get('truncated')}")
    text = io.StringIO()
    pstats.Stats(profile, stream=text).sort_stats("cumulative").print_stats(25)
    for line in text.getvalue().splitlines():
        columns, _, location = line.rpartition(" ")
        if "/" in location:  # keep the file's name, never its path
            line = columns + " " + location.rsplit("/", 1)[-1]
        print(line)
