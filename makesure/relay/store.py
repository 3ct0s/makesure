"""Persistent relay state in SQLite: operator password, enrollment tokens, devices.

SQLite lets the CLI (enroll, revoke) and a running relay share state safely.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from makesure.common.config import ensure_private_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operator (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enroll_tokens (
    token_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    used_by TEXT
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    public_key BLOB NOT NULL,
    name TEXT NOT NULL,
    enrolled_at REAL NOT NULL,
    last_seen REAL
);
"""


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: str
    public_key: bytes
    name: str
    enrolled_at: float
    last_seen: float | None


class EnrollResult(Enum):
    OK = "ok"
    UNKNOWN_TOKEN = "unknown_token"
    EXPIRED_TOKEN = "expired_token"
    USED_TOKEN = "used_token"


class Store:
    def __init__(self, data_dir: Path) -> None:
        ensure_private_dir(data_dir)
        self.path = data_dir / "relay.db"
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    # operator password
    def set_password_hash(self, password_hash: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO operator (id, password_hash) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash",
                (password_hash,),
            )

    def password_hash(self) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT password_hash FROM operator WHERE id = 1").fetchone()
        return row[0] if row else None

    # enrollment tokens
    def add_enroll_token(self, token_hash: str, name: str, created_at: float, expires_at: float) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO enroll_tokens (token_hash, name, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, name, created_at, expires_at),
            )

    def enroll_device(
        self, token_hash: str, device_id: str, public_key: bytes, now: float
    ) -> tuple[EnrollResult, str]:
        """Atomically consume a token and register the device under its name."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT name, expires_at, used_at FROM enroll_tokens WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
                if row is None:
                    result = EnrollResult.UNKNOWN_TOKEN
                elif row[2] is not None:
                    result = EnrollResult.USED_TOKEN
                elif row[1] <= now:
                    result = EnrollResult.EXPIRED_TOKEN
                else:
                    result = EnrollResult.OK
                if result is not EnrollResult.OK:
                    db.execute("ROLLBACK")
                    return result, ""
                name = str(row[0])
                db.execute(
                    "UPDATE enroll_tokens SET used_at = ?, used_by = ? WHERE token_hash = ?",
                    (now, device_id, token_hash),
                )
                db.execute(
                    "INSERT INTO devices (device_id, public_key, name, enrolled_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (device_id, public_key, name, now, now),
                )
                db.execute("COMMIT")
                return EnrollResult.OK, name
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def purge_expired_tokens(self, now: float) -> int:
        with self._connect() as db:
            cur = db.execute("DELETE FROM enroll_tokens WHERE used_at IS NULL AND expires_at <= ?", (now,))
            return cur.rowcount

    # devices
    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT device_id, public_key, name, enrolled_at, last_seen FROM devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return DeviceRecord(row[0], bytes(row[1]), row[2], row[3], row[4]) if row else None

    def list_devices(self) -> list[DeviceRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT device_id, public_key, name, enrolled_at, last_seen FROM devices ORDER BY name"
            ).fetchall()
        return [DeviceRecord(r[0], bytes(r[1]), r[2], r[3], r[4]) for r in rows]

    def device_ids(self) -> set[str]:
        with self._connect() as db:
            return {row[0] for row in db.execute("SELECT device_id FROM devices")}

    def touch_device(self, device_id: str, now: float) -> None:
        with self._connect() as db:
            db.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (now, device_id))

    def remove_device(self, device_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,)).rowcount > 0
