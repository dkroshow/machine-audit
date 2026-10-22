"""Bounded network observations; no addresses or network names enter Readings."""
from __future__ import annotations
import ctypes as C
import hashlib
import hmac
import os
import select
import socket
import struct
import time
import json
import subprocess
import sys
from dataclasses import dataclass
from collections import deque
from .network_rules import Probe, summarize, router_silent, over_usual
import statistics

from .native import I32, U32, U64, bind
from .macos import field

DEADLINE_NS = 900_000_000
GIVEUP_S = 5
INTERNET = ('1.1.1.1', '8.8.8.8')


class IfData(C.Structure):
    _pack_ = 4
    _fields_ = [('bytes8', C.c_ubyte * 8), ('mtu', U32), ('metric', U32)] + [
        (n, U64) for n in 'baudrate ipackets ierrors opackets oerrors collisions ibytes obytes imcasts omcasts iqdrops noproto'.split()
    ] + [('recvtiming', U32), ('xmittiming', U32), ('lastchange', I32 * 2)]


class IfMib(C.Structure):
    _pack_ = 4
    _fields_ = [('name', C.c_char * 16), ('pcount', U32), ('flags', U32), ('snd_len', U32),
                ('snd_maxlen', U32), ('snd_drops', U32), ('filler', U32 * 4), ('data', IfData)]


