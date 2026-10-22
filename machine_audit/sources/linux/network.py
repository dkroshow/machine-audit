"""Linux network sources on the shared probe rules. Addresses and interface names
stay in memory; readings carry interface indexes and a salted router hash."""
from __future__ import annotations

from dataclasses import replace
import socket
import struct
import time

from ..macos import field
from ..network import DEADLINE_NS, GIVEUP_S, Network, Ping, Pinger
from .files import optional

SO_TIMESTAMPNS = 35  # asm-generic/socket.h: receive time as a CLOCK_REALTIME timespec
LINK_STATES = frozenset({'up', 'down', 'dormant', 'lowerlayerdown', 'notpresent', 'testing', 'unknown'})
# /proc/net/dev columns after the name: receive bytes, packets, errs, drop, fifo, frame,
# compressed, multicast, then transmit bytes, packets, errs, drop, ...
DEV = (('receive', 0, 'bytes'), ('receive_errors', 2, 'count'), ('receive_drops', 3, 'count'),
       ('send', 8, 'bytes'), ('send_errors', 10, 'count'), ('send_drops', 11, 'count'))
SNMP = {'Tcp': ('InSegs', 'OutSegs', 'RetransSegs', 'InErrs', 'OutRsts', 'AttemptFails', 'EstabResets', 'CurrEstab'),
        'Udp': ('InDatagrams', 'OutDatagrams', 'InErrors', 'NoPorts', 'RcvbufErrors', 'SndbufErrors')}


def icmp_permitted():
    """Ping sockets need one of our groups inside net.ipv4.ping_group_range."""
    try:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
        return True
    except PermissionError:
        return False


class LinuxPinger(Pinger):
    """The kernel stamps replies in CLOCK_REALTIME, so the send time uses the same clock."""
    timestamp_option = SO_TIMESTAMPNS

    def __init__(self):
        super().__init__(time.time_ns)

    @staticmethod
    def received_ns(ancillary):
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS and len(data) >= 16:
                seconds, nanoseconds = struct.unpack('=qq', data[:16])
                return seconds * 10**9 + nanoseconds
        return None


class TcpProber:
    """Used when ping sockets are not permitted: connect time to port 443 of the internet
    target, timed in user space; a reset is a reply too. A router offers no TCP service
    to assume, so its ping is the labelled gap icmp_not_permitted."""
    port = 443

    def ping_many(self, addresses):
        results = {}
        for scope, address in addresses.items():
            if scope == 'router':
                results[scope] = Ping('icmp_not_permitted', None, 0, 0, False)
                continue
            sent = time.monotonic_ns()
            try:
                socket.create_connection((address, self.port), timeout=GIVEUP_S).close()
                answered = True
            except ConnectionRefusedError:
                answered = True
            except OSError:
                answered = False  # timed out or unreachable
            rtt = time.monotonic_ns() - sent if answered else None
            outcome = 'lost' if rtt is None else 'late' if rtt > DEADLINE_NS else 'ok'
            results[scope] = Ping(outcome, rtt, 0, 0, False)
        return results

    def close(self):
        pass


