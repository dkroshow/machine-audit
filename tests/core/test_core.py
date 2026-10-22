"""Synthetic boundary checks; never read live sources or the user's recorder."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.collector import Collector
from machine_audit.derive import points, summarize
from machine_audit.model import Clock, Field, Reading, Record, admit_label, process_identity
from machine_audit.query import Query
from machine_audit.serve import make_server
from machine_audit.store import Store, unpack_summaries

S=1_000_000_000


def reading(t,value,*,entity="host",label=None,name="cpu.counter",kind="counter",status="observed",source="synthetic",component="cpu",awake=None):
    clock=Clock(t*S,(t if awake is None else awake)*S,t*S)
    return Reading(source,component,clock,clock,(Field(name,value,"seconds",kind,status,entity,label,additive=bool(label)),))


def record(t,value,seq,**kwargs):
    r=reading(t,value,**kwargs)
    return Record(r,"boot","run",seq,r.end.wall_ns,r.end)


class CoreTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parent/".tmp"
        root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.budget=Budget(self.tmp.name,limit_bytes=20_000_000,warning_bytes=10_000_000,free_margin_bytes=0)
        self.store=Store(Path(self.tmp.name)/"readings.sqlite",self.budget)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hour_only_scope_writes_no_minute_summaries(self):
        # Per-scope summary resolution: Linux unit limits (scope unit_detail) were 10% of the first
        # Ubuntu run's summary payload while changing rarely.
        c=Clock(10*S,10*S,10*S)
        reading=Reading("linux.unit_limits","memory",c,c,(Field("unit.memory.max",1,"bytes",entity="cgroup:abc:1",scope="unit_detail"),
                                                          Field("memory.free",2,"bytes")))
        self.store.append([Record(reading,"boot","run",1,c.wall_ns,c)])
        rows=set(self.store.db.execute("SELECT resolution,entity FROM summaries"))
        self.assertEqual(rows,{("hour","cgroup:abc:1"),("minute","host"),("hour","host")})

    def test_raw_counter_reset_and_pid_reuse_do_not_become_usage(self):
        a=process_identity(7,1,"boot");b=process_identity(7,2,"boot")
        rows=points([record(0,10,1,entity=a),record(10,15,2,entity=a),record(20,2,3,entity=a),record(30,100,4,entity=b)])
        self.assertEqual([r["status"] for r in rows],["unobserved","observed","invalid","unobserved"])
        self.assertEqual(rows[1]["total"],5)

    def test_program_denominator_and_simultaneous_peaks_use_aligned_pass(self):
        records=[]
        for seq,t in enumerate((0,10,20),1):
            c=Clock(t*S,t*S,t*S)
            fields=tuple(Field("process.cpu",value,"seconds","counter",entity=f"process:boot:{pid}:1",label="Editor",scope="own",additive=True)
                         for pid,value in ((1,4*t if t<=10 else 40),(2,4*t if t<=10 else 40)))
            records.append(Record(Reading("pass","cpu",c,c,fields),"boot","run",seq,c.wall_ns,c))
        program=[r for r in points(records) if r["entity"]=="program:Editor"]
        summary=summarize(program)
        self.assertEqual(summary["total"],80)
        self.assertEqual(summary["covered_s"],20)
        self.assertEqual(summary["mean_rate"],4)
        self.assertEqual(summary["peak_rate"],8)
        self.assertEqual(summary["above_s"]["4.0"],10)

    def test_original_cpu_snapshots_preserved_derived_busy_separate(self):
        records=[]
        for seq,t in enumerate((0,10),1):
            c=Clock(t*S,t*S,t*S)
            f=(Field("process.user_time",t*2,"ticks","counter",entity="process:1",label="Editor",scale=.5),Field("process.system_time",t,"ticks","counter",entity="process:1",label="Editor",scale=.5))
            records.append(Record(Reading("process","cpu",c,c,f),"boot","run",seq,c.wall_ns,c))
        self.store.append(records)
        with self.store.read() as db:
            raw=Record.unpack(db.execute("SELECT payload FROM readings WHERE sequence=2").fetchone()[0]).to_dict()
        self.assertEqual([f["name"] for f in raw["reading"]["fields"]],["process.user_time","process.system_time"])
        cpu=[p for p in points(records) if p["signal"]=="process.cpu" and p["entity"]=="process:1"][-1]
        self.assertEqual(cpu["total"],30)
        self.assertEqual(cpu["value"],1.5)

    def test_sleep_uses_awake_denominator_and_marks_sleep(self):
        rows=points([record(0,0,1,awake=0),record(20,10,2,awake=10)])
        self.assertEqual(rows[-1]["value"],1)
        self.assertEqual(rows[-1]["sleep_s"],10)

    def test_clock_pair_jitter_is_not_sleep(self):
        # First Linux run: the awake and continuous clocks, read by two calls, differed by up
        # to 8,270 ns between reads and flagged every window as containing sleep.
        def at(awake,continuous,seq,value):
            c=Clock(awake,awake,continuous)
            return Record(Reading("synthetic","cpu",c,c,(Field("cpu.counter",value,"seconds","counter"),)),"boot","run",seq,c.wall_ns,c)
        rows=points([at(0,0,1,0),at(10*S,10*S+8270,2,10)])
        self.assertEqual((rows[-1]["sleep_s"],rows[-1]["value"]),(0.0,1.0))

    def test_snapshot_commit_race_does_not_duplicate_saved_tail(self):
        collector=Collector(self.store,run_id="run",clock=lambda:Clock(20*S,20*S,20*S))
        collector.ingest(reading(0,0));collector.ingest(reading(10,10))
        original=self.store.read
        @contextmanager
        def race():
            collector.flush()
            with original() as db:yield db
        self.store.read=race
        result=Query(self.store,collector).series(0,20*S,signals=["cpu.counter"])
        self.assertEqual(sum(r["total"] or 0 for r in result["rows"]),10)
        self.assertGreaterEqual(collector.committed,2)

    def test_save_failure_keeps_tail_and_now_cache_after_success(self):
        collector=Collector(self.store,clock=lambda:Clock(20*S,20*S,20*S))
        collector.ingest(reading(0,0));collector.ingest(reading(10,10))
        original=self.store.append
        failure=sqlite3.OperationalError("synthetic private detail")
        failure.sqlite_errorname="SQLITE_FULL"
        self.store.append=lambda *args,**kwargs: (_ for _ in ()).throw(failure)
        self.assertFalse(collector.flush());self.assertEqual(collector.committed,0)
        self.assertEqual(collector.status()["save_error"],"sqlite_full")
        self.assertGreaterEqual(len(collector.tail),2)
        self.store.append=original
        self.assertTrue(collector.flush());self.assertEqual(len(collector.tail),0)
        now=Query(self.store,collector).now()
        self.assertTrue(any(r["signal"]=="cpu.counter" for r in now["trail_rows"]))
        self.assertEqual(next(r for r in now["rows"] if r["signal"]=="cpu.counter")["value"],1)

    def test_budget_sqlite_hard_bound_rolls_back_oversized_write(self):
        self.budget.limit_bytes=1_000_000
        with self.assertRaises(sqlite3.DatabaseError):
            with self.budget.sqlite_write(self.store.db,self.store.path,growth_bytes=200_000):
                try:
                    self.store.db.execute("BEGIN IMMEDIATE")
                    self.store.db.execute("CREATE TABLE payloads (data BLOB)")
                    self.store.db.execute("INSERT INTO payloads VALUES(zeroblob(2000000))")
                    self.store.db.execute("COMMIT")
                except sqlite3.DatabaseError:
                    if self.store.db.in_transaction:self.store.db.execute("ROLLBACK")
                    raise
        self.assertLessEqual(self.budget.usage()["bytes"],self.budget.limit_bytes)

    def test_prune_retains_summaries_and_reports_removed_detail(self):
        self.store.append([record(t,t,seq) for seq,t in enumerate(range(0,121,10),1)])
        self.assertTrue(self.store.prune_oldest())
        with self.store.read() as db:
            self.assertGreater(db.execute("SELECT count(*) FROM summaries").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT MIN(wall_ns) FROM readings").fetchone()[0],60*S)
        answer=Query(self.store).coverage(0,120*S)
        self.assertEqual(answer["coverage"]["retention"][0]["kind"],"raw")
        self.assertLessEqual(self.budget.usage()["bytes"],self.budget.limit_bytes)

    def test_private_labels_and_export_exclude_scanner(self):
        self.assertEqual(admit_label("/Users/person/private"),"redacted")
        self.assertEqual(admit_label("name@example.com"),"redacted")
        self.store.append([record(0,0,1,label="/Users/person/private")])
        Path(self.tmp.name,"scanner.sqlite").write_bytes(b"private filenames")
        path=Path(self.tmp.name)/"chunk.sqlite"
        self.store.export_chunk(path,device_id="device",run_id="run")
        with sqlite3.connect(path) as chunk:
            exported="".join(row[0] for row in chunk.execute("SELECT payload FROM records"))
        self.assertNotIn("private",exported)
        self.assertNotIn("scanner",exported)

    def test_http_host_origin_token_and_traversal_boundaries(self):
        server=make_server(Query(self.store),"a"*32,port=0,dashboard=self.tmp.name)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        base=f"http://127.0.0.1:{server.server_port}"
        try:
            for headers in ({},{"Authorization":"Bearer "+"a"*32,"Host":"evil.test"},{"Authorization":"Bearer "+"a"*32,"Origin":"https://evil.test"}):
                with self.assertRaises(HTTPError):urlopen(Request(base+"/api/status",headers=headers))
            with urlopen(Request(base+"/api/status",headers={"Authorization":"Bearer "+"a"*32})) as r:
                self.assertEqual(json.load(r)["shape"],"status")
            with self.assertRaises(HTTPError):urlopen(base+"/%2e%2e/secret.txt")
        finally:server.shutdown();server.server_close();thread.join()

    def test_memory_from_bundled_cpu_pass_is_queryable_without_another_read(self):
        c=Clock(10*S,10*S,10*S)
        fields=(Field("process.phys_footprint",100,"bytes",entity="process:1",label="Editor",scope="footprint",component="memory"),)
        record_=Record(Reading("process","cpu",c,c,fields),"boot","run",1,c.wall_ns,c)
        self.store.append([record_])
        query=Query(self.store)
        self.assertEqual(query.who(0,20*S,component="memory")["rows"][0]["mean"],100)
        self.assertEqual(query.catalog()["signals"][0]["component"],"memory")
        self.assertIn("process",query.coverage(0,20*S,component="memory")["coverage"]["sources"])

    def test_program_peaks_are_four_when_member_peaks_do_not_overlap(self):
        records=[]
        for seq,(t,a,b) in enumerate(((0,0,0),(10,40,0),(20,40,40)),1):
            c=Clock(t*S,t*S,t*S)
            fields=tuple(Field("process.cpu",v,"seconds","counter",entity=f"process:{pid}",label="Editor",additive=True) for pid,v in ((1,a),(2,b)))
            records.append(Record(Reading("pass","cpu",c,c,fields),"boot","run",seq,c.wall_ns,c))
        self.store.append(records)
        result=Query(self.store).who(0,30*S,signal="process.cpu")
        self.assertEqual(result["rows"][0]["peak_rate"],4)
        self.assertEqual(result["rows"][0]["covered_s"],20)
        self.store.prune_oldest()
        retained=Query(self.store).who(0,1000*S,signal="process.cpu")
        self.assertEqual(retained["rows"][0]["peak_rate"],4)
        self.assertEqual(retained["rows"][0]["total"],80)

    def test_denied_and_empty_series_bins_never_become_zero(self):
        self.store.append([record(10,None,1,status="denied",kind="gauge")])
        query=Query(self.store)
        result=query.series(0,20*S,signals=["cpu.counter"],limit=20)
        self.assertTrue(any(r["status"]=="denied" for r in result["rows"]))
        missing=[r for r in result["rows"] if r["status"]=="unobserved"]
        self.assertTrue(missing)
        self.assertTrue(all(r["total"] is None for r in missing))
        self.assertEqual(query.coverage(0,20*S)["coverage"]["sources"]["synthetic"]["denied"],1)

    def test_raw_pruning_keeps_incidents_and_next_counter_baseline(self):
        self.store.append([record(0,0,1),record(10,10,2)])
        c=Clock(20*S,20*S,20*S)
        event=Record(Reading("event","heat",c,c,(Field("heat.state","hot","state","event"),)),"boot","run",3,c.wall_ns,c)
        self.store.append([event])
        self.store.prune_oldest()
        self.store.append([record(30,30,4)])
        with self.store.read() as db:
            latest=next(row for row in unpack_summaries(db.execute("SELECT payload FROM summaries WHERE source='synthetic' ORDER BY id DESC LIMIT 1").fetchone()[0]) if row["signal"]=="cpu.counter")
        self.assertEqual(latest["total"],30)
        self.assertEqual(len(Query(self.store).around(20*S,20*S)["events"]),1)

    def test_hung_source_is_not_replaced_and_other_source_runs(self):
        import time
        gate=threading.Event()
        slow_entered=threading.Event()
        fast_progress=threading.Event()
        calls=[]
        class Slow:
            name="slow";component="cpu";cadence_s=.001
            def read(self):
                calls.append("slow");slow_entered.set();gate.wait()
                return reading(1,1,source="slow")
        class Fast:
            name="fast";component="cpu";cadence_s=.005
            def read(self):
                calls.append("fast")
                if calls.count("fast")>=2:fast_progress.set()
                return reading(1,1,source="fast")
        collector=Collector(self.store,[Slow(),Fast()])
        collector.start()
        server=server_thread=writer=None
        try:
            self.assertTrue(slow_entered.wait(2),"slow source never started")
            self.assertTrue(fast_progress.wait(2),"other source cannot progress while one is blocked")
            # Age the blocked read past ten cadences after observing both
            # threads, rather than assuming two reads fit a 40 ms CPU slice.
            time.sleep(Slow.cadence_s*11)
            self.assertEqual(calls.count("slow"),1)
            self.assertGreater(calls.count("fast"),1)
            self.assertEqual(next(s for s in collector.source_status() if s["source"]=="slow")["status"],"hung")
            saved=threading.Event();outcome=[]
            def flush():
                outcome.append(collector.flush());saved.set()
            writer=threading.Thread(target=flush,daemon=True);writer.start()
            self.assertTrue(saved.wait(2),'blocked native read stalled the writer')
            self.assertEqual(outcome,[True])
            with self.store.read() as db:
                self.assertGreater(db.execute("SELECT count(*) FROM readings WHERE source='fast'").fetchone()[0],0)
            token='synthetic-token-'+'x'*32
            server=make_server(Query(self.store,collector),token,port=0)
            server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
            request=Request(f'http://127.0.0.1:{server.server_port}/api/status',headers={'Authorization':'Bearer '+token})
            with urlopen(request,timeout=2) as response:status=json.load(response)
            self.assertEqual(next(s for s in status['collector']['sources'] if s['source']=='slow')['status'],'hung')
            self.assertEqual(calls.count('slow'),1)
        finally:
            gate.set()
            if writer:writer.join(2)
            collector.close()
            if server is not None:server.shutdown();server.server_close();server_thread.join(2)

    def test_per_entity_timing_uses_union_not_sum_or_envelope(self):
        records=[]
        for seq,(t,a,b) in enumerate(((0,0,0),(30,10,10)),1):
            c=Clock(t*S,t*S,t*S)
            clocks=((0,10),(20,30)) if seq==2 else ((0,0),(20,20))
            fields=tuple(Field("process.cpu",v,"seconds","counter",entity=f"process:{pid}",label="Editor",additive=True,start=Clock(lo*S,lo*S,lo*S),end=Clock(hi*S,hi*S,hi*S)) for pid,v,(lo,hi) in ((1,a,clocks[0]),(2,b,clocks[1])))
            records.append(Record(Reading("pass","cpu",c,c,fields),"boot","run",seq,c.wall_ns,c))
        row=summarize([p for p in points(records) if p["entity"]=="program:Editor"])
        self.assertEqual(row["total"],20)
        self.assertEqual(row["covered_s"],20)
        self.assertEqual(row["mean_rate"],1)

    def test_counter_window_is_not_also_reported_as_missing_bins(self):
        self.store.append([record(0,0,1),record(10,10,2)])
        result=Query(self.store).series(0,10*S,signals=["cpu.counter"],limit=20)
        self.assertFalse(any(row["status"]=="unobserved" and row["end_ns"]>0 for row in result["rows"]))

    def test_export_chunk_hash_identity_and_budget_include_both_files(self):
        import hashlib
        self.store.append([record(0,0,1),record(10,10,2),record(20,20,3)])
        destination=Path(self.tmp.name)/"outgoing.sqlite"
        manifest=self.store.export_chunk(destination,device_id="device",run_id="run",limit=2,max_bytes=131072)
        self.assertEqual(manifest["count"],2)
        self.assertEqual(manifest["first_sequence"],1)
        self.assertEqual(manifest["last_sequence"],2)
        self.assertTrue(manifest["truncated"])
        self.assertEqual(manifest["sha256"],hashlib.sha256(destination.read_bytes()).hexdigest())
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0],2)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%scanner%'").fetchall())
        self.assertIn("outgoing.manifest.json",self.budget.usage()["parts"])
        self.assertLessEqual(self.budget.usage()["bytes"],self.budget.limit_bytes)
        with self.assertRaises(ValueError):self.store.export_chunk(destination,device_id="device",run_id="run")

    def test_derived_cpu_does_not_hide_one_constituent_counter_reset(self):
        records=[]
        for seq,(t,user,system) in enumerate(((0,100,0),(10,0,200)),1):
            c=Clock(t*S,t*S,t*S)
            fields=tuple(Field(name,value,"ticks","counter",entity="process:1",label="Editor") for name,value in (("process.user_time",user),("process.system_time",system)))
            records.append(Record(Reading("pass","cpu",c,c,fields),"boot","run",seq,c.wall_ns,c))
        row=[p for p in points(records) if p["signal"]=="process.cpu" and p["entity"]=="process:1"][-1]
        self.assertEqual(row["status"],"invalid")
        self.assertIsNone(row["total"])

    def test_client_refuses_redirects_before_forwarding_credentials(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from machine_audit.client import request,ClientError
        from machine_audit.config import Config
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(302);self.send_header("Location","http://127.0.0.1:1/stolen");self.end_headers()
        server=ThreadingHTTPServer(("127.0.0.1",0),Redirect)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        config=Config(data_dir=self.tmp.name,port=server.server_port)
        config.token_path.write_text("a"*32);config.token_path.chmod(0o600)
        try:
            with self.assertRaises(ClientError) as raised:request(config,"status")
            self.assertEqual(raised.exception.code,"redirect_refused")
        finally:server.shutdown();server.server_close();thread.join()

    def test_cumulative_hour_summary_stays_one_row_and_keeps_total(self):
        self.store.append([record(0,0,1),record(10,10,2)])
        self.store.append([record(20,30,3)])
        with self.store.read() as db:
            rows=db.execute("SELECT payload FROM summaries WHERE resolution='hour'").fetchall()
        self.assertEqual(len(rows),1)
        row=next(r for r in unpack_summaries(rows[0][0]) if r["signal"]=="cpu.counter")
        self.assertEqual(row["total"],30)
        self.assertEqual(row["peak_rate"],2)

    def test_single_collector_guard_precedes_any_second_run(self):
        first=Collector(self.store);second=Collector(self.store)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError,"collector_already_running"):second.acquire()
        finally:first.close()
        second.acquire();second.close()

    def test_self_cpu_has_one_instance_without_duplicate_program_cost(self):
        records=[]
        for seq,t in enumerate((0,10),1):
            c=Clock(t*S,t*S,t*S)
            f=tuple(Field(name,t,"seconds","counter",entity="process:1",label="collector",additive=True) for name in ("self.user_time","self.system_time"))
            records.append(Record(Reading("self.process","self",c,c,f),"boot","run",seq,c.wall_ns,c))
        derived=points(records)
        self.assertFalse(any(p["entity"].startswith("program:") for p in derived))
        self.store.append(records)
        result=Query(self.store).who(0,20*S,signal="self.cpu",component="self")
        self.assertEqual(len(result["rows"]),1)
        self.assertEqual(result["rows"][0]["total"],20)

    def test_query_starts_snapshot_before_racing_cumulative_commit(self):
        import time
        collector=Collector(self.store,run_id="run",clock=lambda:Clock(30*S,30*S,30*S))
        collector.ingest(reading(0,0));collector.ingest(reading(10,10));collector.flush()
        collector.ingest(reading(20,20))
        original=self.store.read
        started=threading.Event();threads=[]
        @contextmanager
        def racing_read():
            def flush():started.set();collector.flush()
            thread=threading.Thread(target=flush);threads.append(thread);thread.start()
            started.wait(1);time.sleep(.01)
            with original() as db:yield db
        self.store.read=racing_read
        result=Query(self.store,collector).series(0,30*S,signals=["cpu.counter"])
        for thread in threads:thread.join(2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(r["total"] or 0 for r in result["rows"]),20)

    def test_http_series_uses_same_query_units_and_totals(self):
        self.store.append([record(0,0,1),record(10,7,2)])
        server=make_server(Query(self.store),"a"*32,port=0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            req=Request(f"http://127.0.0.1:{server.server_port}/api/series?start=0&end=20&signals=cpu.counter",headers={"Authorization":"Bearer "+"a"*32})
            with urlopen(req) as response:result=json.load(response)
            self.assertEqual(sum(r["total"] or 0 for r in result["rows"]),7)
            self.assertEqual(next(r["unit"] for r in result["rows"] if r["total"] is not None),"seconds")
        finally:server.shutdown();server.server_close();thread.join()

    def test_http_admits_only_loopback_and_configured_hosts_and_origins(self):
        # Proxies send https Origins and tunnels http; anything else stays refused.
        server=make_server(Query(self.store),"a"*32,port=0,allowed_hosts=["dash.example.org","localhost:9000"])
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def get(host,origin=None):
            headers={"Host":host,"Authorization":"Bearer "+"a"*32,**({"Origin":origin} if origin else {})}
            try:
                with urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/catalog",headers=headers)) as response:return response.status
            except HTTPError as error:
                with error:return json.load(error)["error"]
        try:
            loopback=f"127.0.0.1:{server.server_port}"
            for host,origin in ((loopback,None),("dash.example.org","https://dash.example.org"),("localhost:9000","http://localhost:9000")):
                self.assertEqual(get(host,origin),200)
            self.assertEqual(get("other.example.org"),"host_not_allowed")
            self.assertEqual(get("dash.example.org","https://other.example.org"),"origin_not_allowed")
        finally:server.shutdown();server.server_close();thread.join()

    def test_cpu_remainder_compares_matching_windows_and_keeps_memory_nonadditive(self):
        records=[];seq=0
        for t in (0,10):
            c=Clock(t*S,t*S,t*S)
            seq+=1
            records.append(Record(Reading("host","cpu",c,c,(Field("cpu.busy",3*t,"seconds","counter"),)),"boot","run",seq,c.wall_ns,c))
            seq+=1
            fields=tuple(Field("process.cpu",t,"seconds","counter",entity=f"process:{pid}",label="Editor",additive=True) for pid in (1,2))
            records.append(Record(Reading("pass","cpu",c,c,fields),"boot","run",seq,c.wall_ns,c))
        self.store.append(records)
        result=Query(self.store).who(0,10*S,signal="process.cpu")
        self.assertEqual(result["accounting"]["attributed"],20)
        self.assertEqual(result["accounting"]["host_total"],30)
        self.assertEqual(result["accounting"]["remainder"],10)
        self.assertFalse(Query(self.store).who(0,10*S,component="memory")["accounting"]["additive"])

    def test_pruning_retires_routine_hours_before_incident_evidence(self):
        c=Clock(10*S,10*S,10*S)
        event=Record(Reading("events","heat",c,c,(Field("heat.state","hot","state","event"),)),"boot","run",2,c.wall_ns,c)
        self.store.append([record(0,0,1),event])
        self.assertTrue(self.store.prune_oldest())
        self.assertTrue(self.store.prune_oldest())
        self.assertTrue(self.store.prune_oldest())
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM summaries").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM incidents").fetchone()[0],1)
        self.assertTrue(self.store.prune_oldest())


if __name__=="__main__":unittest.main()
