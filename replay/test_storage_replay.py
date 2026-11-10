"""Storage questions through the normal scanner ingestion and shared Query API."""
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from machine_audit.query import Query
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.store import Store
from replay.scenarios import BASE_NS, SECOND
from replay.storage_scenario import GB, VOLUME_ID, seed_storage, write_snapshot


class StorageReplay(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("MACHINE_AUDIT_TEST_ROOT", Path(__file__).parent / ".scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="storage-replay-", dir=scratch)
        self.store = Store(Path(self.temp.name) / "history.sqlite")
        self.query = Query(self.store, storage=seed_storage(self.temp.name))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_shared_space_reconciles_and_drilldown_keeps_the_same_accounting(self):
        """Folders' physical space plus the residual must equal the same snapshot's used
        space; rows that share blocks state the shared amount, so they add up too."""
        volume = self.query.storage_query("volumes")["rows"][0]
        self.assertTrue(volume["valid"])
        self.assertEqual(volume["listed"], 9*GB)
        self.assertEqual(volume["physical"], 7*GB)
        self.assertEqual(volume["not_placed"], 4*GB)
        self.assertEqual(volume["physical"]+volume["not_placed"], volume["volume_used"])
        answer = self.query.storage_query("children", volume_id=VOLUME_ID)
        children = answer["rows"]
        self.assertEqual({row["label"]: row["physical"] for row in children},
                         {"Example projects": 5*GB, "Shared copy": 4*GB})
        self.assertEqual(answer["folder"]["shared_between_rows"], 2*GB)
        self.assertEqual(sum(row["physical"] for row in children)-answer["folder"]["shared_between_rows"], volume["physical"])
        projects = next(row for row in children if row["label"] == "Example projects")
        data = self.query.storage_query("children", volume_id=VOLUME_ID, parent_id=projects["id"])["rows"]
        self.assertEqual(data[0]["label"], "Example data")
        self.assertEqual(data[0]["physical"], projects["physical"])

    def test_free_space_is_the_live_reading_with_purgeable_beside_it(self):
        """A page showed the map scan's free space, read at the scan's start, as if
        current: 20 GB below macOS's own figure. The live minute reading wins, and
        purgeable is what Finder adds on top."""
        from machine_audit.collector import Collector
        from machine_audit.model import Clock
        from replay.storage_scenario import capacity_reading
        mapped = self.query.storage_query("volumes")["rows"][0]
        self.assertEqual((mapped["available"], mapped["capacity_source"], mapped["purgeable"]), (9*GB, "map", None))
        later = mapped["observed_ns"] + SECOND
        collector = Collector(self.store, boot_id="synthetic-boot", run_id="synthetic",
                              clock=lambda: Clock(later, later, later))
        self.addCleanup(collector.close)
        collector.ingest(capacity_reading(later))
        live = Query(self.store, collector, storage=self.query.storage).storage_query("volumes")["rows"][0]
        self.assertEqual((live["available"], live["purgeable"], live["available_important"], live["capacity_source"]),
                         (8*GB, 2_500_000_000, 10_500_000_000, "live"))
        self.assertEqual(live["capacity_observed_ns"], later)

    def test_growth_keeps_original_parent_and_child_points_with_overlap_explicit(self):
        """The shared query must preserve, not suppress, nested original growth."""
        answer = self.query.storage_query("growth", volume_id=VOLUME_ID,
                                          start_ns=BASE_NS, end_ns=BASE_NS+60*SECOND)
        changed = [row for row in answer["rows"] if row["change"]]
        self.assertEqual({row["label"]: row["change"] for row in changed},
                         {"Synthetic volume": GB, "Example projects": GB, "Example data": GB})
        for row in changed:
            self.assertEqual(row["metric"], "physical")
            self.assertEqual(row["start_ns"], BASE_NS)
            self.assertEqual(row["end_ns"], BASE_NS+40*SECOND)
        self.assertTrue(answer["coverage"]["nested_totals_overlap"])
        self.assertEqual(answer["coverage"]["growth_grouping"], "original_totals_with_overlap")

    def test_storage_cursors_continue_without_offset_and_keep_nested_rows(self):
        """The shared API must retain original rows across bounded cursor pages."""
        first = self.query.storage_query("children", volume_id=VOLUME_ID, limit=1)
        self.assertTrue(first["truncated"])
        self.assertIsInstance(first["next_cursor"], str)
        second = self.query.storage_query("children", volume_id=VOLUME_ID, limit=1, cursor=first["next_cursor"])
        self.assertEqual(len(second["rows"]), 1)
        self.assertNotEqual(first["rows"][0]["id"], second["rows"][0]["id"])
        self.assertFalse(second["truncated"])
        with self.assertRaises(ValueError):
            self.query.storage_query("children", volume_id=VOLUME_ID, offset=1)
        growth = self.query.storage_query("growth", volume_id=VOLUME_ID, limit=2,
                                         start_ns=BASE_NS, end_ns=BASE_NS+60*SECOND)
        self.assertTrue(growth["truncated"])
        self.assertEqual(growth["coverage"]["growth_grouping"], "original_totals_with_overlap")
        more = self.query.storage_query("growth", volume_id=VOLUME_ID, limit=2,
                                       start_ns=BASE_NS, end_ns=BASE_NS+60*SECOND, cursor=growth["next_cursor"])
        self.assertFalse(more["truncated"])
        self.assertIn("Example data", [row["label"] for row in more["rows"]])

    def test_invalid_or_unsupported_physical_accounting_withholds_physical(self):
        """Too-small totals and unsupported filesystems must not fabricate agreement."""
        mapping = MapStore(Path(self.temp.name) / "scanner.sqlite")
        volume = Volume(VOLUME_ID, "Synthetic volume", "", "hfs", 1, 20*GB, 15*GB, 15*GB, 5*GB)
        try:
            write_snapshot(mapping, volume, BASE_NS+120*SECOND, growth=GB)
            bad = self.query.storage_query("volumes")["rows"][0]
            self.assertEqual(bad["status"], "invalid")
            self.assertIsNone(bad["physical"])
            self.assertIsNone(bad["not_placed"])
            self.assertEqual(bad["listed"], 9*GB)
            self.assertTrue(all(row["physical"] is None for row in self.query.storage_query("children")["rows"]))
            write_snapshot(mapping, replace(volume, fs="ext4", used=11*GB), BASE_NS+180*SECOND, growth=GB)
            unsupported = self.query.storage_query("volumes")["rows"][0]
            self.assertEqual(unsupported["status"], "unavailable")
            self.assertIsNone(unsupported["physical"])
        finally:
            mapping.close()


if __name__ == "__main__":
    unittest.main()
