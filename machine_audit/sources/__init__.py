"""Source factory. Optional access is explicit; importing never starts collection."""
from __future__ import annotations
import hashlib
import json
import sys
from machine_audit.model import Clock, Reading
from .macos import MacOS, ReadSource, field


class GapSource:
    def __init__(self,name,component,cadence_s,reason,status='not_enabled'):
        self.name,self.component,self.cadence_s,self.reason,self.status=name,component,cadence_s,reason,status
    def read(self):
        now=Clock.now()
        return Reading(self.name,self.component,now,now,(),self.status,self.reason)


class FanSource(ReadSource):
    @property
    def cadence_s(self):
        return self.smc.fan_cadence_s
    @cadence_s.setter
    def cadence_s(self,value):
        pass
    def __init__(self,smc,clock):
        self.smc=smc
        super().__init__('macos.fans','heat',5,smc.fans,clock)


def boot_id():
    if sys.platform != 'darwin':
        raise NotImplementedError('linux_sources_not_implemented')
    from .native import Darwin
    return hashlib.sha256(Darwin().bytes('kern.boottime')).hexdigest()[:32]


def create_sources(config,budget,operations=None,scanner=None):
    if sys.platform != 'darwin':
        return [GapSource('platform.'+c,c,60,'linux_sources_not_implemented','unavailable')
                for c in ('cpu','memory','gpu','storage','network','power','heat','self')]
    from .native import Darwin
    from .iokit import IOKit
    from .devices import Devices
    from .smc import SMC
    from .network import Network
    from .self_cost import SelfCost
    from machine_audit.scanner.native import MacFilesystem
    native,io=Darwin(),IOKit()
    mac=MacOS(native)
    mac.config_identity=hashlib.sha256(json.dumps({'enabled':config.enabled,'source_options':config.source_options},sort_keys=True).encode()).hexdigest()
    mac.install_revision=config.source_options.get('install_revision')
    device,smc=Devices(io,mac.clock),SMC(io,native)
    network=Network(native,io,mac.clock,config.identity_salt())
    own=SelfCost(mac,budget,operations,scanner)
    fs=MacFilesystem(native)
    def volumes():
        for v in fs.volumes():
            for name,value in [('capacity',v.capacity),('free',v.free),('available',v.available),('used',v.used)]:
                yield field('storage.'+name,value,'bytes',entity='volume:'+v.id,scope='volume')
    sources=mac.sources()+[
        ReadSource('macos.disk','storage',5,device.disk,mac.clock),
        ReadSource('macos.capacity','storage',60,volumes,mac.clock),
        ReadSource('macos.heat','heat',5,device.heat,mac.clock),
        ReadSource('macos.thermal_state','heat',5,device.thermal,mac.clock),
        ReadSource('macos.temperatures','heat',5,smc.temperatures,mac.clock),
        ReadSource('macos.sensor_sweep','heat',60,lambda:smc.temperatures(True),mac.clock),
        ReadSource('macos.power','power',5,smc.power,mac.clock),
        ReadSource('macos.supply','power',5,device.supply,mac.clock),FanSource(smc,mac.clock),
        ReadSource('macos.network','network',10,network.interfaces,mac.clock),
        ReadSource('macos.probes','network',5,network.probes,mac.clock),
        ReadSource('macos.dns_connect','network',60,network.dns_connect,mac.clock),
        ReadSource('macos.wifi','network',10,network.wifi,mac.clock),
        ReadSource('macos.portal','network',5,network.portal,mac.clock),
        ReadSource('self.process','self',10,own.processes,mac.clock),
        ReadSource('self.storage','self',60,own.storage,mac.clock),
        ReadSource('self.scanner','self',60,own.scanner_runs,mac.clock),
        GapSource('macos.system_process','cpu',60,'shipped_helper_required'),
        GapSource('macos.package_power','power',30,'shipped_helper_required'),
    ]
    if config.enabled.get('network_speed',False):
        from .speed import SpeedTest
        speed=SpeedTest(budget,mac.clock,seconds=int(config.source_options.get('network_speed_seconds',5)),
                        run_bytes=int(config.source_options.get('network_speed_run_bytes',10_000_000)),
                        daily_bytes=int(config.source_options.get('network_speed_daily_bytes',0)))
        sources.append(ReadSource('macos.capacity_test','network',3600,speed.read,mac.clock))
    else:
        sources.append(GapSource('macos.capacity_test','network',3600,'active_test_not_enabled'))
    if config.enabled.get('gpu',False):
        sources.extend([ReadSource('macos.gpu','gpu',5,device.gpu,mac.clock),
                        ReadSource('macos.gpu_clients','gpu',10,device.clients,mac.clock)])
    else:
        sources.extend([GapSource('macos.gpu','gpu',5,'exclusive_gpu_reader_required'),
                        GapSource('macos.gpu_clients','gpu',10,'exclusive_gpu_reader_required')])
    return [s if config.enabled.get(s.name,True) else GapSource(s.name,s.component,s.cadence_s,'source_disabled') for s in sources]
