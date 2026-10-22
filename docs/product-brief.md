# Product brief

## Users and operating contexts

Machine-audit serves humans and agents responsible for keeping devices running
well. These are two distinct users of the same product, each with an experience
optimized for how they work:

- **Humans:** a dashboard for understanding machine state, exploring history,
  and investigating problems. The dashboard of an earlier private prototype
  provides a starting point for this experience.
- **Agents:** an interface designed for efficiently finding and interpreting
  the evidence needed for their tasks. Its usability deserves the same
  attention as the human dashboard.

Both interfaces should draw on the same evidence and preserve its meaning,
limitations, and uncertainty. Their presentation can differ to suit their users.

Both interfaces should support examining a selected time interval, from a
continuously updating live view to historical periods. Resource use, contention,
and workload attribution should refer to that same interval. Available detail,
collection gaps, and changes in capacity should remain visible.

The product must support two overlapping operating contexts:

- **Agent workstations and servers:** machines running delegated tasks,
  automation, and services, often without someone watching them.
- **Personal computers:** laptops and desktops where background work must
  coexist with the person's immediate needs.

A single machine may serve both purposes. Machine-audit should help its users
understand competition between workloads without assuming that unattended
work is wasteful or that a busy machine is unhealthy.

The product grew from problems with our own devices: a powerful Mac Pro that
required increasing care to maintain, and a laptop whose memory was
overwhelmed while testing a browser-based agent harness. These experiences
exposed a gap between successfully completing individual tasks and keeping
the machine healthy over time.

## Platform scope and adaptation

The initial rollout is Big Mac (an Intel Mac Pro), followed by a physical Ubuntu
workstation and an Ubuntu AWS instance. The [installation guide](installation.md#initial-machines)
records the known inventory and rollout. Apple silicon and other systems remain
adaptation targets. The [vision](vision.md#an-adaptable-framework) explains the
broader ambition; no plugin framework or complete platform parity is required.

Implementing agents discover and validate each machine's sources, permissions and
cost as part of implementation. The existing source guidance is their starting
point; a separate up-front investigation of every Ubuntu detail is not required.
Record the resulting capabilities and gaps without claiming untested support.

Standardize the questions and organization of the views across operating systems,
while preserving differences in the underlying evidence. Comparable measurements
should use consistent definitions and units. Different measurements may inform
the same question without being presented as equivalent, and unavailable
information must not appear as zero or healthy.

## Execution and distribution

Distribute Machine-audit as source code that runs through an installed runtime.
Keep setup, dependencies, and maintenance simple, and make the code easy for
humans and agents to inspect and adapt. Working assumption: Python for collection
and services, with an HTML/CSS/JavaScript dashboard. The [architecture](architecture.md)
describes the working design; [installation and migration](installation.md) covers
the first-user trial and Tailscale dashboard access.

## Initial product scope

The first Big Mac trial includes CPU, memory, GPU, storage, network, power and
heat, with the human dashboard and agent interface. All components are in scope;
optional privileged diagnostics still require a disclosed access decision.

The first version focuses on collecting useful measurements and events, retaining
them efficiently, and making them accessible to humans and agents. We do not yet
know exactly how every signal should combine into a diagnosis. Preserve enough
evidence to improve that understanding through real incidents; measurements we
did not capture cannot be recovered afterward. The [storage approach](storage-approach.md)
sets the collection baseline, preservation rules and storage budget.

The initial product should help users inspect what happened before, during, and
after an incident. Comprehensive diagnosis, workload-specific capacity predictions,
and automatic conclusions are not prerequisites for this first version. The
questions below guide collection and presentation; they do not imply that every
answer will initially be automated.

## Questions it must answer

Machine-audit should equip a human or agent to maintain reliable performance,
investigate problems, improve resource use, and judge whether a machine can
take on more work.

Its central questions are:

- **Is the machine under strain?** Which resources are limiting useful work?
  Is the condition brief, sustained, or recurring?
- **What happened earlier?** When did strain occur, how long did it last, and
  what workloads were running?
- **What contributed?** Which programs, services, or groups of related
  processes consumed resources or experienced delays? What remains unattributed?
- **Could the work run more efficiently?** Is there evidence of unnecessary
  repetition, resource growth, competing workloads, or expensive background
  activity? What change is worth investigating?
- **Can the machine handle more work?** What headroom exists for the proposed
  workload, including during busy periods? What might become the next constraint?
- **Did a change help?** Under comparable conditions, did performance improve,
  and what tradeoffs appeared?

Answers must distinguish measurements, interpretations, and recommendations.
When the evidence cannot establish a cause or predict capacity, the tool
should say so.

The [Performance Components](machine-information.md) document turns these questions
into resource components and defines the shared roles of history and
attribution. Use it as the foundation for collection design and platform
adaptations; current measurement capabilities do not limit the model.

## Cost of monitoring

Machine-audit's own operating cost is a first-class product view, available live
and for a selected historical interval. Humans have a readily accessible
**Monitoring cost** view; agents retrieve the same evidence through the
[shared queries](query-design.md). This is ongoing measurement, not just a
pre-release benchmark.

| Resource | Evidence to retain and show |
| --- | --- |
| CPU | CPU time and usage over the covered interval |
| Memory | Current, average and peak use, with the accounting scope named |
| Disk | Retained space and growth, plus bytes read and written where measurable |
| Network | Bytes sent and received, distinguishing probes, dashboard traffic and log transfers where measurable |

Include collection, helpers, scanning, storage, queries and log transfer when
present. Show measured totals only where accounting permits, with available
breakdowns and coverage. Shared-process work must not be counted twice; overlapping
memory measures must not be added. Browser rendering and indirect kernel/device
work are explicit limitations when not measured. Missing cost is never zero.

Preserve this history under the shared storage rules so users can inspect spikes
and compare versions or settings under comparable workloads. Source read durations,
missed reads and scanner run records help explain cost; elapsed duration is not
CPU time. Self-measurement's cost is included in the measured product processes.

## Boundaries

- Respect existing permissions and security boundaries. Missing access is a
  limitation to explain, not a barrier to bypass.
- Remain useful without elevated permissions. Additional access must have a
  specific, disclosed purpose.
- Observe and advise. Decisions to stop programs, delete data, change settings,
  or alter workloads belong to the human or separately authorized agent.
- Collect and retain only information justified by these questions. Technical
  access alone does not justify collection.
- Make the tool's own resource use visible. Monitoring must not quietly become
  another source of strain.
