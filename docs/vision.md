# Machine-audit

Agents are trained to accomplish tasks, not to care for the machines they run
on. As we delegate more work to agents, we need better visibility into the
resources that work consumes—and whether it interferes with everything else
the machine needs to do.

Machine-audit gives humans and agents a shared view of a machine's health and
performance. It helps them understand what is happening, what changed, which
workloads contribute, and what might improve the situation.

We aim to make that visibility available across operating systems, covering
CPU, memory, GPU, storage, network, power, and heat. Different machines expose
different information. Machine-audit should make those differences clear,
provide useful answers with the access available, and distinguish missing
evidence from a healthy reading.

The [Performance Components](machine-information.md) document defines what we want
to understand about each resource, with history and attribution across them.
It is the conceptual foundation for collection and platform adaptations.

## An adaptable framework

Machine-audit is a framework whose principles and guidance aim to account for
all device types and operating systems. Complete coverage is an ambition that
can never be fully achieved. Other humans and agents should be able to take
the framework and adapt it to their own machines, workloads, and needs.

We will start with macOS and Ubuntu. The principles used to support them should
also guide adaptations for other Linux distributions and other operating
systems. Adaptability does not establish support: each implementation must
make its capabilities and limitations explicit and substantiate its claims
with testing on the systems it serves.

## Useful answers, grounded in evidence

A collection of metrics is not enough. Machine-audit should help answer
practical questions:

- Is this machine struggling, and with what?
- Which programs or workloads are contributing?
- Is this a brief event, a recurring pattern, or a lasting change?
- What could improve the situation, and what evidence supports that suggestion?
- After a change, did performance actually improve?

Machine-audit serves two kinds of users through the same product. Humans use
a dashboard to understand these answers without becoming systems experts.
Agents use their own interface, designed for efficient access to structured
evidence with clear units, time windows, limitations, and uncertainty. Both
experiences deserve deliberate design and should work from the same facts.

Machine-audit observes and advises. It does not kill programs, delete files,
or change system settings to resolve a finding. A recommendation is not
permission to act.

## Worth more than it costs

Machine-audit must earn its place on the machine. Its own CPU use, memory,
storage writes, disk space, and network traffic are part of the product's
performance—not hidden implementation details.

Our goal is to demonstrate that its usefulness outweighs its operating cost.
We will measure that cost, test whether its findings help resolve real
problems, and remove collection that does not justify its burden.

## Trust that can be examined

Monitoring software can reveal sensitive information. Elevated permissions
can make it more useful, but also increase what users must trust.

Machine-audit should provide a useful baseline without elevated permissions.
Each additional permission should have a clear purpose, a bounded scope, and
an explanation of what it enables.

We will develop openly, collect evidence justified by the product's questions,
and limit sensitive detail. Storage and network behavior must be explicit.
We will design for skeptical review and publish independent security audits as
they are completed, including their scope, findings, and our response.

We succeed when Machine-audit installs easily, runs efficiently in the
background, and gives humans and agents reliable evidence to keep their
machines running well.
