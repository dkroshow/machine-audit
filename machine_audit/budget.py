"""Cross-process write admission for a flat, private managed data directory."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import uuid

GB = 1_000_000_000
LIMIT_BYTES = 10 * GB
FREE_MARGIN_BYTES = 64 * 1024**2


class BudgetExceeded(RuntimeError):
    pass


class _ReservationBusy(Exception):
    pass


def free_space(root: str | Path) -> dict:
    """Free bytes the default cap needs at its worst sqlite_write peak, and free bytes now.

    Managed files already on disk hold their share. Reads only; root may not exist yet.
    """
    root = Path(root)
    used = 0
    if root.is_dir():
        for path in root.iterdir():
            try:info=path.stat()
            except FileNotFoundError:continue
            if stat.S_ISREG(info.st_mode):used+=info.st_size
    existing = next(p for p in (root, *root.parents) if p.exists())
    return {"required_free_bytes": max(0, LIMIT_BYTES + FREE_MARGIN_BYTES - used),
            "free_bytes": shutil.disk_usage(existing).free}


class Budget:
    """Reserve write peaks without holding the shared lock during the work.

    Writers reserve a conservative peak, including SQLite journal growth. All
    managed files live directly in root. Never write outside this protocol.
    """
    def __init__(self, root: str | Path, limit_bytes: int = LIMIT_BYTES,
                 warning_bytes: int = 5 * GB, free_margin_bytes: int = FREE_MARGIN_BYTES):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("data directory cannot be a symlink")
        os.chmod(self.root, 0o700)
        self.limit_bytes = limit_bytes
        self.warning_bytes = warning_bytes
        self.free_margin_bytes = free_margin_bytes
        self._mutex = threading.RLock()
        self.last_checkpoint_error = None

    def usage(self) -> dict:
        """Report physical bytes separately from the reserved WAL-copy allowance."""
        parts = {}
        for path in self.root.iterdir():
            try:info=path.lstat()
            except FileNotFoundError:continue  # A completed reservation vanished.
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("managed directory must contain only regular files")
            parts[path.name] = info.st_size
        total = sum(parts.values())
        return {"bytes": total, "limit_bytes": self.limit_bytes, "warning_bytes": self.warning_bytes,
                "warning": total >= self.warning_bytes, "parts": parts,
                "checkpoint_reservation_bytes":sum(size for name,size in parts.items() if name.endswith("-wal")),
                "checkpoint_error":self.last_checkpoint_error}

    @contextmanager
    def _guard(self):
        # Only admission metadata is protected here. Never execute SQLite or
        # caller work while holding this cross-store lock.
        with self._mutex:
            fd = os.open(self.root / ".budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @contextmanager
    def _claim(self, resource=None):
        # This lock marks liveness only; nobody waits for it. A crashed process
        # releases it automatically, so its unused reservation is reclaimable.
        path=self.root/('.budget-'+uuid.uuid4().hex+'.claim')
        with self._guard():
            fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        claim=(path,fd,resource)
        try:
            yield claim
        finally:
            try:
                with self._guard():path.unlink()
            finally:os.close(fd)

    def _active_claims(self, exclude):
        claims=[]
        for path in self.root.glob('.budget-*.claim'):
            identity=path.name[8:-6]
            if len(identity)!=32 or any(c not in '0123456789abcdef' for c in identity):continue
            if path==exclude:continue
            fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    data=os.read(fd,513)
                    if not data:continue  # Created, but no work admitted yet.
                    try:
                        resource,peak=json.loads(data)
                        if (len(data)!=512 or not isinstance(peak,int) or peak<0 or
                            (resource is not None and (not isinstance(resource,str) or Path(resource).name!=resource))):
                            raise ValueError()
                    except (ValueError,TypeError):
                        raise BudgetExceeded('invalid_budget_reservation') from None
                    claims.append((resource,peak,path))
                else:path.unlink()  # No live writer owns this leftover claim.
            finally:os.close(fd)
        return claims

    @staticmethod
    def _sqlite_bytes(usage,name):
        return sum(usage['parts'].get(name+suffix,0) for suffix in ('','-wal','-shm'))

    def _admit(self,claim,peak,*,check_free=True):
        while True:
            try:return self._admit_once(claim,peak,check_free=check_free)
            except _ReservationBusy as busy:
                # Wait for one conflicting reservation, never the shared lock.
                # Unlink/close or process death wakes this wait without polling.
                try:fd=os.open(busy.args[0],os.O_RDONLY|os.O_NOFOLLOW)
                except FileNotFoundError:continue
                try:fcntl.flock(fd,fcntl.LOCK_SH)
                finally:os.close(fd)

    def _admit_once(self,claim,peak,*,check_free):
        path,fd,resource=claim
        with self._guard():
            claims=self._active_claims(path)
            if resource is not None:
                for name,_,other_path in claims:
                    if name==resource:raise _ReservationBusy(other_path)
            usage=self.usage()
            resources={name for name,_,_ in claims if name is not None}
            if resource is not None:resources.add(resource)
            allowances=[(max(0,maximum-self._sqlite_bytes(usage,name)) if name is not None else maximum,other_path)
                        for name,maximum,other_path in claims]
            extra=sum(amount for amount,_ in allowances)
            wait_path=next((other_path for amount,other_path in allowances if amount),None)
            extra+=sum(size for name,size in usage['parts'].items()
                       if name.endswith('-wal') and name[:-4] not in resources)
            own=self._sqlite_bytes(usage,resource) if resource is not None else 0
            # Reservation records have fixed bounded size and count in the cap.
            other=usage['bytes']+512-path.stat().st_size+extra-own
            try:maximum=peak(other,own) if callable(peak) else peak
            except BudgetExceeded:
                if wait_path:raise _ReservationBusy(wait_path) from None
                raise
            total=other+maximum
            if total>self.limit_bytes:
                if wait_path:raise _ReservationBusy(wait_path)
                raise BudgetExceeded('managed_storage_limit')
            if check_free and shutil.disk_usage(self.root).free < max(0,total-usage['bytes'])+self.free_margin_bytes:
                raise BudgetExceeded('free_space_margin')
            data=json.dumps([resource,maximum],separators=(',',':')).encode()
            if len(data)>512:raise ValueError('invalid SQLite reservation name')
            if os.pwrite(fd,data.ljust(512,b' '),0)!=512:
                raise OSError('reservation_write_failed')
            return maximum

    @contextmanager
    def reserve(self, peak_bytes: int):
        if peak_bytes < 0:raise ValueError('reservation must be nonnegative')
        with self._claim() as claim:
            self._admit(claim,peak_bytes)
            yield
            if self.usage()['bytes']>self.limit_bytes:
                raise BudgetExceeded('writer_exceeded_reservation')

    @contextmanager
    def sqlite_write(self, connection, path: str | Path, growth_bytes: int = 16*1024**2,
                     *, max_growth_bytes: int | None = None):
        """Bound one SQLite writer's DB + WAL + checkpoint peak.

        SQLite cannot spill transaction pages. With a cleared WAL and a fixed
        page count, each dirty page contributes at most one WAL frame. Reserve
        the entire possible DB and WAL, including checkpoint overlap. A pinned
        reader prevents WAL truncation and therefore refuses this write.
        Before checkpointing, public page counts bound its remaining DB growth;
        the owning WAL is not charged twice once its pages are already copied.
        Other stores' WAL-copy allowances remain reserved for the new write.
        """
        path = Path(path)
        if (path.parent.resolve() != self.root.resolve() or growth_bytes < 0 or
            (max_growth_bytes is not None and max_growth_bytes<0)):
            raise ValueError("invalid SQLite reservation")
        if connection.in_transaction:
            raise ValueError("SQLite admission requires no active transaction")
        with self._claim(path.name) as claim:
            # Claim this database before querying SQLite; another compliant
            # writer cannot change its page count during checkpoint admission.
            self._admit(claim,lambda other,own:own,check_free=False)
            page = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            pending_growth = max(0,pages*page-path.stat().st_size)
            self._admit(claim,lambda other,own:own+pending_growth,check_free=bool(pending_growth))
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint[0] or checkpoint[1] != checkpoint[2]:
                raise BudgetExceeded("checkpoint_pinned_by_reader")
            # Drop the completed checkpoint allowance before competing for a
            # write reservation; two idle checkpoint claims must not deadlock.
            self._admit(claim,lambda other,own:own,check_free=False)
            page = connection.execute("PRAGMA page_size").fetchone()[0]
            existing = connection.execute("PRAGMA page_count").fetchone()[0]
            connection.execute("PRAGMA cache_spill=OFF")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            admission={}
            def peak(other,own):
                # SHM grows in 32 KiB chunks: each chunk indexes 4062 WAL frames.
                def bound(pages):
                    return pages*page + 32 + pages*(page+24) + ((pages+4061)//4062+1)*32768
                # Payload size does not bound B-tree/index growth. Admit all
                # pages that the proven DB + one-frame-per-page WAL bound fits.
                cap = (self.limit_bytes-other)//(2*page+32)
                if max_growth_bytes is not None:
                    cap=min(cap,existing+(max_growth_bytes+page-1)//page)
                while cap >= existing and other+bound(cap)>self.limit_bytes:
                    cap-=1
                if cap < existing:
                    raise BudgetExceeded("sqlite_checkpoint_headroom")
                admission.update(max_pages=cap,peak_bytes=other+bound(cap),page_size=page)
                return bound(cap)
            self._admit(claim,peak)
            connection.execute(f"PRAGMA max_page_count={admission['max_pages']}")
            try:
                yield admission
                if connection.in_transaction:
                    raise ValueError('SQLite transaction must finish inside reservation')
                try:
                    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    self.last_checkpoint_error = "reader_pinned" if checkpoint[0] else None
                except Exception as exc:
                    self.last_checkpoint_error = type(exc).__name__
                # A racing reader may pin the committed WAL. The commit still
                # succeeded; the next write refuses until it can truncate.
            finally:
                if connection.in_transaction:connection.execute('ROLLBACK')
