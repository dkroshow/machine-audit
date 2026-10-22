"""The reference network rules, shared by collection and historical callers."""
from __future__ import annotations
from dataclasses import dataclass
import math

DEADLINE_NS=900_000_000
GIVEUP_NS=5_000_000_000
BANDS={'internet':((10,'exceptional'),(15,'excellent'),(25,'good'),(50,'fair'),(100,'acceptable'),(200,'poor')),
       'router':((2,'exceptional'),(5,'excellent'),(10,'good'),(30,'fair'),(100,'poor'))}


@dataclass(frozen=True)
class Probe:
    at_ns:int
    scope:str
    outcome:str
    rtt_ns:int|None
    session:str
    interval_s:float=5
    busy:bool=False


def latency_rating(scope,median_ms,p95_ms=None):
    bands=[b for _,b in BANDS[scope]]+['bad']
    band=next((b for edge,b in BANDS[scope] if median_ms<edge),'bad')
    spikes='none'
    if p95_ms is not None:
        spikes='none' if p95_ms<max(2*median_ms,median_ms+20) else 'variable' if p95_ms<max(4*median_ms,median_ms+80) else 'unstable'
    if spikes=='unstable':band=bands[min(len(bands)-1,bands.index(band)+1)]
    return band,spikes


def summarize(probes,scope,*,web_connected=False):
    rows=[p for p in probes if p.scope==scope]
    sampled=[p for p in rows if p.outcome in ('ok','late','lost')]
    rtts=sorted(p.rtt_ns for p in sampled if p.rtt_ns is not None)
    run=longest=0.0
    count=dropouts=0
    previous=None
    for p in rows:
        if previous and (p.session!=previous.session or p.at_ns-previous.at_ns>p.interval_s*1.5e9):
            run=count=0
        if p.outcome in ('lost','no_path'):
            run+=p.interval_s;count+=1
            if count==2 and run>=3:dropouts+=1
        else:run=count=0
        longest=max(longest,run)
        previous=p
    seconds=sum(p.interval_s for p in sampled)
    loss=100*sum(p.outcome=='lost' for p in sampled)/len(sampled) if sampled else None
    median=rtts[(len(rtts)-1)//2]/1e6 if rtts else None
    p95=rtts[math.ceil(.95*len(rtts))-1]/1e6 if rtts else None
    band,spikes=latency_rating(scope,median,p95) if median is not None else (None,None)
    grade='bad' if band=='bad' else 'slow' if band=='poor' else 'fine' if band else None
    if not sampled and any(p.outcome=='no_path' for p in rows):grade='no_path'
    elif loss is not None:
        if (loss>=5 and loss/100*seconds>=10) or longest>=10:grade='bad'
        elif grade is not None and loss>=(1 if scope=='router' else 2):grade='slow' if grade=='fine' else grade
    # Working application traffic shows an ICMP block, not total network failure.
    blocked=scope=='internet' and web_connected and bool(sampled) and all(p.outcome=='lost' for p in sampled)
    if blocked:grade=None
    return {'median_ms':median,'p95_ms':p95,'worst_ms':max(rtts)/1e6 if rtts else None,
            'loss_percent':None if blocked else loss,'probed_s':seconds,
            'late_s':sum(p.interval_s for p in rows if p.outcome=='late'),
            'no_path_s':sum(p.interval_s for p in rows if p.outcome=='no_path'),
            'longest_silence_s':longest,'dropouts':dropouts,'grade':grade,'band':band,'spikes':spikes,
            'icmp_blocked':blocked}


def router_silent(probes):
    """Only a continuous five-minute router silence with internet replies qualifies."""
    router=[p for p in probes if p.scope=='router']
    if not router or sum(p.interval_s for p in router)<300:return False
    if any(p.outcome!='lost' for p in router):return False
    return any(p.scope=='internet' and p.outcome in ('ok','late') for p in probes)


def over_usual(windows,baseline):
    active=False;high=0
    for median in windows:
        high=high+1 if median-baseline>max(.25*baseline,5) else 0
        if not active and high>=2:active=True
        elif active and median-baseline<max(.15*baseline,3):active=False
    return active
