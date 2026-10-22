"""Portable checks use synthetic values tied to reviewed source receipts."""
import ctypes
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from machine_audit.model import Clock, Reading
from machine_audit.sources.native import RUsage4, TaskVM, VMStats
from machine_audit.sources.macos import MacOS, ReadSource, field
from machine_audit.sources.smc import Param, admitted_sensors, decode
from machine_audit.sources.devices import Devices
from machine_audit.sources.network import checksum, Ping


class SourceTests(unittest.TestCase):
    def test_darwin_struct_offsets_follow_installed_sdk(self):
        # SDK layouts, not implementation-derived expected values; mistakes corrupt reads.
        self.assertEqual(ctypes.sizeof(RUsage4),296)
        self.assertEqual(RUsage4.proc_start_abstime.offset,80)
        self.assertEqual(RUsage4.runnable_time.offset,288)
        self.assertEqual(ctypes.sizeof(VMStats),152)
        self.assertEqual(TaskVM.decompressions.offset,344)
        self.assertEqual(TaskVM.ledger_swapins.offset,348)
        self.assertEqual(ctypes.sizeof(Param),80)

    def test_raw_counter_and_per_process_clocks_survive_bundled_pass(self):
        native=Mock()
        native.numer,native.denom=125,3
        native.bytes.side_effect=lambda name: struct.pack('=4i',100,10000,0,100) if name=='kern.clockrate' else b'boot'
        native.absolute.side_effect=range(100,10000,100)
        native.continuous.side_effect=range(100,10000,100)
        native.processes.return_value=[7]
        r=RUsage4();r.proc_start_abstime=100;r.user_time=123;r.system_time=456
        native.process.return_value=r
        native.process_path.return_value='/usr/bin/python3'
        native.task_memory.side_effect=PermissionError()
        mac=MacOS(native)
        fields=mac.processes()
        user=next(f for f in fields if f.name=='process.user_time')
        self.assertEqual(user.value,123)
        self.assertEqual(user.unit,'mach_ticks')
        self.assertEqual(user.rate_unit,'CPUs')
        self.assertIsNotNone(user.start)
        self.assertGreater(user.end.awake_ns,user.start.awake_ns)
        gap=next(f for f in fields if f.name=='process.ledger_swapins')
        self.assertIsNone(gap.value);self.assertEqual(gap.status,'denied')
        self.assertFalse(any(f.name=='process.cpu' for f in fields))

    def test_failure_is_gap_and_exception_path_is_never_stored(self):
        def fail():raise PermissionError('/private/customer-secret')
        reading=ReadSource('test.source','cpu',1,fail).read()
        self.assertEqual(reading.status,'denied')
        self.assertEqual(reading.reason,'native_read_failed')
        self.assertNotIn('customer',repr(reading))

    def test_smc_receipts_exclude_relative_duplicate_and_sentinel_channels(self):
        values={'TC0P':('sp78',42),'TC0p':('sp78',37),'TCXR':('sp78',40),'TG0E':('sp78',50),
                'TA0P':('sp78',0),'TH0a':('sp78',32)}
        self.assertEqual(set(admitted_sensors(values)),{'TC0P','TH0a'})
        self.assertEqual(decode('sp78',b'\x2a\x80'),42.5)
        self.assertEqual(decode('fpe2',b'\x1f\x40'),2000)

    def test_gpu_client_creator_name_never_becomes_a_label(self):
        io=Mock();io.objects.return_value=[1];io.identity.side_effect=lambda obj:obj+100
        io.children.return_value=[2]
        io.property.side_effect=lambda obj,key:{'accumulatedGPUTime':123,'IOUserClientCreator':'pid 7, Private Customer Report'}.get(key)
        fields=list(Devices(io).clients())
        self.assertEqual(next(f.value for f in fields if f.name=='gpu.client_time'),123)
        self.assertNotIn('Private',repr(fields))
        self.assertEqual(next(f.value for f in fields if f.name=='gpu.client_pid'),7)

    def test_icmp_checksum_matches_known_echo_header(self):
        self.assertEqual(checksum(bytes.fromhex('0800000012340001')),0xe5ca)
        self.assertEqual(Ping('lost',None,46,0,True).rtt_ns,None)

    def test_self_metadata_names_install_revision_and_distinguishes_missing_or_invalid(self):
        from machine_audit.sources.self_cost import SelfCost
        budget=Mock();budget.usage.return_value={'bytes':0,'parts':{}}
        for revision,status in [('c'*40,'observed'),(None,'unobserved'),('/private/not-a-revision','invalid')]:
            with self.subTest(status=status):
                mac=SimpleNamespace(config_identity='a'*64,install_revision=revision)
                fields={f.name:f for f in SelfCost(mac,budget,registry=Mock()).storage()}
                self.assertEqual(fields['self.config_identity'].value,'a'*64)
                result=fields['self.install_revision']
                self.assertEqual(result.value,revision if status=='observed' else None)
                self.assertEqual(result.status,status)
                self.assertEqual(result.unit,'git_commit')

    def test_storage_part_identity_survives_reordering_and_new_files(self):
        from machine_audit.sources.self_cost import SelfCost
        budget=Mock()
        mac=SimpleNamespace(config_identity='a'*64,install_revision=None)
        own=SelfCost(mac,budget,registry=Mock())
        snapshots=[{'readings.sqlite':10,'scanner.sqlite':20,'private-a':3},
                   {'private-b':4,'scanner.sqlite-wal':5,'scanner.sqlite':21,'private-a':3,'readings.sqlite':11}]
        rows=[]
        for files in snapshots:
            budget.usage.return_value={'bytes':sum(files.values()),'parts':files}
            rows.append({f.entity:f.value for f in own.storage() if f.name=='self.storage_part'})
        self.assertEqual(rows[0],{'storage:readings:db':10,'storage:scanner:db':20,'storage:other:files':3})
        self.assertEqual(rows[1],{'storage:readings:db':11,'storage:scanner:db':21,'storage:scanner:wal':5,'storage:other:files':7})

