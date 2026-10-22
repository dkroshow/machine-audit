"""Bounded loopback HTTP, with exact Host/Origin and per-run bearer auth."""
from __future__ import annotations
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit


def make_server(query, token: str, port=8765, dashboard=None, allowed_hosts=()):
    if len(token)<32:
        raise ValueError("API token is too short")
    slots=threading.BoundedSemaphore(4)
    waiters=threading.BoundedSemaphore(4)
    root=Path(dashboard).resolve() if dashboard else Path(__file__).resolve().parent.parent/"dashboard"

    class Handler(BaseHTTPRequestHandler):
        server_version="MachineAudit"
        sys_version=""

        def log_message(self,*args):
            pass

        def _send(self,status,payload,kind="application/json; charset=utf-8",timing=None):
            body=payload if isinstance(payload,bytes) else json.dumps(payload,separators=(",",":"),allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy","default-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            if timing:self.send_header("Server-Timing",timing)
            self.end_headers()
            self.wfile.write(body)
            if query.collector:
                with query.collector.lock:
                    query.collector.operations["http_sent_bytes"]+=len(body)

        def do_GET(self):
            port=self.server.server_port
            hosts={f"127.0.0.1:{port}",f"localhost:{port}"}
            origins={f"http://{h}" for h in hosts}
            # A reverse proxy serves https; an ssh tunnel stays plain http.
            hosts.update(allowed_hosts)
            origins.update(f"{scheme}://{h}" for h in allowed_hosts for scheme in ("http","https"))
            if len(self.headers.get_all("Host",[]))!=1 or self.headers["Host"] not in hosts:
                return self._send(403,{"error":"host_not_allowed"})
            if len(self.headers.get_all("Origin",[]))>1 or self.headers.get("Origin") and self.headers["Origin"] not in origins:
                return self._send(403,{"error":"origin_not_allowed"})
            parsed=urlsplit(self.path)
            if parsed.path.startswith("/api/"):
                supplied=self.headers.get("Authorization","")
                if len(self.headers.get_all("Authorization",[]))!=1 or len(supplied)>512 or not hmac.compare_digest(supplied,"Bearer "+token):
                    return self._send(401,{"error":"authentication_required"})
                if not slots.acquire(blocking=False):
                    if not waiters.acquire(blocking=False):
                        return self._send(503,{"error":"query_capacity"})
                    try:
                        acquired=slots.acquire(timeout=2)
                    finally:
                        waiters.release()
                    if not acquired:
                        return self._send(503,{"error":"query_capacity"})
                try:
                    args=parse_qs(parsed.query,keep_blank_values=True)
                    if any(len(v)!=1 for v in args.values()):
                        raise ValueError("duplicate parameter")
                    args={k:v[0] for k,v in args.items()}
                    allowed={"component","signals","signal","start","end","limit","at","span","action","parent","volume","parent_id","volume_id","offset","metric","cursor","brief"}
                    if set(args)-allowed:
                        raise ValueError("unknown parameter")
                    component=args.get("component")
                    limit=int(args.get("limit",600))
                    signals=args.get("signals","").split(",") if args.get("signals") else None
                    route=parsed.path[5:]
                    brief=args.get("brief","0")
                    if brief not in ("0","1") or brief=="1" and route not in ("now","who","series"):
                        raise ValueError("brief is 0 or 1, for now, who and series")
                    brief=brief=="1"
                    if route in {"series","who","coverage"}:
                        bounds=(int(float(args["start"])*1e9),int(float(args["end"])*1e9))
                    began,cpu=time.monotonic(),time.thread_time()
                    if route=="now": result=query.now(component,limit,brief)
                    elif route=="catalog": result=query.catalog()
                    elif route=="status": result=query.status()
                    elif route=="series": result=query.series(*bounds,signals,component,limit,brief)
                    elif route=="who": result=query.who(*bounds,args.get("signal"),component,limit,brief)
                    elif route=="coverage": result=query.coverage(*bounds,component,limit)
                    elif route=="around": result=query.around(int(float(args["at"])*1e9),int(float(args.get("span",60))*1e9),component,signals,limit)
                    elif route=="storage": result=query.storage_query(**args)
                    else: return self._send(404,{"error":"route_not_found"})
                    # The answer's own elapsed and CPU milliseconds; nothing else.
                    timing=f"query;dur={(time.monotonic()-began)*1000:.1f}, cpu;dur={(time.thread_time()-cpu)*1000:.1f}"
                    return self._send(200,result,timing=timing)
                except (ValueError,KeyError,TypeError,OverflowError):
                    return self._send(400,{"error":"invalid_query"})
                except Exception:
                    return self._send(500,{"error":"query_failed"})
                finally:
                    slots.release()
            path=unquote(parsed.path)
            target=(root/("index.html" if path=="/" else path.lstrip("/"))).resolve()
            if not target.is_relative_to(root) or not target.is_file() or target.suffix not in {".html",".js",".mjs",".css",".svg",".png",".ico"}:
                return self._send(404,{"error":"asset_not_found"})
            types={".mjs":"text/javascript; charset=utf-8",".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",".css":"text/css; charset=utf-8",".svg":"image/svg+xml",".png":"image/png",".ico":"image/x-icon"}
            return self._send(200,target.read_bytes(),types[target.suffix])

        def do_POST(self): self._send(405,{"error":"read_only_api"})
        def do_PUT(self): self._send(405,{"error":"read_only_api"})
        def do_DELETE(self): self._send(405,{"error":"read_only_api"})

    server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
    server.daemon_threads=True
    return server


def write_token(path):
    import os
    token=secrets.token_urlsafe(32)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,"O_NOFOLLOW",0),0o600)
    with os.fdopen(fd,"w") as stream:
        os.fchmod(stream.fileno(),0o600)
        stream.write(token+"\n")
    return token
