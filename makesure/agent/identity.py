"""Device identity: an Ed25519 keypair kept in the per-user data directory."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from makesure.common.config import ensure_private_dir
from makesure.common.protocol import device_id_for

log = logging.getLogger(__name__)

KEY_FILE = "device_key.pem"


@dataclass(frozen=True)
class DeviceIdentity:
    private_key: Ed25519PrivateKey

    @property
    def public_key_raw(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @property
    def device_id(self) -> str:
        return device_id_for(self.public_key_raw)

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)


def load_or_create_identity(data_dir: Path) -> DeviceIdentity:
    ensure_private_dir(data_dir)
    path = data_dir / KEY_FILE
    if path.exists():
        return _load(path)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:  # another instance created it first
        return _load(path)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    if sys.platform != "win32":
        path.chmod(0o600)
    identity = DeviceIdentity(key)
    log.info("generated new device identity %s in %s", identity.device_id, data_dir)
    return identity


def _load(path: Path) -> DeviceIdentity:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} does not hold an Ed25519 key")
    return DeviceIdentity(key)
