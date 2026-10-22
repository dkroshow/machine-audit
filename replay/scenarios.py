"""Deterministic synthetic observations, not OS captures or query answers."""
from __future__ import annotations

from dataclasses import replace
from machine_audit.model import Clock, Field, Reading, Record

BASE_NS = 1_800_000_000_000_000_000
SECOND = 1_000_000_000


def clock(second: int) -> Clock:
    return Clock(BASE_NS + second * SECOND, second * SECOND, second * SECOND)


def record(second: int, fields: list[Field], *, source: str, component: str,
           sequence: int, run: str = "synthetic") -> Record:
    return Record(Reading(source, component, clock(second), clock(second), tuple(fields)),
                  "synthetic-boot", run, sequence, clock(second).wall_ns, clock(second))


def cpu_scenario(*, contended: bool) -> list[Record]:
    """Eight usable CPUs. Four CPUs of work, then 12 runnable tasks only if contended.

    Eight host counters advance by 30 user and 20 system ticks per second in both
    cases. Two instances of one program overlap in each ten-second pass; their
    combined rate is two CPUs, not a rate divided by summed instance lifetimes.
    """
    records = []
    for second in range(0, 61, 10):
        fields = []
        for cpu in range(8):
            for state, ticks_per_second in (("user", 30), ("system", 20), ("idle", 50), ("nice", 0)):
                fields.append(Field("cpu." + state, second * ticks_per_second, "ticks", "counter",
                                    entity=f"cpu:{cpu}", scale=0.01, rate_unit="CPUs"))
        fields.extend([
            Field("cpu.runnable", 12000 if contended and second >= 20 else 4000,
                  "LOAD_SCALE_1000", scale=0.001),
            Field("cpu.wait", None, "seconds", "counter", status="unavailable"),
        ])
        for pid in (11, 12):
            for state in ("user", "system"):
                fields.append(Field("process." + state + "_time", second * SECOND // 2, "ns", "counter",
                                    entity=f"process:synthetic:{pid}:1", label="Example worker",
                                    scope="process", additive=True, scale=1e-9, rate_unit="CPUs"))
        records.append(record(second, fields, source="synthetic.cpu", component="cpu", sequence=len(records) + 1))
    return records


def memory_scenario() -> list[Record]:
    """Pressure and movement, a stall interval, then recovery; missing is explicit.

    Swap-in source units are pages. PSI source units are microseconds. Recovery
    means available memory and pressure recovered; cumulative movement counters
    remain high. Overlapping per-process footprints have no host remainder.
    """
    records = []
    # (second, available bytes, pressure, swap-in pages, PSI stall microseconds)
    observations = [(0, 8_000_000_000, "normal", 100, 0),
                    (10, 2_000_000_000, "warning", 120, 0),
                    (20, 500_000_000, "critical", 180, 2_000_000),
                    (30, 6_000_000_000, "normal", 180, 2_000_000),
                    (40, 7_000_000_000, "normal", 180, 2_000_000)]
    for second, available, pressure, swap, stall in observations:
        records.append(record(second, [
            Field("memory.available", available, "bytes"),
            Field("memory.pressure", pressure, "level", "state"),
            Field("memory.swapins", swap, "pages", "counter"),
            Field("memory.psi.some", stall, "us", "counter", scale=1e-6),
            Field("memory.macos_stall", None, "seconds", "counter", status="unavailable"),
            Field("process.memory", 3_000_000_000, "bytes", entity="process:synthetic:21:1", label="Example browser", scope="footprint"),
            Field("process.memory", 3_000_000_000, "bytes", entity="process:synthetic:22:1", label="Example browser", scope="footprint"),
        ], source="synthetic.memory", component="memory", sequence=len(records)+1))
    return records


def all_components() -> list[Record]:
    """Small multi-component UI fixture. Values are invented and labelled synthetic."""
    records=cpu_scenario(contended=True)
    for row in memory_scenario():
        records.append(Record(row.reading,row.boot_id,row.run_id,len(records)+1,row.due_wall_ns,row.received))
    extras={
        "gpu":[Field("gpu.utilization", 34, "percent", "rate",entity="gpu:synthetic",label="Example GPU",scope="device"),
               Field("gpu.memory", 1_400_000_000,"bytes",entity="gpu:synthetic",label="Example GPU",scope="device"),
               Field("gpu.wait",None,"seconds",status="unavailable")],
        "storage":[Field("disk.write",120_000_000,"bytes","counter"),Field("storage.available",400_000_000_000,"bytes")],
        "network":[Field("network.receive",75_000_000,"bytes","counter"),Field("network.router.rtt",2.1,"ms"),
                   Field("network.internet.rtt",18.5,"ms"),Field("network.internet.lost",0,"count",status="measured_zero"),
                   Field("network.capacity.download",None,"Mbps",status="not_enabled")],
        "power":[Field("power.smc_system",118,"watts",scope="smc_system"),Field("power.supply","AC","state","state"),
                 Field("power.battery",False,"boolean","state"),Field("power.wall",None,"watts",status="unavailable")],
        "heat":[Field("heat.temperature",66,"degC",entity="sensor:synthetic",label="Example CPU sensor",scope="sensor"),
                Field("heat.fan",1200,"rpm",scope="fan"),Field("heat.speed_limit",100,"percent")],
        "self":[Field("self.cpu",0.12,"seconds","counter",entity="monitor:synthetic",label="Collector",scope="managed_process",additive=True),
                Field("self.memory",24_000_000,"bytes",entity="monitor:synthetic",label="Collector",scope="rss"),
                Field("self.storage",4_000_000,"bytes"),
                Field("self.io.read",0,"bytes","counter"),Field("self.io.write",0,"bytes","counter"),
                Field("self.network",None,"bytes",status="unobserved"),
                Field("self.query.duration",0.003,"seconds",scope="elapsed")],
    }
    for second in (0,10,20,30,40,50,60):
        for component, fields in extras.items():
            rates = {"disk.write":500_000, "network.receive":2_500_000, "self.cpu":0.012,
                     "self.io.read":2048, "self.io.write":4096, "self.storage":100}
            values = [replace(field, value=field.value + second*rates[field.name])
                      if field.name in rates else field for field in fields]
            records.append(record(second,values,source=f"synthetic.{component}",component=component,sequence=len(records)+1))
    return records


def crowded_dashboard() -> list[Record]:
    """A busy catalog must not hide another component; build IDs must wrap."""
    records = all_components()
    records.append(record(30, [Field(f"cpu.sample_{i:03d}", i, "count") for i in range(620)],
                          source="synthetic.cpu.crowded", component="cpu", sequence=len(records)+1))
    records.append(record(30, [Field("self.install_revision", "synthetic-build-"+"a"*64, "version", "state",
                                    entity="monitor:synthetic", label="Synthetic build")],
                          source="synthetic.self.identity", component="self", sequence=len(records)+1))
    return records
