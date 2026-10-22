"""Private filesystem map. Collection requires the owner's install-time choices."""
from .store import MapQuery, MapStore


def run_scan(data_dir, *, full_disk_access=False, full=False, grain='files', roots=None, access=None, exclusions=(), force=False):
    from .runner import run_scan as run
    return run(data_dir,full_disk_access=full_disk_access,full=full,grain=grain,roots=roots,access=access,exclusions=exclusions,force=force)


def main(config, root=None, force=False):
    import json
    if not config.enabled.get('scanner',False):
        print(json.dumps({'status':'not_enabled','reason':'scanner_not_enabled'}))
        return 2
    options=config.source_options
    # Desired access does not authorize a protected probe. The owner launcher
    # passes a fresh observation directly to run_scan after explicit approval.
    result=run_scan(config.root,full_disk_access=False,
                    full=bool(options.get('scanner_full',False)),grain=options.get('scanner_grain','files'),
                    roots=[root] if root else None,exclusions=config.scanner_exclusions,force=force)
    print(json.dumps(result))
    return 0 if result['status']=='observed' else 1