class NetworkRulesTests(unittest.TestCase):
    def test_late_is_not_loss_and_dropout_needs_two_pings(self):
        from machine_audit.sources.network_rules import Probe,summarize
        rows=[Probe(0,'internet','late',1_000_000_000,'s'),Probe(5_000_000_000,'internet','lost',None,'s')]
        result=summarize(rows,'internet')
        self.assertEqual(result['loss_percent'],50)
        self.assertEqual(result['late_s'],5)
        self.assertEqual(result['dropouts'],0)
        rows.append(Probe(10_000_000_000,'internet','lost',None,'s'))
        self.assertEqual(summarize(rows,'internet')['dropouts'],1)

    def test_silent_router_and_working_connect_are_unknown_not_bad(self):
        from machine_audit.sources.network_rules import Probe,summarize,router_silent
        rows=[]
        for i in range(60):
            rows.extend([Probe(i*5*10**9,'router','lost',None,'s'),Probe(i*5*10**9,'internet','ok',20_000_000,'s')])
        self.assertTrue(router_silent(rows))
        self.assertFalse(router_silent(rows[2:]))
        result=summarize([Probe(0,'internet','lost',None,'s')],'internet',web_connected=True)
        self.assertTrue(result['icmp_blocked']);self.assertIsNone(result['grade'])

    def test_arp_parser_returns_hardware_without_persisting_network_address(self):
        from machine_audit.sources.network import router_hardware
        address=bytes([16,2,0,0,192,0,2,1])+bytes(8)
        hardware=bytes([14,18,0,0,6,0,6,0])+bytes.fromhex('001122334455')+bytes(2)
        header=bytearray(92);struct.pack_into('=H',header,0,124);struct.pack_into('=I',header,12,3)
        self.assertEqual(router_hardware(bytes(header)+address+hardware,'192.0.2.1'),bytes.fromhex('001122334455'))

if __name__=='__main__':unittest.main()
