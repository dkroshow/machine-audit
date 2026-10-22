# Performance Components

Conceptual model for Machine-audit.

This describes what we want to understand, independently of what any existing recorder collects. Platform-specific measurement methods and gaps are in the component views linked below.

The five buckets are Compute (CPU, Memory, GPU), Storage, Network, Power, and Heat.

## Across every component

**History:** How each component changes over time. Relatively straightforward once readings are available: store them so past activity can be reviewed. Caching and careful query design can make historical data faster and cheaper to retrieve.

**Attribution:** Which processes or groups of related processes contribute to use or experience delays. What we can attribute depends on what the system exposes, which permissions the user is willing to grant, and how computationally intensive the information is to fetch. Attribution should distinguish who uses a resource, who experiences delays, and what causes those delays.

## CPU

**Capacity:** CPU resources available to the machine or workload, including any limits.

**Usage:** CPU time spent executing work. High usage shows activity; it does not, by itself, establish a problem.

**Contention:** Work ready to run but waiting for CPU capacity. This captures demand that cannot immediately be served.

See [CPU view](cpu-view.md).

## Memory

**Capacity:** Memory available to the machine or workload, including applicable limits.

**Usage:** What occupies memory, including programs, the operating system, and cached data, with attention to what can be reclaimed readily. Full memory is not necessarily strained memory.

**Movement:** Compression, decompression, and movement between memory and storage. This activity does not, by itself, establish a problem.

**Contention:** Useful work delayed or memory needs left unmet, including available evidence of stalls, allocation failures, and memory-related process terminations.

Memory use identifies consumers, not necessarily the cause of pressure.

See [Memory view](memory-view.md).

## GPU

**Capacity:** Available GPUs, their capabilities, and observed operating limits, including power and thermal restrictions.

**Usage:** Activity by GPU engine where available. Each percentage must retain its meaning; activity alone does not establish full use of execution resources.

**Contention:** Evidence that ready work is delayed getting GPU execution time, distinguishing measurements from estimates.

**Memory:** GPU memory use, limits, movement where observable, and allocation failures. Distinguish dedicated and shared memory without double-counting shared capacity.

See [GPU view](gpu-view.md).

## Storage

Storage is a matter of visibility: what is on the drive, and how that changes
over time. An initial scan builds the map; regular updates keep it current.

**Space:** How much storage is occupied, how much remains, and what occupies it: the drive's parts that add up to its capacity, then a map of every folder with what it actually uses, and what the map cannot see.

**Change:** How the map moves over any interval: which folders grew or shrank, since when, and by how much. Attribution says which program is writing and which has a folder in use.

**Activity and waiting:** The disk as a performance resource, separate from the map. Activity is data being read and written, the work storage performs. Waiting is work delayed until storage requests finish. Both are small counter signals read beside memory movement.

See [Storage view](storage-view.md).

## Network

**Latency:** How long communication takes, from reaching the local router to receiving a response from a remote service.

**Speed:** How fast data moves, including available transfer capacity and the transfer rates workloads actually achieve.

**Reliability:** How consistently communication succeeds. Loss, failures, and interruptions matter even when successful communication is fast.

See [Network view](network-view.md).

## Power

**Consumption:** How much power the machine and its components use, and how that accumulates into energy use over time.

**Supply:** Where power comes from and whether the available supply can meet the machine's needs.

**Battery:** Charge remaining, charging or discharging, and battery health, where a battery is present.

See [Power view](power-view.md).

## Heat

**Temperature:** How hot components are and how close they are to their operating limits.

**Cooling:** How the machine removes heat, including cooling activity and whether it keeps temperatures under control.

**Throttling:** Performance reduced to control heat. This distinguishes a warm machine from one whose temperature limits useful work.

See [Heat view](heat-view.md).

