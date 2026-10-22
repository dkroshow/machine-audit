"""Opt-in bounded HTTPS capacity test. Never enabled by the normal baseline."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import http.client
import json
import os
from pathlib import Path
import threading
import time

from .macos import field


class SpeedTest:
    host='speed.cloudflare.com'
    def __init__(self,budget,clock,*,seconds=5,run_bytes=10_000_000,daily_bytes=0):
        if not 1<=seconds<=30 or not 0<run_bytes<=1_000_000_000 or daily_bytes<0:
            raise ValueError('invalid_speed_budget')
        self.budget,self.clock=budget,clock
        self.seconds,self.run_bytes,self.daily_bytes=seconds,run_bytes,daily_bytes
        self.path=budget.root/'network-budget.json'

    def _reserve_transfer(self):
        today=datetime.now(timezone.utc).date().isoformat()
        with self.budget.reserve(8192):
            used=0
            if self.path.exists():
                if self.path.is_symlink():raise ValueError('speed_budget_symlink')
                saved=json.loads(self.path.read_text())
                if saved['day']==today:used=int(saved['reserved_bytes'])
            if used+self.run_bytes>self.daily_bytes:return False
            fd=os.open(self.path,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as out:
                json.dump({'day':today,'reserved_bytes':used+self.run_bytes},out)
                out.flush();os.fsync(out.fileno())
        return True

    def _direction(self,direction,cap):
        deadline=time.monotonic()+self.seconds
        remaining=cap
        total=0
        failures=0
        lock=threading.Lock()
        def stream():
            nonlocal remaining,total,failures
            connection=http.client.HTTPSConnection(self.host,timeout=2)
            try:
                while time.monotonic()<deadline:
                    with lock:
                        count=min(65536,remaining)
                        remaining-=count
                    if count<=0:break
                    try:
                        if direction=='down':
                            connection.request('GET',f'/__down?bytes={count}',headers={'Cache-Control':'no-cache'})
                        else:
                            connection.request('POST','/__up',body=b'0'*count,headers={'Content-Type':'application/octet-stream'})
                        response=connection.getresponse()
                        if response.status!=200:raise OSError('speed_http_error')
                        received=0
                        while time.monotonic()<deadline:
                            chunk=response.read(min(16384,count+1-received))
                            if not chunk:break
                            received+=len(chunk)
                            if received>count:raise OSError('speed_response_limit')
                        measured=received if direction=='down' else count
                        with lock:total+=measured
                    except (OSError,http.client.HTTPException):
                        with lock:failures+=1
                        break
            finally:connection.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:stream(),range(4)))
        return total,failures

    def read(self):
        if not self._reserve_transfer():
            return [field('network.capacity_test',None,'state','state',status='not_enabled')]
        out=[]
        for direction in ('down','up'):
            start=self.clock()
            count,failures=self._direction(direction,self.run_bytes//2)
            end=self.clock()
            elapsed=(end.awake_ns-start.awake_ns)/1e9
            out.extend([
                field('network.capacity_'+direction,count*8/elapsed/1e6 if elapsed>0 and count else None,'Mbps','rate',
                      scope='active_test',start=start,end=end,status='observed' if count and not failures else 'failed'),
                field('network.capacity_'+direction+'_bytes',count,'payload_bytes',scope='active_test',start=start,end=end),
                field('network.capacity_'+direction+'_seconds',elapsed,'seconds',scope='active_test',start=start,end=end)])
        out.append(field('network.capacity_method','bounded HTTPS streams','state','state',scope='active_test'))
        return out
