"""Observed tracked-folder hierarchy with a compact postorder query index.

Called inside the map publisher's transaction and shared budget reservation.
No historical placement is inferred from today's mutable map.
"""
from __future__ import annotations
import hashlib
import json
from .errors import ScanError


def save_hierarchy(db, generation, volume, observed_ns, *, volume_key, delta=None):
    """`generation` overlaid by a published `delta` is the published map read here."""
    from .store import published
    visible = published('nodes', 'n', ':g', ':v', ':d')
    names = dict(g=generation, v=volume_key, d=delta)
    tracked = {r['id']: r for r in db.execute(f'''
        SELECT t.id,source_id(n.parent) AS parent,n.label,n.id IS NOT NULL AS present
        FROM tracked t LEFT JOIN nodes n
          ON n.generation IN (:g,:d) AND n.volume=:v AND n.id=inode_key(t.id) AND {visible}
        WHERE t.volume=:volume ORDER BY t.id''', dict(names, volume=volume))}
    parent_cache = {}
    parents = {}
    for identity, row in tracked.items():
        parent = row['parent']
        visited = {identity}
        while parent is not None and parent not in tracked:
            if parent in visited:
                raise ScanError('hierarchy_cycle')
            visited.add(parent)
            if parent not in parent_cache:
                found = db.execute(f'''SELECT source_id(n.parent) AS parent FROM nodes n
                    WHERE n.generation IN (:g,:d) AND n.volume=:v AND n.id=inode_key(:id) AND {visible}''',
                    dict(names, id=parent)).fetchone()
                parent_cache[parent] = found['parent'] if found else None
            parent = parent_cache[parent]
        parents[identity] = parent
    # A version changes only when placement, names or presence change, not sizes.
    evidence = [(i,r['parent'],parents[i],r['label'],r['present']) for i,r in tracked.items()]
    version = hashlib.sha256(json.dumps(evidence,separators=(',',':')).encode()).hexdigest()
    exists = db.execute('SELECT 1 FROM hierarchy_nodes WHERE volume=? AND version=? LIMIT 1',
                        (volume,version)).fetchone()
    if not exists:
        children = {}
        for identity,parent in parents.items():
            children.setdefault(parent,[]).append(identity)
        for group in children.values():
            group.sort(reverse=True)
        stack = [(i,False) for i in children.get(None,[])]
        visiting, finished, starts = set(), set(), {}
        rank = 0
        while stack:
            identity, exiting = stack.pop()
            if exiting:
                visiting.remove(identity)
                rank += 1
                row = tracked[identity]
                db.execute('INSERT INTO hierarchy_nodes VALUES(?,?,?,?,?,?,?,?)',
                           (volume,version,identity,row['parent'],row['label'],row['present'],rank,starts[identity]))
                finished.add(identity)
                continue
            if identity in visiting or identity in finished:
                raise ScanError('hierarchy_cycle')
            visiting.add(identity)
            starts[identity] = rank+1
            stack.append((identity,True))
            stack.extend((child,False) for child in children.get(identity,[]))
        if len(finished) != len(tracked):
            raise ScanError('hierarchy_cycle')
    # Record every actual observation, reusing unchanged hierarchy rows. This
    # also pins size evidence when a requested interval has not finished yet.
    db.execute('INSERT INTO hierarchy_epochs VALUES(?,?,?)',(volume,observed_ns,version))
