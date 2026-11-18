"""Operator and device authentication."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from makesure.common.config import LoginLimits
from makesure.common.protocol import auth_payload, device_id_for, normalize_host
from makesure.relay.store import EnrollResult, Store

log = logging.getLogger(__name__)

# scrypt parameters: N=2^15, r=8 needs 32 MiB per hash.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_enroll_token() -> str:
    return secrets.token_urlsafe(24)


class SessionManager:
    """Operator sessions held in memory, keyed by the SHA-256 of the cookie value."""

    MAX_SESSIONS = 256

    def __init__(self, lifetime_s: float) -> None:
        self.lifetime_s = lifetime_s
        self._sessions: OrderedDict[str, float] = OrderedDict()

    def create(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        self._prune(now)
        token = secrets.token_urlsafe(32)
        self._sessions[hash_token(token)] = now + self.lifetime_s
        while len(self._sessions) > self.MAX_SESSIONS:
            self._sessions.popitem(last=False)
        return token

    def validate(self, token: str | None, now: float | None = None) -> bool:
        if not token or len(token) > 128:
            return False
        now = time.time() if now is None else now
        expiry = self._sessions.get(hash_token(token))
        if expiry is None:
            return False
        if expiry <= now:
            self._sessions.pop(hash_token(token), None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(hash_token(token), None)

    def _prune(self, now: float) -> None:
        for key in [k for k, exp in self._sessions.items() if exp <= now]:
            del self._sessions[key]


class LoginLimiter:
    """Per-client-IP failure counting with a lockout."""

    MAX_TRACKED = 10_000

    def __init__(self, limits: LoginLimits) -> None:
        self._limits = limits
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, ip: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        until = self._locked_until.get(ip, 0.0)
        if until <= now:
            self._locked_until.pop(ip, None)
            return 0.0
        return until - now

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if len(self._failures) >= self.MAX_TRACKED:
            self._prune(now)
        window = self._failures.setdefault(ip, deque(maxlen=self._limits.max_failures))
        window.append(now)
        recent = [t for t in window if now - t <= self._limits.window_s]
        if len(recent) >= self._limits.max_failures:
            self._locked_until[ip] = now + self._limits.lockout_s
            window.clear()
            log.warning("login locked for %s for %.0f s after repeated failures", ip, self._limits.lockout_s)

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def _prune(self, now: float) -> None:
        stale = [ip for ip, w in self._failures.items() if not w or now - w[-1] > self._limits.window_s]
        for ip in stale:
            del self._failures[ip]
        if len(self._failures) >= self.MAX_TRACKED:
            self._failures.clear()


@dataclass(frozen=True, slots=True)
class AgentAuthResult:
    ok: bool
    device_id: str = ""
    name: str = ""
    reason: str = ""


def _b64(value: Any, length: int) -> bytes | None:
    if not isinstance(value, str) or len(value) > 200:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == length else None


def verify_agent(
    store: Store, msg: dict[str, Any], nonce: bytes, accepted_hosts: set[str], now: float
) -> AgentAuthResult:
    """Check an agent's ``auth`` reply to our challenge; enroll it if it brings a token."""
    public_key = _b64(msg.get("public_key"), 32)
    signature = _b64(msg.get("signature"), 64)
    host = msg.get("host")
    if public_key is None or signature is None or not isinstance(host, str) or len(host) > 255:
        return AgentAuthResult(False, reason="malformed")
    device_id = device_id_for(public_key)
    if msg.get("device_id") != device_id:
        return AgentAuthResult(False, reason="device_id_mismatch")
    if normalize_host(host) not in accepted_hosts:
        log.warning("agent %s signed for host %r, not one of ours", device_id, host)
        return AgentAuthResult(False, device_id, reason="wrong_host")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, auth_payload(nonce, host))
    except InvalidSignature:
        return AgentAuthResult(False, device_id, reason="bad_signature")
    record = store.get_device(device_id)
    if record is not None:
        if not hmac.compare_digest(record.public_key, public_key):
            return AgentAuthResult(False, device_id, reason="key_mismatch")
        return AgentAuthResult(True, device_id, record.name)
    token = msg.get("enroll_token")
    if not isinstance(token, str) or not token or len(token) > 200:
        return AgentAuthResult(False, device_id, reason="unknown_device")
    result, name = store.enroll_device(hash_token(token), device_id, public_key, now)
    if result is not EnrollResult.OK:
        log.warning("enrollment of %s refused: %s", device_id, result.value)
        return AgentAuthResult(False, device_id, reason="enrollment_token_invalid")
    log.info("enrolled new device %s as %r", device_id, name)
    return AgentAuthResult(True, device_id, name)
