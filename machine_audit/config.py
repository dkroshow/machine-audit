"""Private configuration paths shared by runtime and onboarding."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import secrets
import uuid


def default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "machine-audit"


@dataclass
class Config:
    data_dir: str = field(default_factory=lambda: str(default_data_dir()))
    port: int = 8765
    tailscale_host: str | None = None
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tail_limit_bytes: int = 64*1024**2
    enabled: dict[str, bool] = field(default_factory=dict)
    source_options: dict = field(default_factory=dict)
    scanner_exclusions: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def store_path(self) -> Path:
        return self.root / "readings.sqlite"

    @property
    def token_path(self) -> Path:
        return self.root / "api.token"

    @property
    def salt_path(self) -> Path:
        return self.root / "identity.salt"

    def identity_salt(self) -> bytes:
        """Private random key for hashed hardware identities. Never exported: the
        device id travels with every export, so it must not be the key."""
        if self.salt_path.is_symlink():
            raise ValueError("identity salt cannot be a symlink")
        if not self.salt_path.exists():
            self.salt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32))
        salt = self.salt_path.read_text().strip()
        if len(salt) != 64 or any(c not in "0123456789abcdef" for c in salt):
            raise ValueError("identity salt file is damaged")
        return salt.encode()

    @property
    def path(self) -> Path:
        return getattr(self, "_loaded_path", self.root / "config.json")

    def validate(self):
        if not 1 <= self.port <= 65535 or self.tail_limit_bytes < 1024:
            raise ValueError("invalid runtime configuration")
        if not self.root.is_absolute():
            raise ValueError("data_dir must be absolute")
        if self.tailscale_host and (not self.tailscale_host.endswith(".ts.net") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in self.tailscale_host)):
            raise ValueError("tailscale_host must be a bare lower-case ts.net hostname")
        if not isinstance(self.scanner_exclusions, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in self.scanner_exclusions):
            raise ValueError("scanner_exclusions must be a list of absolute paths")
        self.scanner_exclusions = [os.path.normpath(p) for p in self.scanner_exclusions]
        if len(set(self.scanner_exclusions)) != len(self.scanner_exclusions):
            raise ValueError("scanner_exclusions must not repeat a path")
        return self

    def save(self, path: str | Path | None = None):
        self.validate()
        destination = Path(path) if path else self.path
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination.parent,0o700)
        if destination.is_symlink():
            raise ValueError("config cannot be a symlink")
        temporary = destination.with_suffix(".new")
        fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"w") as stream:
                json.dump(asdict(self),stream,indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,destination)
            self._loaded_path = destination.absolute()
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def load(cls, path: str | Path | None = None):
        explicit = path is not None
        path = Path(path).expanduser().absolute() if explicit else default_data_dir()/"config.json"
        if not path.exists():
            config = cls(data_dir=str(path.parent)).validate()
        else:
            if path.is_symlink():
                raise ValueError("config cannot be a symlink")
            config = cls(**json.loads(path.read_text())).validate()
        if explicit:
            config._loaded_path = path
        return config
