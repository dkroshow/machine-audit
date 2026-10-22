"""Original folder totals remain available across pages and historical moves."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapQuery, MapStore
from machine_audit.scanner.walker import Node

GB = 1_000_000_000
T0, T1, T2 = (n * GB for n in (100, 200, 300))
SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)


class GrowthValuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.store = MapStore(Path(self.temp.name) / 'scanner.sqlite',
                              Budget(self.temp.name, free_margin_bytes=0))
        self.query = MapQuery(self.store.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self, at, nodes, volume_id='v', fs='hfs'):
        volume = Volume(volume_id, 'Volume', '/synthetic', fs, 1,
                        1000 * GB, 100 * GB, 100 * GB, 900 * GB)
        self.store.prepare(volume)
        for identity, parent, label, own in nodes:
            self.store.directory(Node(identity, parent, label, 'directory', own, 0,
                                      'observed', None, 0))
        with patch('machine_audit.scanner.store.time.time_ns', return_value=at):
            self.store.finish_volume(volume, Blocks(), [], full=True, lean=True)

    def answer(self, start=T0, end=T1, limit=100, volume_id='v'):
        return self.query.query('growth', volume_id=volume_id, start_ns=start,
                                end_ns=end, limit=limit)

    def single_row_pages(self, start=T0, end=T1, volume_id='v'):
        rows = []
        cursor = None
        for _ in range(20):
            page = self.query.query('growth', volume_id=volume_id, start_ns=start,
                                    end_ns=end, limit=1, cursor=cursor)
            self.assertLessEqual(len(page['rows']), 1)
            self.assertEqual(page['coverage']['growth_grouping'], 'original_totals_with_overlap')
            self.assertTrue(page['coverage']['nested_totals_overlap'])
            self.assertFalse(page['coverage']['global_growth_complete'])
            rows.extend(page['rows'])
            if not page['truncated']:
                self.assertEqual(len({row['id'] for row in rows}), len(rows))
                return {row['id']: row for row in rows}
            self.assertTrue(page['next_cursor'])
            cursor = page['next_cursor']
        self.fail('cursor did not make bounded progress')

    def test_legitimate_parent_changes_and_zero_rows_survive_every_page_size(self):
        # These four examples caught information loss from ancestor suppression.
        cases = [('small', 50_000_000, 0), ('unexplained', 9 * GB, GB),
                 ('opposite', 11 * GB, -GB), ('cancellation', GB, -GB)]
        for volume_id, own_change, child_change in cases:
            with self.subTest(case=volume_id):
                for at, step in ((T0, 0), (T1, 1)):
                    self.snapshot(at, [('parent', None, 'Projects', 10 * GB + step * own_change),
                                       ('child', 'parent', 'Transcripts', 10 * GB + step * child_change)], volume_id)
                rows = {row['id']: row for row in self.answer(volume_id=volume_id)['rows']}
                self.assertEqual(set(rows), {'parent', 'child'})
                self.assertEqual(self.single_row_pages(volume_id=volume_id), rows)
                for identity, initial, change in (
                        ('parent', 20 * GB, own_change + child_change),
                        ('child', 10 * GB, child_change)):
                    row = rows[identity]
                    self.assertEqual((row['start_bytes'], row['end_bytes'], row['change']),
                                     (initial, initial + change, change))
                    self.assertEqual((row['start_ns'], row['end_ns']),
                                     (T0, T1 if change else T0))
                    self.assertEqual((row['metric'], row['status']), ('uses', 'observed'))
                    self.assertEqual((row['hierarchy_observed_ns'], row['hierarchy_status']),
                                     (T1, 'observed'))
                self.assertEqual((rows['parent']['parent_id'], rows['parent']['parent_label']), (None, None))
                self.assertEqual((rows['child']['parent_id'], rows['child']['parent_label']), ('parent', 'Projects'))

    def test_zero_unknown_and_missing_identities_do_not_become_each_other(self):
        self.snapshot(T0, [('root', None, 'Root', 2 * GB),
                           ('steady', 'root', 'Steady', 2 * GB),
                           ('zero', 'root', 'Zero', GB),
                           ('deleted', 'root', 'Deleted', 2 * GB)])
        self.snapshot(T1, [('root', None, 'Root', 2 * GB),
                           ('steady', 'root', 'Steady', 2 * GB),
                           ('zero', 'root', 'Zero', 0)])
        self.snapshot(T2, [('root', None, 'Root', 2 * GB),
                           ('steady', 'root', 'Steady', 2 * GB),
                           ('zero', 'root', 'Zero', 0),
                           ('later', 'root', 'Later', 2 * GB)])
        rows = self.single_row_pages()
        self.assertEqual(set(rows), {'root', 'steady', 'zero', 'deleted', 'later'})
        self.assertEqual((rows['zero']['start_bytes'], rows['zero']['end_bytes'], rows['zero']['change']), (GB, 0, -GB))
        self.assertEqual((rows['steady']['start_ns'], rows['steady']['end_ns'], rows['steady']['change']), (T0, T0, 0))
        self.assertEqual((rows['deleted']['start_bytes'], rows['deleted']['end_bytes'], rows['deleted']['change']), (2 * GB, None, None))
        self.assertEqual((rows['deleted']['status'], rows['deleted']['hierarchy_status']), ('unobserved', 'not_observed'))
        self.assertIsNone(rows['deleted']['parent_id'])
        self.assertEqual((rows['later']['start_bytes'], rows['later']['end_bytes'], rows['later']['change']), (None, None, None))
        self.assertEqual((rows['later']['status'], rows['later']['hierarchy_status']), ('unobserved', 'unobserved'))
        self.assertIsNone(rows['later']['parent_id'])

    def test_metric_change_keeps_endpoints_but_withholds_delta(self):
        self.snapshot(T0, [('parent', None, 'Projects', 10 * GB), ('child', 'parent', 'Transcripts', 10 * GB)])
        self.snapshot(T1, [('parent', None, 'Projects', 10 * GB), ('child', 'parent', 'Transcripts', 11 * GB)], fs='unsupported')
        rows = self.single_row_pages()
        for identity, initial, final in (('parent', 20 * GB, 21 * GB), ('child', 10 * GB, 11 * GB)):
            row = rows[identity]
            self.assertEqual((row['start_bytes'], row['end_bytes']), (initial, final))
            self.assertEqual((row['start_ns'], row['end_ns']), (T0, T1))
            self.assertEqual((row['start_metric'], row['end_metric']), ('uses', 'listed'))
            self.assertEqual((row['metric'], row['status'], row['change']), ('listed', 'unobserved', None))
        for row in self.answer(start=0, end=T0)['rows']:
            self.assertEqual((row['start_metric'], row['end_metric']), (None, 'uses'))
        for row in self.answer(start=0, end=T0 - 1)['rows']:
            self.assertEqual((row['start_metric'], row['end_metric']), (None, None))

    def test_later_move_preserves_historical_parent_and_invalidates_old_cursor(self):
        def nodes(parent, size):
            return [('root', None, 'Root', 0), ('left', 'root', 'Old parent', 2 * GB),
                    ('right', 'root', 'New parent', 2 * GB), ('leaf', parent, 'Leaf', size)]
        self.snapshot(T0, nodes('left', 4 * GB))
        self.snapshot(T1, nodes('left', 5 * GB))
        cursor = self.answer(limit=1)['next_cursor']
        self.snapshot(T2, nodes('right', 5 * GB))
        direct = next(row for row in self.answer()['rows'] if row['id'] == 'leaf')
        self.assertEqual(direct['parent_id'], 'left')
        historical = self.single_row_pages()['leaf']
        self.assertEqual((historical['parent_id'], historical['parent_label'], historical['hierarchy_observed_ns']),
                         ('left', 'Old parent', T1))
        current = self.single_row_pages(end=T2)['leaf']
        self.assertEqual((current['parent_id'], current['parent_label'], current['hierarchy_observed_ns']),
                         ('right', 'New parent', T2))
        self.assertEqual((current['start_ns'], current['end_ns']), (T0, T1))
        with self.assertRaisesRegex(ValueError, 'stale_or_incompatible'):
            self.query.query('growth', volume_id='v', start_ns=T0, end_ns=T1, limit=1, cursor=cursor)
        before = self.answer(start=0, end=T0 - 1)['rows']
        self.assertTrue(all(row['parent_id'] is None and row['hierarchy_status'] == 'unobserved' for row in before))
        self.assertTrue(all(row['hierarchy_observed_ns'] is None for row in before))
        # Retained size evidence can predate the first hierarchy observation.
        with self.store.write():
            self.store.db.executemany('INSERT INTO points VALUES(?,?,?,?,?,?,?)',
                                      [('v', 'leaf', at, 'Earlier leaf', 'uses', size, 'observed')
                                       for at, size in ((10, 3 * GB), (20, 4 * GB))])
        earlier = next(row for row in self.answer(start=10, end=20)['rows'] if row['id'] == 'leaf')
        self.assertEqual((earlier['start_bytes'], earlier['end_bytes'], earlier['change']), (3 * GB, 4 * GB, GB))
        self.assertEqual((earlier['parent_id'], earlier['parent_label'], earlier['hierarchy_status']), (None, None, 'unobserved'))

    def test_untracked_parent_name_is_explicitly_unobserved(self):
        self.snapshot(T0, [('root', None, 'Root', 2 * GB), ('child', 'root', 'Child', 2 * GB)])
        self.snapshot(T1, [('root', None, 'Root', 2 * GB), ('small', 'root', 'Untracked parent', 0),
                           ('child', 'small', 'Child', GB // 2)])
        row = self.single_row_pages()['child']
        self.assertEqual(row['parent_id'], 'small')
        self.assertIsNone(row['parent_label'])
        self.assertEqual((row['hierarchy_status'], row['hierarchy_observed_ns']), ('parent_unobserved', T1))


if __name__ == '__main__':
    unittest.main()