def checksum(data):
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack('!' + 'H' * (len(data) // 2), data))
    total = (total & 0xffff) + (total >> 16)
    total = (total & 0xffff) + (total >> 16)
    return ~total & 0xffff


@dataclass(frozen=True)
class Ping:
    outcome: str
    rtt_ns: int | None
    sent_bytes: int
    received_bytes: int
    kernel_timestamp: bool


class Pinger:
    """One unprivileged ICMP socket. Timestamp is Darwin uptime nanoseconds."""
    timestamp_option = 0x0800  # SO_TIMESTAMP_MONOTONIC

    def __init__(self, awake_ns):
        self.awake_ns = awake_ns
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        self.sock.setsockopt(socket.SOL_SOCKET, self.timestamp_option, 1)
        self.sock.setblocking(False)
        self.sequence = 0
        self.nonce = os.urandom(16)

    def ping_many(self, addresses):
        pending={}; results={}
        # Both targets leave before waiting. One silent router cannot delay the
        # internet probe, and two failures consume one five-second give-up span.
        for scope,address in addresses.items():
            self.sequence=(self.sequence+1)&65535
            payload=self.nonce+struct.pack('!H',self.sequence)
            header=struct.pack('!BBHHH',8,0,0,os.getpid()&65535,self.sequence)
            packet=struct.pack('!BBHHH',8,0,checksum(header+payload),os.getpid()&65535,self.sequence)+payload
            sent=self.awake_ns()
            self.sock.sendto(packet,(address,0))
            pending[payload]=(scope,address,sent,len(packet)+20)
        deadline=time.monotonic()+GIVEUP_S
        while pending and (remaining:=deadline-time.monotonic())>0:
            if not select.select([self.sock],[],[],remaining)[0]:break
            raw,ancillary,flags,remote=self.sock.recvmsg(65536,128)
            offset=(raw[0]&15)*4 if raw and raw[0]>>4==4 else 0
            if len(raw)<offset+8 or raw[offset]!=0:continue
            key=raw[offset+8:]
            if key not in pending:continue
            scope,address,sent,sent_bytes=pending[key]
            if remote[0]!=address:continue
            received=self.received_ns(ancillary)
            size=len(raw)+(20 if offset==0 else 0)
            if received is None or received<sent:
                results[scope]=Ping('timestamp_unavailable',None,sent_bytes,size,False)
            else:
                rtt=received-sent
                results[scope]=Ping('late' if rtt>DEADLINE_NS else 'ok',rtt,sent_bytes,size,True)
            del pending[key]
        for scope,address,sent,size in pending.values():results[scope]=Ping('lost',None,size,0,True)
        return results

    @staticmethod
    def received_ns(ancillary):
        return next((struct.unpack('=Q',d[:8])[0] for level,kind,d in ancillary
            if level==socket.SOL_SOCKET and kind==4 and len(d)>=8),None)

    def close(self):
        self.sock.close()


def router_hardware(raw, address):
    """Parse NET_RT_FLAGS/RTF_LLINFO replies; return only the matching MAC bytes."""
    target=socket.inet_aton(address)
    offset=0
    while offset+92<=len(raw):
        length=struct.unpack_from('=H',raw,offset)[0]
        if length<92 or offset+length>len(raw):raise ValueError('invalid_route_message')
        mask=struct.unpack_from('=I',raw,offset+12)[0]
        cursor=offset+92;addresses={}
        for index in range(8):
            if not mask & (1<<index):continue
            if cursor+2>offset+length:raise ValueError('short_route_address')
            size=raw[cursor]
            if size<2 or cursor+size>offset+length:raise ValueError('short_route_address')
            addresses[index]=raw[cursor:cursor+size]
            cursor+=(size+3)&~3
        destination,gateway=addresses.get(0,b''),addresses.get(1,b'')
        if len(destination)>=8 and destination[1]==socket.AF_INET and destination[4:8]==target and len(gateway)>=8 and gateway[1]==18:
            name_length,address_length=gateway[5],gateway[6]
            if address_length and 8+name_length+address_length<=len(gateway):
                return gateway[8+name_length:8+name_length+address_length]
        offset+=length
    return None


class Network:
    def __init__(self, native, io, clock, salt):
        self.native, self.io, self.clock, self.salt = native, io, clock, salt
        self.pinger = None
        self.dynamic_store=None
        self.target = 0
        self.sent_bytes = self.received_bytes = 0
        self.last_path = None
        self.history=deque()
        self.web_success_ns=0
        self.last_traffic=None
        self.busy=False
        self.session='unavailable'
        self.last_sleep=None
        self.portal_due=None
        self.portal_checked=0
        self.portal_state=None
        self.portal_clock=None
        self.last_dropout_count=0
        self.session_checked=0
        self.minute_index=None
        self.quiet_minutes={}
        self.window_medians=deque(maxlen=2)

    def interfaces(self):
        n, value, size = self.native, I32(), C.c_size_t(4)
        mib = (I32 * 5)(4, 18, 0, 1, 1)  # CTL_NET, PF_LINK, IFMIB_SYSTEM, IFCOUNT
        n.check(n.sysctl(mib, 5, C.byref(value), C.byref(size), None, 0))
        total=0
        for i in range(1, value.value + 1):
            data, size = IfMib(), C.c_size_t(C.sizeof(IfMib))
            mib = (I32 * 6)(4, 18, 0, 2, i, 1)
            if n.sysctl(mib, 6, C.byref(data), C.byref(size), None, 0):
                continue
            if data.flags & 8 or data.data.bytes8[0] not in (6, 0xf3):
                continue
            total+=data.data.ibytes+data.data.obytes
            entity = f'interface:{i}'
            for key, name, unit in [('ibytes', 'receive', 'bytes'), ('obytes', 'send', 'bytes'),
                                    ('ierrors', 'receive_errors', 'count'), ('oerrors', 'send_errors', 'count')]:
                yield field('network.' + name, getattr(data.data, key), unit, 'counter', entity=entity,
                            scope='interface', additive=True)

        self.traffic(total)

    def traffic(self,total):
        """Own traffic of 10 Mbps or more tags the next probes as taken while busy."""
        now=self.clock().awake_ns
        if self.last_traffic is not None:
            before,t=self.last_traffic
            self.busy=total>=before and now>t and (total-before)*8/(now-t)*1e3>=10
        self.last_traffic=(total,now)

    def path(self):
        sc = C.CDLL('/System/Library/Frameworks/SystemConfiguration.framework/SystemConfiguration')
        if not self.dynamic_store:
            name=self.io.string(None,b'Machine-audit',0x08000100)
            try:self.dynamic_store=bind(sc,'SCDynamicStoreCreate',C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p)(None,name,None,None)
            finally:self.io.release(name)
            if not self.dynamic_store:raise OSError('network_path_read_failed')
        k = self.io.string(None, b'State:/Network/Global/IPv4', 0x08000100)
        try:
            obj = bind(sc, 'SCDynamicStoreCopyValue', C.c_void_p, C.c_void_p, C.c_void_p)(self.dynamic_store, k)
            try:
                return self.io.decode(obj) or {}
            finally:
                if obj:
                    self.io.release(obj)
        finally:
            self.io.release(k)

    def session_id(self,router):
        hardware=self.hardware(router)
        return hmac.new(self.salt,hardware,hashlib.sha256).hexdigest()[:16] if hardware else None

    def hardware(self,router):
        mib=(I32*6)(4,17,0,socket.AF_INET,2,0x400)
        size=C.c_size_t()
        self.native.check(self.native.sysctl(mib,6,None,C.byref(size),None,0))
        if size.value>4*1024**2:raise OSError('route_table_limit')
        buf=C.create_string_buffer(size.value)
        self.native.check(self.native.sysctl(mib,6,buf,C.byref(size),None,0))
        return router_hardware(buf.raw[:size.value],router)

    def make_pinger(self):
        return Pinger(lambda:self.clock().awake_ns)

    def probes(self):
        at=self.clock()
        sleep=at.continuous_ns-at.awake_ns
        if self.last_sleep is not None and sleep-self.last_sleep>1_000_000_000:
            self.history.clear()
        self.last_sleep=sleep
        path=self.path()
        router=path.get('Router')
        path_key=(router,path.get('PrimaryInterface'))
        if path_key!=self.last_path or at.awake_ns-self.session_checked>=60e9:
            if path_key!=self.last_path:self.history.clear()
            self.last_path=path_key
            if path_key!=getattr(self,'portal_path',None):
                self.portal_due=at.awake_ns+5_000_000_000 if router else None
                self.portal_path=path_key
            try:new_session=self.session_id(router) if router else None
            except (OSError,ValueError):new_session=None
            if new_session!=self.session:
                self.history.clear();self.window_medians.clear()
            self.session=new_session;self.session_checked=at.awake_ns
        # Addresses remain only in memory. Router identity is unavailable until
        # an ARP hardware address has been observed; an IP is never substituted.
        yield field('network.path_up',bool(router),'boolean','state')
        yield field('network.session_identity',self.session,'state','state',status='observed' if self.session else 'unavailable')
        results={}
        if router:
            if self.pinger is None:self.pinger=self.make_pinger()
            target_index=self.target%2;self.target+=1
            results=self.pinger.ping_many({'router':router,'internet':INTERNET[target_index]})
        else:
            target_index=0
            results={scope:Ping('no_path',None,0,0,False) for scope in ('router','internet')}
        end=self.clock()
        if end.continuous_ns-at.continuous_ns-(end.awake_ns-at.awake_ns)>1_000_000_000:
            results={scope:Ping('unobserved',None,r.sent_bytes,r.received_bytes,r.kernel_timestamp) for scope,r in results.items()}
        for scope,result in results.items():
            self.sent_bytes+=result.sent_bytes;self.received_bytes+=result.received_bytes
            self.history.append(Probe(at.awake_ns,scope,result.outcome,result.rtt_ns,self.session,busy=self.busy))
            yield field('network.ping_outcome',result.outcome,'state','state',entity=scope,scope=scope)
            yield field('network.ping_rtt',result.rtt_ns,'ns',entity=scope,scope=scope,
                        status='observed' if result.rtt_ns is not None else 'unobserved')
            yield field('network.ping_target_class','router' if scope=='router' else f'internet{target_index+1}',
                        'state','state',entity=scope,scope=scope)
            yield field('network.self_traffic_guard',self.busy,'boolean','state',entity=scope,scope=scope)
        while self.history and at.awake_ns-self.history[0].at_ns>300*1e9:self.history.popleft()
        minute=at.awake_ns//60_000_000_000
        if self.minute_index is not None and minute!=self.minute_index and self.session:
            quiet=[p for p in self.history if p.scope=='internet' and at.awake_ns-p.at_ns<=60e9]
            if len(quiet)>=12 and all(not p.busy and p.outcome in ('ok','late') for p in quiet):
                recent=summarize(quiet,'internet')['median_ms']
                self.quiet_minutes.setdefault(self.session,deque(maxlen=1440)).append(recent)
            current=summarize(self.history,'internet')['median_ms']
            if current is not None:self.window_medians.append(current)
        self.minute_index=minute
        baseline_rows=self.quiet_minutes.get(self.session,())
        baseline=statistics.median(baseline_rows) if len(baseline_rows)>=30 else None
        yield field('network.quiet_baseline',baseline,'ms',scope='internet',status='observed' if baseline is not None else 'unobserved')
        yield field('network.slower_than_usual',over_usual(self.window_medians,baseline) if baseline is not None else None,
                    'boolean','state',scope='internet',status='observed' if baseline is not None else 'unobserved')
        silent=router_silent(self.history)
        for scope in ('router','internet'):
            summary=summarize(self.history,scope,web_connected=at.awake_ns-self.web_success_ns<120e9)
            for name in ('median_ms','p95_ms','worst_ms','loss_percent','probed_s','late_s','longest_silence_s','dropouts'):
                value=summary[name]
                if silent and scope=='router' and name in ('median_ms','p95_ms','worst_ms','loss_percent'):value=None
                yield field('network.window_'+name,value,'ms' if name.endswith('_ms') else 'percent' if name=='loss_percent' else 'count' if name=='dropouts' else 'seconds',
                            entity=scope,scope=scope,status='observed' if value is not None else 'unobserved')
            grade=None if silent and scope=='router' else summary['grade']
            yield field('network.grade',grade,'state','state',entity=scope,scope=scope,status='observed' if grade else 'unobserved')
        inet_summary=summarize(self.history,'internet')
        router_summary=summarize(self.history,'router')
        if inet_summary['dropouts']>self.last_dropout_count and router_summary['median_ms'] is not None:
            self.portal_due=at.awake_ns
        self.last_dropout_count=inet_summary['dropouts']
        yield field('self.probe_send',self.sent_bytes,'bytes','counter',scope='probe',additive=True)
        yield field('self.probe_receive',self.received_bytes,'bytes','counter',scope='probe',additive=True)

    def dns_connect(self):
        # A fixed, isolated probe bounds libc DNS as well as connect. The child
        # emits numeric timings only; no address, response body or exception text.
        program = """import json,resource,socket,time
r={}; t=time.monotonic_ns()
try:
 a=socket.getaddrinfo('www.apple.com',443,type=socket.SOCK_STREAM);r['dns']=time.monotonic_ns()-t
except OSError:a=[]
if a:
 t=time.monotonic_ns()
 try:
  with socket.socket(a[0][0],a[0][1],a[0][2]) as s:s.settimeout(2);s.connect(a[0][4])
  r['connect']=time.monotonic_ns()-t
 except OSError:pass
u=resource.getrusage(resource.RUSAGE_SELF);r['cpu_ns']=int((u.ru_utime+u.ru_stime)*1e9);r['peak_rss']=u.ru_maxrss
print(json.dumps(r))
"""
        try:
            completed = subprocess.run([sys.executable,'-I','-c',program],capture_output=True,timeout=5,check=True)
            data = json.loads(completed.stdout)
        except (subprocess.SubprocessError,ValueError):
            data = {}
        if 'connect' in data:self.web_success_ns=self.clock().awake_ns
        for name in ('dns','connect'):
            yield field('network.'+name,data.get(name),'ns',scope='resolver' if name=='dns' else 'internet',
                        status='observed' if name in data else 'failed')
        yield field('network.resolver','system cached resolver','state','state',scope='resolver')
        self.probe_child_cpu = getattr(self,'probe_child_cpu',0) + data.get('cpu_ns',0)
        yield field('self.probe_child_cpu',self.probe_child_cpu if 'cpu_ns' in data else None,'ns','counter',
                    scope='short_lived_children',scale=1e-9,rate_unit='CPUs',status='observed' if 'cpu_ns' in data else 'unobserved')
        yield field('self.probe_child_peak_memory',data.get('peak_rss'),'bytes',scope='short_lived_children',
                    status='observed' if 'peak_rss' in data else 'unobserved')

    def portal(self):
        now=self.clock()
        if self.portal_due is not None and now.awake_ns>=self.portal_due and now.awake_ns-self.portal_checked>=300e9:
            program="""import json,urllib.request,resource
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
state='failed';count=0
try:
 o=urllib.request.build_opener(NoRedirect())
 with o.open('http://captive.apple.com/hotspot-detect.html',timeout=3) as r:
  b=r.read(4096);count=len(b);state='ok' if r.status==200 and b.rstrip(b'\\r\\n')==b'<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>' else 'portal'
except urllib.error.HTTPError as e:state='portal' if 300<=e.code<400 else 'failed'
except OSError:pass
u=resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({'state':state,'bytes':count,'cpu_ns':int((u.ru_utime+u.ru_stime)*1e9)}))
"""
            try:
                result=subprocess.run([sys.executable,'-I','-c',program],capture_output=True,timeout=5,check=True)
                data=json.loads(result.stdout)
                self.portal_state=data['state']
                yield field('self.portal_receive',data['bytes'],'payload_bytes',scope='probe')
                yield field('self.portal_child_cpu',data['cpu_ns'],'ns',scope='short_lived_children')
            except (subprocess.SubprocessError,ValueError):self.portal_state='failed'
            self.portal_checked=now.awake_ns;self.portal_due=None;self.portal_clock=now
        yield field('network.portal',self.portal_state,'state','state',start=self.portal_clock,end=self.portal_clock,
                    status='observed' if self.portal_state else 'unobserved')

    def wifi(self):
        # Loading CoreWLAN does not request the SSID or BSSID, scan or location.
        C.CDLL('/System/Library/Frameworks/CoreWLAN.framework/CoreWLAN')
        objc=C.CDLL('/usr/lib/libobjc.A.dylib')
        get_class=bind(objc,'objc_getClass',C.c_void_p,C.c_char_p)
        selector=bind(objc,'sel_registerName',C.c_void_p,C.c_char_p)
        address=C.cast(objc.objc_msgSend,C.c_void_p).value
        send=C.CFUNCTYPE(C.c_void_p,C.c_void_p,C.c_void_p)(address)
        client=send(get_class(b'CWWiFiClient'),selector(b'sharedWiFiClient'))
        interface=send(client,selector(b'interface')) if client else None
        if not interface:
            yield field('network.wifi_present',False,'boolean','state')
            return
        yield field('network.wifi_present',True,'boolean','state')
        integer=C.CFUNCTYPE(C.c_long,C.c_void_p,C.c_void_p)(address)
        number=C.CFUNCTYPE(C.c_double,C.c_void_p,C.c_void_p)(address)
        values={}
        for name,method in [('wifi_signal',b'rssiValue'),('wifi_noise',b'noiseMeasurement')]:
            value=integer(interface,selector(method))
            values[name]=value if value else None
            yield field('network.'+name,value if value else None,'dBm',scope='wifi')
        snr=values['wifi_signal']-values['wifi_noise'] if all(v is not None for v in values.values()) else None
        yield field('network.wifi_snr',snr,'dB',scope='wifi')
        value=number(interface,selector(b'transmitRate'))
        yield field('network.wifi_link_rate',value if value>0 else None,'Mbps',scope='wifi')
