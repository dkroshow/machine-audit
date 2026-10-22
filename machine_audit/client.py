"""Authenticated local client shared by CLI and onboarding diagnostics."""
from __future__ import annotations
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

MAX_RESPONSE_BYTES=8*1024**2
SHAPES=frozenset({"now","catalog","status","series","who","coverage","around","storage"})


class ClientError(RuntimeError):
    def __init__(self,code: str):
        self.code=code
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ClientError("redirect_refused")


def request(config,shape: str,params=None) -> dict:
    config.validate()
    if shape not in SHAPES:
        raise ClientError("unknown_query")
    path=config.token_path
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ClientError("token_permissions")
        with path.open() as stream:token=stream.read(129).strip()
    except FileNotFoundError:
        raise ClientError("not_running") from None
    except OSError:
        raise ClientError("token_unreadable") from None
    if not 32 <= len(token) <= 128 or not all(c.isalnum() or c in "-_" for c in token):
        raise ClientError("invalid_token")
    query=urlencode({k:v for k,v in (params or {}).items() if v is not None})
    url=f"http://127.0.0.1:{config.port}/api/{shape}"+("?"+query if query else "")
    opener=build_opener(ProxyHandler({}),NoRedirect())
    req=Request(url,headers={"Authorization":"Bearer "+token,"Accept":"application/json"})
    try:
        with opener.open(req,timeout=15) as response:
            body=response.read(MAX_RESPONSE_BYTES+1)
            if len(body)>MAX_RESPONSE_BYTES:
                raise ClientError("response_too_large")
    except HTTPError as exc:
        raise ClientError({401:"authentication_failed",403:"access_denied",400:"invalid_query",503:"query_capacity"}.get(exc.code,"server_error")) from None
    except (URLError,TimeoutError,OSError):
        raise ClientError("not_running") from None
    try:
        result=json.loads(body)
        if not isinstance(result,dict):raise ValueError()
        return result
    except (ValueError,UnicodeError):
        raise ClientError("invalid_response") from None
