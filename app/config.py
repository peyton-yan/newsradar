"""Application bootstrap settings.

Only infrastructure settings live here.  Business settings are stored in the
``settings`` table and edited from the web UI.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


APP_VERSION = "0.1.0"


@dataclass(frozen=True)
class Settings:
    env: str = "production"
    db_path: Path = Path("/var/lib/newsradar/newsradar.db")
    master_key_file: Path = Path("/etc/newsradar/master.key")
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            env=os.getenv("NR_ENV", "production"),
            db_path=Path(os.getenv("NR_DB_PATH", "/var/lib/newsradar/newsradar.db")),
            master_key_file=Path(os.getenv("NR_MASTER_KEY_FILE", "/etc/newsradar/master.key")),
            host=os.getenv("NR_HOST", "127.0.0.1"),
            port=int(os.getenv("NR_PORT", "8080")),
            log_level=os.getenv("NR_LOG_LEVEL", "INFO").upper(),
        )

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.master_key_file.parent.mkdir(parents=True, exist_ok=True)


def load_or_create_master_key(path: Path) -> bytes:
    """Load the Fernet master key, generating it with mode 0600 on first run."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        path.write_bytes(key + b"\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    try:
        Fernet(key)
    except Exception as exc:  # noqa: BLE001 - report a precise startup error
        raise RuntimeError(f"invalid Fernet master key: {path}") from exc
    return key


def session_secret_from_master_key(master_key: bytes) -> str:
    """Derive a separate session signing key from the master key.

    Rotating the master key invalidates existing sessions, which is the desired
    behaviour.  The master key itself is never used as a cookie secret.
    """

    return hashlib.sha256(b"newsradar-session-v1:" + master_key).hexdigest()
