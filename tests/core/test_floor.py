"""The retention floors at the cap. Fixtures are synthetic: one reading an hour for 16 days."""
from itertools import groupby
from pathlib import Path
import shutil
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.query import Query
from machine_audit.store import FLOOR_UNMET,Store

S=10**9
HOUR=3600*S
DAYS=16
NEWEST=(DAYS*24-1)*HOUR
FINE=NEWEST-48*HOUR
HOURS=NEWEST-14*24*HOUR


def reading(n):
    c=Clock(n*HOUR,n*HOUR,n*HOUR)
    return Record(Reading('synthetic.host','cpu',c,c,tuple(Field(f'cpu.f{i}',float(n+i),'count') for i in range(8))),'boot','run',n+1,c.wall_ns,c)


class FloorTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root);self.stores=[]
        self.base=self.store('base')
        self.base.append([reading(n) for n in range(DAYS*24)])
        self.base.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def tearDown(self):
        for store in self.stores:store.close()
        self.tmp.cleanup()

    def store(self,name,limit=10**9):
        root=Path(self.tmp.name)/name;root.mkdir(exist_ok=True)
        store=Store(root/'readings.sqlite',Budget(root,limit_bytes=limit,free_margin_bytes=0))
        self.stores.append(store)
        return store

    def copy(self,name,limit):
        root=Path(self.tmp.name)/name;root.mkdir()
        shutil.copy(self.base.path,root/'readings.sqlite')
        return self.store(name,limit)

    @staticmethod
    def oldest(store):
        tiers={resolution:store.db.execute('SELECT MIN(bucket_ns) FROM summaries WHERE resolution=?',(resolution,)).fetchone()[0] for resolution in ('minute','15-minute','hour')}
        return store.db.execute('SELECT MIN(wall_ns) FROM readings').fetchone()[0],tiers

    def retention(self,store):
        return [(kind,reason) for kind,reason in store.db.execute('SELECT kind,reason FROM retention ORDER BY rowid')]

    def test_at_the_cap_old_hour_rows_go_before_the_last_48_hours_of_fine_detail(self):
        # Hour rows filled Big Mac's budget and left 13 minutes of raw readings: strict tier
        # order pruned every finer tier before any hour row. Now everything older than 48 h
        # goes first, then hour rows older than 14 days; only then do the floors give, raw
        # first, and each step says so.
        store=self.base
        while FLOOR_UNMET not in {reason for _,reason in self.retention(store)}:self.assertTrue(store.prune_oldest())
        raw,tiers=self.oldest(store)
        self.assertEqual(raw,FINE+HOUR)  # the first reading inside the floor has just gone
        self.assertEqual(tiers,{'minute':FINE,'15-minute':FINE,'hour':HOURS})
        self.assertEqual(store.db.execute('SELECT count(*) FROM readings').fetchone()[0],48)
        while store.prune_oldest():pass
        self.assertEqual([kind for kind,_ in groupby(self.retention(store))],
                         [('raw','storage_budget'),('minute','storage_budget'),('15-minute','storage_budget'),('hour','storage_budget'),
                          ('raw',FLOOR_UNMET),('minute',FLOOR_UNMET),('15-minute',FLOOR_UNMET),('hour',FLOOR_UNMET)])

    def test_saves_are_admitted_with_the_floors_kept_and_with_the_cap_too_small_for_them(self):
        # The cap is shared: the padding stands for the scanner's map. With room for the
        # floors a save prunes only what is older; without it the save still fits, raw inside
        # the floor gives first, recent hour rows stay, and coverage names the cause.
        measure=self.copy('measure',10**9)
        while FLOOR_UNMET not in {reason for _,reason in self.retention(measure)}:measure.prune_oldest()
        floors=measure.budget.usage()['bytes']
        raw_bytes=measure.db.execute('SELECT sum(length(payload)) FROM readings').fetchone()[0]
        limit=20*1024**2
        def at_cap(name,free):
            store=self.copy(name,limit)
            (store.path.parent/'scanner.sqlite').write_bytes(bytes(int(limit*.9)-free))
            store.append([reading(DAYS*24)])
            self.assertEqual(store.db.execute('SELECT last_sequence FROM runs').fetchone()[0],DAYS*24+1)
            self.assertLessEqual(store.budget.usage()['bytes'],limit)
            return store
        kept=at_cap('kept',floors+128*1024)
        self.assertNotIn(FLOOR_UNMET,{reason for _,reason in self.retention(kept)})
        # Pruning runs before the save, so the floors count back from the reading before it,
        # and it stops once the save fits: a tier may keep more than its floor, never less.
        raw,tiers=self.oldest(kept)
        self.assertEqual(raw,FINE)
        self.assertLessEqual(max(tiers['minute'],tiers['15-minute']),FINE)
        self.assertLessEqual(tiers['hour'],HOURS)
        gave=at_cap('gave',floors-raw_bytes//2)
        raw,tiers=self.oldest(gave)
        self.assertGreater(raw,FINE)
        self.assertEqual(tiers,{'minute':FINE,'15-minute':FINE,'hour':HOURS})
        gaps=Query(gave).coverage(FINE,raw)['coverage']['gaps']
        self.assertEqual({(g['kind'],g['reason']) for g in gaps if g['status']=='pruned'},{('raw',FLOOR_UNMET)})
