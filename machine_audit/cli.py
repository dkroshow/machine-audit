"""One entrypoint for collection, queries, and owner-supplied onboarding."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import importlib
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import webbrowser
from .config import Config
from .client import request, ClientError


def parser():
    p=argparse.ArgumentParser(prog="machine-audit")
    p.add_argument("--config")
    p.add_argument("--data-dir")
    commands=p.add_subparsers(dest="command",required=True)
    commands.add_parser("run")
    commands.add_parser("open")
    for command in ("start", "stop", "permissions"):
        commands.add_parser(command)
    for command in ("setup","doctor","install"):
        sub=commands.add_parser(command)
        sub.add_argument("--json",action="store_true")
        if command=="setup":
            # Unattended setup: answers as flags instead of terminal questions.
            sub.add_argument("--non-interactive",action="store_true")
            sub.add_argument("--helper",choices=("yes","no"),default="no")
            sub.add_argument("--hosts",default="",help="comma-separated dashboard host names")
            sub.add_argument("--exclude",action="append",default=[],help="absolute folder path the scanner never enters")
        if command=="install":
            sub.add_argument("--yes",action="store_true",help="install without the confirmation question")
            sub.add_argument("--upgrade",action="store_true",help="replace an existing Linux installation, keeping the old one renamed")
    scan=commands.add_parser("scan")
    scan.add_argument("--root")
    scan.add_argument("--full",action="store_true",help="walk in full now (Linux); one scan at a time still holds")
    for command in ("now","catalog","status","series","who","coverage","around","storage"):
        sub=commands.add_parser(command)
        sub.add_argument("--json",action="store_true")
        sub.add_argument("--component")
        sub.add_argument("--signals")
        sub.add_argument("--signal")
        sub.add_argument("--start",default=str(time.time()-300))
        sub.add_argument("--end",default=str(time.time()))
        sub.add_argument("--at",default=str(time.time()))
        sub.add_argument("--span",default="60")
        sub.add_argument("--limit",type=int,default=600)
        sub.add_argument("--action",default="volumes")
        if command=="storage":
            sub.add_argument("--volume-id")
            sub.add_argument("--parent-id")
            sub.add_argument("--cursor")
    return p


def timestamp(value):
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(timezone.utc).timestamp()


def local_query(store, collector=None, config=None):
    from .query import Query
    from .scanner import MapQuery
    return Query(store, collector, storage=MapQuery(store.path.parent / "scanner.sqlite"),config=config)


def _run(config,ready):
    from .budget import Budget
    from .collector import Collector
    from .serve import make_server,write_token
    from .store import Store
    from .sources import create_sources,boot_id
    from .model import Clock
    budget=Budget(config.root)
    store=Store(config.store_path,budget)
    collector=server=server_thread=None
    try:
        operations={"flush_count":0,"flush_elapsed_s":0.0,"query_count":0,"query_elapsed_s":0.0,"http_sent_bytes":0}
        sources=create_sources(config,budget,operations=operations)
        clock=next((s.clock for s in sources if callable(getattr(s,"clock",None))),Clock.now)
        collector=Collector(store,sources,boot_id=boot_id(),clock=clock,tail_limit_bytes=config.tail_limit_bytes)
        collector.operations=operations
        collector.acquire()
        query=local_query(store,collector,config)
        server=make_server(query,write_token(config.token_path),config.port,allowed_hosts=config.allowed_hosts)
        server_thread=threading.Thread(target=server.serve_forever,daemon=True)
        server_thread.start()
        ready()
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,lambda *_:collector.stop_event.set())
        clean=collector.run()
        if not clean:print("Collector stopped before all pending evidence was saved.",file=sys.stderr)
        return 0 if clean else 2
    finally:
        if server is not None:
            if server_thread is not None and server_thread.is_alive():server.shutdown()
            server.server_close()
        if collector is not None and collector._instance_fd is not None and collector._closer is None:
            collector.close()
        if collector is None or collector._closer is None or not collector._closer.is_alive():store.close()


def main(argv=None):
    args=parser().parse_args(argv)
    config=Config.load(args.config)
    if args.data_dir:
        config.data_dir=args.data_dir
    config.validate()
    if args.command in {"setup","doctor","install"}:
        module=importlib.import_module("machine_audit.installation" if args.command=="install" else "machine_audit.onboarding")
        options={}
        if args.command=="setup" and args.non_interactive:
            options["answers"]={"helper":args.helper=="yes","hosts":[h.strip().lower() for h in args.hosts.split(",") if h.strip()],"exclusions":args.exclude}
        if args.command=="install":
            options.update(yes=args.yes,upgrade=args.upgrade)
        result=getattr(module,args.command)(config,json_output=args.json,**options)
        return result if isinstance(result,int) else 0
    if args.command in {"start", "stop", "permissions"}:
        from .installation import control, check_access
        try:
            return check_access(config) if args.command == "permissions" else control(config,args.command)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print("Installation action failed: " + type(exc).__name__, file=sys.stderr)
            return 2
    if args.command=="scan":
        module=importlib.import_module("machine_audit.scanner")
        return module.main(config,root=args.root,force=args.full)
    if args.command=="run":
        if not (Path(args.config) if args.config else config.path).is_file():
            print("Run machine-audit setup before starting collection.",file=sys.stderr)
            return 2
        from .startup import guarded_run
        return guarded_run(config,lambda ready:_run(config,ready))
    if args.command=="open":
        try:request(config,"status")
        except ClientError as exc:
            print(f"Cannot open dashboard: {exc.code}",file=sys.stderr);return 1
        token=config.token_path.read_text().strip()
        webbrowser.open(f"http://127.0.0.1:{config.port}/#token={token}")
        return 0
    params={"component":args.component,"signals":args.signals,"signal":args.signal,"limit":args.limit}
    if args.command in {"series","who","coverage","storage"}:
        params.update(start=timestamp(args.start),end=timestamp(args.end))
    if args.command=="around":params.update(at=timestamp(args.at),span=args.span)
    if args.command=="storage":params.update(action=args.action,volume_id=args.volume_id,parent_id=args.parent_id,cursor=args.cursor)
    params={k:v for k,v in params.items() if v is not None}
    try:
        result=request(config,args.command,params)
    except ClientError as exc:
        if exc.code!="not_running":
            print(f"Query failed: {exc.code}",file=sys.stderr);return 1
        from .store import Store
        try:store=Store(config.store_path,read_only=True)
        except (FileNotFoundError,PermissionError):
            print("No readable saved history; run setup and start collection.",file=sys.stderr);return 2
        query=local_query(store,config=config)
        try:
            if args.command in {"now","catalog","status"}:result=getattr(query,args.command)()
            elif args.command=="series":result=query.series(int(params["start"]*1e9),int(params["end"]*1e9),args.signals.split(",") if args.signals else None,args.component,args.limit)
            elif args.command=="who":result=query.who(int(params["start"]*1e9),int(params["end"]*1e9),args.signal,args.component,args.limit)
            elif args.command=="coverage":result=query.coverage(int(params["start"]*1e9),int(params["end"]*1e9),args.component,args.limit)
            elif args.command=="around":result=query.around(int(timestamp(args.at)*1e9),int(float(args.span)*1e9),args.component,limit=args.limit)
            else:result=query.storage_query(**params)
        finally:store.close()
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0
