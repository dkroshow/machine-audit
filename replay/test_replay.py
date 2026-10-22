"""Behavioral acceptance at the ordinary Store -> Query boundary.

These scenarios are synthetic and make no claim about macOS source parsing.
"""
import os
from pathlib import Path
import tempfile
import unittest

from machine_audit.query import Query
from machine_audit.store import Store
from replay.scenarios import BASE_NS, SECOND, cpu_scenario, memory_scenario


class StoredReplay(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("MACHINE_AUDIT_TEST_ROOT", Path(__file__).parent / ".scratch"))
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="replay-", dir=root)
        self.store = Store(Path(self.temp.name) / "history.sqlite")
        self.query = Query(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, records):
        self.store.start_run(records[0].run_id, records[0].boot_id, BASE_NS)
        self.store.append(records)
        self.store.stop_run(records[0].run_id, records[-1].reading.end.wall_ns)

    def series(self, component, seconds=61):
        return self.query.series(BASE_NS, BASE_NS + seconds * SECOND, component=component)

    def test_equal_cpu_use_does_not_hide_extra_runnable_demand(self):
        """The host query distinguishes busy/unconstrained from contention evidence."""
        self.ingest(cpu_scenario(contended=False))
        first = self.series("cpu")
        busy = [r for r in first["rows"] if r["signal"] == "cpu.busy"]
        self.assertEqual(sum(r["total"] or 0 for r in busy), 24000)
        self.assertEqual({r["unit"] for r in busy}, {"ticks"})
        self.assertTrue(all(abs(r["mean_rate"] - 4) < 1e-9 for r in busy if r["total"] is not None))
        self.assertTrue(all(r["mean"] == 0 for r in first["rows"] if r["signal"] == "cpu.excess_runnable" and r["status"] in {"observed", "measured_zero"}))
        self.assertEqual({r["mean"] for r in first["rows"] if r["signal"] == "cpu.runnable" and r["status"] in {"observed", "measured_zero"}}, {4000})
        # A separate disposable store preserves the first scenario unchanged.
        second_store = Store(Path(self.temp.name) / "contended.sqlite")
        try:
            second_store.append(cpu_scenario(contended=True))
            answer = Query(second_store).series(BASE_NS, BASE_NS + 61 * SECOND, component="cpu")
            self.assertEqual(sum(r["total"] or 0 for r in answer["rows"] if r["signal"] == "cpu.busy"), 24000)
            demand = [r["mean"] for r in answer["rows"] if r["signal"] == "cpu.runnable" and r["status"] in {"observed", "measured_zero"}]
            self.assertEqual(max(demand), 12000)
            self.assertEqual(max(r["mean"] for r in answer["rows"] if r["signal"] == "cpu.excess_runnable" and r["status"] in {"observed", "measured_zero"}), 4)
            self.assertTrue(all(r["mean"] == 8 for r in answer["rows"] if r["signal"] == "cpu.capacity" and r["status"] in {"observed", "measured_zero"}))
            unavailable = [r for r in answer["rows"] if r["signal"] == "cpu.wait"]
            self.assertTrue(unavailable)
            self.assertTrue(all(r["status"] in {"unavailable", "unobserved"} and r["total"] is None for r in unavailable))
        finally:
            second_store.close()

    def test_missing_bins_do_not_overlap_a_covered_counter_window(self):
        """Series gap shading must not call a measured ten-second window missing."""
        self.ingest(cpu_scenario(contended=False))
        rows = [r for r in self.series("cpu")["rows"] if r["signal"] == "cpu.busy"]
        valid = [r for r in rows if r["status"] in {"observed", "measured_zero"}]
        missing = [r for r in rows if r["status"] == "unobserved"]
        self.assertTrue(valid)
        for gap in missing:
            for window in valid:
                self.assertFalse(max(gap["start_ns"], window["start_ns"]) < min(gap["end_ns"], window["end_ns"]),
                                 "A missing bin overlaps a counter window that was measured")

    def test_two_simultaneous_program_instances_keep_union_elapsed_time_and_peak(self):
        """The who boundary must not halve a concurrent program's two-CPU rate."""
        self.ingest(cpu_scenario(contended=True))
        answer = self.query.who(BASE_NS, BASE_NS + 60 * SECOND, signal="process.cpu", component="cpu")
        self.assertEqual(len(answer["rows"]), 1)
        row = answer["rows"][0]
        self.assertEqual(row["label"], "Example worker")
        self.assertEqual(row["instance_count"], 2)
        self.assertEqual(row["total"], 120 * SECOND)
        self.assertEqual(row["covered_s"], 60)
        self.assertAlmostEqual(row["mean_rate"], 2)
        self.assertAlmostEqual(row["peak_rate"], 2)
        self.assertEqual(row["above_s"]["2.0"], 60)

    def test_memory_movement_stalls_and_recovery_keep_units_and_distinct_evidence(self):
        """Recovery must not erase earlier movement or equate pressure with stalls."""
        self.ingest(memory_scenario())
        answer = self.series("memory", 41)
        swap = [r for r in answer["rows"] if r["signal"] == "memory.swapins"]
        stalls = [r for r in answer["rows"] if r["signal"] == "memory.psi.some"]
        self.assertEqual(sum(r["total"] or 0 for r in swap), 80)
        self.assertEqual({r["unit"] for r in swap}, {"pages"})
        self.assertEqual(sum(r["total"] or 0 for r in stalls), 2_000_000)
        self.assertEqual({r["unit"] for r in stalls}, {"us"})
        available = [r for r in answer["rows"] if r["signal"] == "memory.available" and r["status"] in {"observed", "measured_zero"}]
        self.assertEqual(min(r["min"] for r in available), 500_000_000)
        self.assertEqual(max(available, key=lambda r:r["end_ns"])["last"], 7_000_000_000)
        recovery = self.query.around(BASE_NS + 30 * SECOND, 10 * SECOND, component="memory")
        self.assertTrue(recovery["series"]["rows"])
        self.assertTrue(all(r["total"] is None for r in answer["rows"] if r["signal"] == "memory.macos_stall"))

    def test_overlapping_memory_footprints_remain_separate_and_have_no_remainder(self):
        """The workload boundary must not invent a six-GB program or host remainder."""
        self.ingest(memory_scenario())
        answer = self.query.who(BASE_NS, BASE_NS + 40 * SECOND, signal="process.memory", component="memory")
        self.assertEqual(len(answer["rows"]), 2)
        self.assertEqual([r["mean"] for r in answer["rows"]], [3_000_000_000, 3_000_000_000])
        self.assertTrue(all(r["entity"].startswith("process:") for r in answer["rows"]))
        self.assertNotIn("remainder", answer)


if __name__ == "__main__":
    unittest.main()