class LinuxNetwork(Network):
    def __init__(self, root, clock, salt, icmp):
        super().__init__(None, None, clock, salt)
        self.root, self.icmp = root, icmp

    def make_pinger(self):
        return LinuxPinger() if self.icmp else TcpProber()

    def path(self):
        """The default IPv4 route with the lowest metric, as the Mac path keys."""
        best = None
        for line in (self.root / 'proc/net/route').read_text().splitlines()[1:]:
            f = line.split()
            # Destination and mask 0, flags RTF_UP|RTF_GATEWAY; addresses are little-endian hex.
            if len(f) >= 8 and f[1] == f[7] == '00000000' and int(f[3], 16) & 3 == 3:
                if best is None or int(f[6]) < best[0]:
                    best = (int(f[6]), socket.inet_ntoa(struct.pack('<I', int(f[2], 16))), f[0])
        return {'Router': best[1], 'PrimaryInterface': best[2]} if best else {}

    def hardware(self, router):
        for line in (self.root / 'proc/net/arp').read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[0] == router and int(f[2], 16) & 2:  # ATF_COM: resolved
                return bytes.fromhex(f[3].replace(':', ''))
        return None

    def probes(self):
        for f in super().probes():
            if not self.icmp and f.name in ('self.probe_send', 'self.probe_receive'):
                f = replace(f, value=None, status='unobserved')  # handshake bytes are not observed
            yield f
        yield field('network.probe_method', 'icmp' if self.icmp else 'tcp_connect', 'state', 'state', scope='internet')

    def interfaces(self):
        total = 0
        for line in (self.root / 'proc/net/dev').read_text().splitlines()[2:]:
            name, _, counters = line.partition(':')
            values = [int(v) for v in counters.split()]
            sysfs = self.root / 'sys/class/net' / name.strip()
            if (sysfs / 'type').read_text().strip() == '772':  # ARPHRD_LOOPBACK
                continue
            kw = dict(entity='interface:' + (sysfs / 'ifindex').read_text().strip(), scope='interface')
            for key, index, unit in DEV:
                yield field('network.' + key, values[index], unit, 'counter', additive=True, **kw)
            # A down interface is a state to show, not a failed read.
            state = (optional(sysfs / 'operstate') or 'unknown').strip()
            yield field('network.link_state', state if state in LINK_STATES else 'unknown', 'state', 'state', **kw)
            physical = (sysfs / 'device').exists()  # bridges, veth and tunnels carry traffic twice
            yield field('network.physical', physical, 'boolean', 'state', **kw)
            try:
                speed = int((sysfs / 'speed').read_text())
            except (OSError, ValueError):
                speed = -1  # absent, or EINVAL while the link is down
            yield field('network.link_speed', speed if speed > 0 else None, 'Mbps', **kw)
            if physical:
                total += values[0] + values[8]
        self.traffic(total)
        headers = {}
        for line in (self.root / 'proc/net/snmp').read_text().splitlines():
            protocol, _, rest = line.partition(':')
            if protocol not in headers:
                headers[protocol] = rest.split()
                continue
            row = dict(zip(headers[protocol], rest.split()))
            for key in SNMP.get(protocol, ()):
                yield field(f'network.{protocol.lower()}.{key}', int(row[key]) if key in row else None, 'count',
                            'gauge' if key == 'CurrEstab' else 'counter')

    def dns_connect(self):
        resolv = optional(self.root / 'etc/resolv.conf') or ''
        stub = any(line.split()[:2] == ['nameserver', '127.0.0.53'] for line in resolv.splitlines())
        for f in super().dns_connect():
            yield replace(f, value='systemd-resolved stub') if f.name == 'network.resolver' and stub else f

    def wifi(self):
        # /proc/net/wireless is the coarse source: signal and noise, no link rate (nl80211 is not read).
        lines = (optional(self.root / 'proc/net/wireless') or '').splitlines()[2:]
        yield field('network.wifi_present', bool(lines), 'boolean', 'state')
        for line in lines:
            name, _, rest = line.partition(':')
            f = rest.split()
            kw = dict(entity='interface:' + (self.root / 'sys/class/net' / name.strip() / 'ifindex').read_text().strip(),
                      scope='wifi')
            # Zero is no reading, and -256 dBm is the driver's "no noise value".
            signal, noise = (float(v.rstrip('.')) for v in f[2:4])
            signal, noise = signal or None, noise if noise not in (0, -256) else None
            yield field('network.wifi_signal', signal, 'dBm', **kw)
            yield field('network.wifi_noise', noise, 'dBm', **kw)
            yield field('network.wifi_snr', signal - noise if signal is not None and noise is not None else None, 'dB', **kw)
