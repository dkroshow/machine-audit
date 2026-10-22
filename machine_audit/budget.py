"""Cross-process write admission for a flat, private managed data directory."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import threading

GB = 1_000_000_000
LIMIT_BYTES = 10 * GB
FREE_MARGIN_BYTES = 64 * 1024**2


class BudgetExceeded(RuntimeError):
    pass


def free_space(root: str | Path) -> dict:
    """Free bytes the default cap needs at its worst sqlite_write peak, and free bytes now.

    Managed files already on disk hold their share. Reads only; root may not exist yet.
    """
    root = Path(root)
    used = sum(p.stat().st_size for p in root.iterdir() if p.is_file()) if root.is_dir() else 0
    existing = next(p for p in (root, *root.parents) if p.exists())
    return {"required_free_bytes": max(0, LIMIT_BYTES + FREE_MARGIN_BYTES - used),
            "free_bytes": shutil.disk_usage(existing).free}


class Budget:
    """Hold the shared lock from size check through write/checkpoint completion.

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
            if path.is_symlink() or path.is_dir():
                raise ValueError("managed directory must contain only regular files")
            if path.is_file():
                parts[path.name] = path.stat().st_size
        total = sum(parts.values())
        return {"bytes": total, "limit_bytes": self.limit_bytes, "warning_bytes": self.warning_bytes,
                "warning": total >= self.warning_bytes, "parts": parts,
                "checkpoint_reservation_bytes":sum(size for name,size in parts.items() if name.endswith("-wal")),
                "checkpoint_error":self.last_checkpoint_error}

    @contextmanager
    def reserve(self, peak_bytes: int):
        if peak_bytes < 0:
            raise ValueError("reservation must be nonnegative")
        with self._mutex:
            fd = os.open(self.root / ".budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                usage = self.usage()
                reservation = peak_bytes + usage["checkpoint_reservation_bytes"]
                if usage["bytes"] + reservation > self.limit_bytes:
                    raise BudgetExceeded("managed_storage_limit")
                if shutil.disk_usage(self.root).free < reservation + self.free_margin_bytes:
                    raise BudgetExceeded("free_space_margin")
                yield
                if self.usage()["bytes"] > self.limit_bytes:
                    raise BudgetExceeded("writer_exceeded_reservation")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @contextmanager
    def sqlite_write(self, connection, path: str | Path, growth_bytes: int = 16*1024**2):
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
        if path.parent.resolve() != self.root.resolve() or growth_bytes < 0:
            raise ValueError("invalid SQLite reservation")
        if connection.in_transaction:
            raise ValueError("SQLite admission requires no active transaction")
        with self._mutex:
            fd = os.open(self.root / ".budget.lock", os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX)
                page = connection.execute("PRAGMA page_size").fetchone()[0]
                pages = connection.execute("PRAGMA page_count").fetchone()[0]
                pending_growth = max(0,pages*page-path.stat().st_size)
                # Checkpointing can copy the WAL yet fail to truncate it. Its
                # remaining growth is bounded by the logical database size.
                # A no-growth retry may clear already-backfilled reservation.
                if pending_growth:
                    if self.usage()["bytes"] + pending_growth > self.limit_bytes:
                        raise BudgetExceeded("managed_storage_limit")
                    if shutil.disk_usage(self.root).free < pending_growth + self.free_margin_bytes:
                        raise BudgetExceeded("free_space_margin")
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] or checkpoint[1] != checkpoint[2]:
                    raise BudgetExceeded("checkpoint_pinned_by_reader")
                page = connection.execute("PRAGMA page_size").fetchone()[0]
                existing = connection.execute("PRAGMA page_count").fetchone()[0]
                connection.execute("PRAGMA cache_spill=OFF")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                usage = self.usage()
                own = sum(p.stat().st_size for p in (path,Path(str(path)+"-wal"),Path(str(path)+"-shm")) if p.exists())
                other = usage["bytes"]-own + usage["checkpoint_reservation_bytes"]-usage["parts"].get(path.name+"-wal",0)
                # SHM grows in 32 KiB chunks: each chunk indexes 4062 WAL frames.
                def bound(pages):
                    return pages*page + 32 + pages*(page+24) + ((pages+4061)//4062+1)*32768
                # Payload size does not bound B-tree/index growth. Admit all
                # pages that the proven DB + one-frame-per-page WAL bound fits.
                cap = (self.limit_bytes-other)//(2*page+32)
                while cap >= existing and other+bound(cap)>self.limit_bytes:
                    cap-=1
                if cap < existing:
                    raise BudgetExceeded("sqlite_checkpoint_headroom")
                peak = other+bound(cap)
                if shutil.disk_usage(self.root).free < peak-usage["bytes"]+self.free_margin_bytes:
                    raise BudgetExceeded("free_space_margin")
                connection.execute(f"PRAGMA max_page_count={cap}")
                yield {"max_pages":cap,"peak_bytes":peak,"page_size":page}
                try:
                    checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    self.last_checkpoint_error = "reader_pinned" if checkpoint[0] else None
                except Exception as exc:
                    self.last_checkpoint_error = type(exc).__name__
                # A racing reader may pin the committed WAL. The commit still
                # succeeded; the next write refuses until it can truncate.
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN)
                os.close(fd)
