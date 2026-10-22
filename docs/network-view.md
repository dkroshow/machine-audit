# Network view

Implementation guidance for [Network Latency, Speed and Reliability](machine-information.md#network).
The [storage approach](storage-approach.md#collection-baseline) owns the cadences;
the [product brief](product-brief.md) owns the human/agent questions; the shared
[time and coverage rules](cpu-view.md#time-windows-and-coverage-working) apply.

The network design comes from an earlier private prototype, whose
measurements and rules were settled against real networks. This document maps
that design onto the three components, says what must be re-verified in
Python, and names Ubuntu sources or gaps.

## Three components, one path

The prototype orders its measurements by place along the path: the Wi-Fi link,
the router, the internet beyond it, a destination's name lookup and connect,
and data transfer. Machine-audit orders them by component. The two are axes of
the same grid, and each reading sits in one cell. Scope stays visible on every
reading: a router figure never stands in for an internet one.

| Reading | Component | Scope | Kind |
| --- | --- | --- | --- |
| Router ping round trip | Latency | Local link and router | Probe |
| Internet ping round trip (two public targets, alternating) | Latency | Provider and beyond | Probe |
| Name lookup time, resolver kind | Latency | Resolver (varies by network) | Probe |
| Web connect time to a fixed site, port 443 | Latency | Destination, end to end | Probe |
| Latency rise while the line is full | Latency | Internet, under load | Probe during test |
| Wi-Fi link rate, band, channel, generation | Speed | Local link only | Reading |
| Line capacity, download and upload | Speed | Internet, ceiling | Active test |
| Bytes in and out per interface | Speed | This machine, observed use | Counter |
| Bytes per program | Speed | This machine, attribution | Optional read |
| Lost and late pings, longest silence, dropouts and where | Reliability | Router or past it | Probe |
| Lookup or connect failure, with reason | Reliability | Resolver or destination | Probe |
| Captive sign-in page | Reliability | Network policy | Check |
| Path up/down, interface or gateway change | Reliability | Path | Event |
| Wi-Fi signal, noise, and their gap | Reliability | Local link | Reading |

The distinctions the product brief asks for fall out of the Scope and Kind
columns. **Capacity is not use:** the speed test states what the line can
carry; interface counters state what this machine moved; neither states what a
given workload got. **A probe is not the workload:** a 12 ms ping says the path
answers quickly; it does not say the browser was fast. **Local is not remote:**
a strong Wi-Fi link with a dead internet, and a weak link with a healthy
provider, are different problems and the grid keeps them apart. **Missing is
not healthy:** each reading carries a status, and no path, a blocked ping, a
reading the card reports as zero, and a failed read are each kept as that, never
as a good figure.

## Rules that carry over

These are the hard-won parts. Keep their meaning exactly; the thresholds and
bands stay in one place in the program, printed with the readings.

- **Two ping targets, always.** The router and a public address. Router
  answers and internet does not: the fault is past the router. Both silent:
  the fault is local. One target cannot tell those apart.
- **Late is not lost.** A reply after the deadline (0.9 s) is late; no reply
  within the give-up time (5 s) is lost. They are counted apart.
- **Keep the longest silence, not just the share lost.** A line that loses
  2% in 40-second holes is worse than one that loses 2% evenly. A dropout is
  at least two lost internet pings in a row covering at least 3 s; one lost
  ping is never a dropout. Each dropout records whether the router kept
  answering.
- **Time from the kernel, not the loop.** Latency runs from just before the
  send to the kernel's receive timestamp. Timing the reply when the collector
  gets to it put other collector work into the figure; the prototype measured
  that error at about 9 ms and fixed it.
- **A silent router is unknown, not bad.** A router that answers no ping for
  5 minutes while the internet answers does not answer pings; its latency and
  loss are null and set no grade. With the internet silent too, it is an outage.
- **A working connect overrides ping loss.** Some networks block pings while
  real traffic flows; a recent successful web connect says so.
- **The machine's own traffic is a guard, not a judgment.** A ping taken while
  this machine was itself moving a lot of data is tagged, so a backup is not
  blamed on the network. With the per-program setting on, the program is named.
- **Sessions per network.** A session runs from one network start to the next
  with sleep cut out; the network is the router's hardware address, stored
  only as an HMAC keyed by the private random `identity.salt` in the data
  directory (mode 0600, never exported; the exported device id is not the
  key). "Slower than usual here" compares against this
  network's earlier quiet minutes. No network names or addresses are stored.
- **Wi-Fi quality is signal minus noise.** Link rate is the link's speed, never
  the internet's. A reading of zero from the card is no reading.
- **The speed test runs for seconds, not for bytes**, so a fast line is not
  under-read during ramp-up, and pings keep running during it to measure the
  rise under load. It is the only real data cost and has its own daily transfer
  budget, which is separate from the storage budget. The Apple
  `networkQuality` method stays an optional macOS extra.

## Re-verify in Python

The design is portable; these mechanisms were proven in Swift and must be
measured again before the cadences in the storage approach are trusted.

- **Unprivileged ICMP.** macOS allows an ICMP datagram socket without
  privilege, which is how the prototype pings. Ubuntu allows it only for groups
  inside `net.ipv4.ping_group_range`; systemd's default sysctl opens it to all
  groups on recent releases, but each Ubuntu target must be checked. If closed, the
  choice is a disclosed setting change or no router pings, not a raw socket
  taken silently.
- **Kernel receive timestamps.** `SO_TIMESTAMP_MONOTONIC` on macOS; `SO_TIMESTAMPNS`
  or `SO_TIMESTAMPING` on Linux, read through `recvmsg` ancillary data. Confirm
  Python's `socket.recvmsg` exposes them on both, and measure the residual
  timing error the way the prototype did.
- **Ping cost.** The prototype measured 5 s pings at about 1.5 MB/day each way
  and a few hundredths of a percent of a core. Repeat on each target in Python.
- **Name lookup includes the cache.** `getaddrinfo` goes through the system
  resolver (mDNSResponder, systemd-resolved) and its cache, on both OSes. The
  figure is what a program experiences, not the network alone. Say so in the
  view; do not try to bypass the cache.
- **Speed test.** Parallel HTTPS streams to the same fixed server, download
  then upload, time-bounded with a byte cap. Pure Python TLS streams may not
  saturate a fast line; measure and record the method and its limit.
- **Interval under 5 s is invisible.** Gaps shorter than the ping interval can
  be missed on either OS. The view says this rather than implying coverage.

## Ubuntu sources and gaps

Unverified on each Ubuntu target until a reading is captured there. Each entry
names a source or says gap; nothing here is a capability claim.

| Reading | macOS | Ubuntu candidate |
| --- | --- | --- |
| Pings | ICMP datagram socket | Same, subject to `ping_group_range` |
| Interface byte counters | `sysctl` routing table | `/sys/class/net/<if>/statistics`, `/proc/net/dev` |
| Wi-Fi signal, noise, rate | CoreWLAN | nl80211 via `iw dev <if> link` and `station dump`; noise from `survey dump`; `/proc/net/wireless` as a coarse fallback |
| Router identity | ARP table via `sysctl` | `/proc/net/arp` or `ip neigh`, hashed the same way |
| Path and gateway events | `NWPathMonitor`, SystemConfiguration | rtnetlink socket (`AF_NETLINK`, route and link messages); `/proc/net/route` for the gateway |
| Metered or constrained path | `NWPath` expensive/constrained flags | NetworkManager `connection.metered` where NetworkManager runs; otherwise unknown, recorded as such |
| Resolver kind | `scutil --dns` | `resolvectl status`, `/etc/resolv.conf` |
| Captive portal | Apple's fixed page | Same page; the check is OS-independent |
| Speed test | HTTPS streams | Same |
| Bytes per program | Process socket table | **Gap.** `/proc/net/tcp` plus socket inodes name connections but not bytes; per-socket counters need `ss -ti` per connection or packet capture. Not a baseline read until a cheap source is measured |
| Sleep cut from sessions | Shared time rules | Same rules; `CLOCK_BOOTTIME` minus `CLOCK_MONOTONIC` |

A machine with no Wi-Fi (Big Mac on Ethernet) has no link readings, and the
view says so; it does not show an empty or healthy link.

## Storage and outputs

Follow the [preservation contract](storage-approach.md#preserve-readings-derive-summaries):
each probe result with its send time, scope, target class, outcome and timing;
counter snapshots as read; link readings as samples; path and portal events with
their observed times. Minute summaries keep median, p95, worst, loss, late
seconds, longest loss run, seconds probed and seconds without a path, as
the prototype's minute rows do. Sessions are a derived view over those and are
rebuilt rather than trusted as a source.

Both interfaces show the grid above for a selected interval, with the grade
rules printed beside the grades. The agent output states, per reading, its
scope, kind, status and the interval it covers, so an agent can tell a blocked
ping from a slow provider without reading the dashboard's prose.
