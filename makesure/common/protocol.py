"""Wire protocol shared by agent, relay and viewers.

Control messages are JSON text frames: an object with a string ``type`` field.
Video travels in binary frames: a fixed little-endian header followed by one
encoded access unit (an Annex B H.264 access unit or one JPEG image).
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

PROTOCOL_VERSION = 1
MAGIC = b"MS"
FLAG_KEYFRAME = 0x01

# magic(2s) version(B) codec(B) flags(B) seq(I) capture_ts_ms(Q) width(H) height(H)
HEADER = struct.Struct("<2sBBBIQHH")
HEADER_SIZE = HEADER.size
_TS_OFFSET = 9  # byte offset of the timestamp field inside HEADER

MAX_CONTROL_BYTES = 16 * 1024
AUTH_CONTEXT = b"makesure-auth-v1"


class Codec(IntEnum):
    H264 = 1
    JPEG = 2


class MsgType:
    """Names of every control message type in the protocol."""

    # relay -> agent (the complete set an agent will ever act on)
    AUTH_CHALLENGE = "auth_challenge"
    AUTH_RESULT = "auth_result"
    START = "start"
    STOP = "stop"
    KEYFRAME_REQUEST = "keyframe_request"
    QUALITY_HINT = "quality_hint"
    PING = "ping"
    PONG = "pong"
    # agent -> relay
    AUTH = "auth"
    STATUS = "status"
    # relay -> viewer
    STATE = "state"


AGENT_ACCEPTED_TYPES: frozenset[str] = frozenset(
    {
        MsgType.AUTH_CHALLENGE,
        MsgType.AUTH_RESULT,
        MsgType.START,
        MsgType.STOP,
        MsgType.KEYFRAME_REQUEST,
        MsgType.QUALITY_HINT,
        MsgType.PING,
        MsgType.PONG,
    }
)


class ProtocolError(ValueError):
    """Raised when a peer sends malformed data."""


@dataclass(frozen=True, slots=True)
class FrameHeader:
    codec: Codec
    keyframe: bool
    seq: int
    capture_ts_ms: int
    width: int
    height: int

    def pack(self) -> bytes:
        return HEADER.pack(
            MAGIC,
            PROTOCOL_VERSION,
            int(self.codec),
            FLAG_KEYFRAME if self.keyframe else 0,
            self.seq & 0xFFFFFFFF,
            self.capture_ts_ms,
            self.width,
            self.height,
        )


def pack_frame(header: FrameHeader, payload: bytes) -> bytes:
    return header.pack() + payload


def parse_header(data: bytes | bytearray | memoryview) -> FrameHeader:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("frame shorter than header")
    magic, version, codec, flags, seq, ts, width, height = HEADER.unpack_from(data, 0)
    if magic != MAGIC or version != PROTOCOL_VERSION:
        raise ProtocolError("bad magic or version")
    try:
        codec_enum = Codec(codec)
    except ValueError as exc:
        raise ProtocolError(f"unknown codec {codec}") from exc
    if width == 0 or height == 0:
        raise ProtocolError("zero frame size")
    return FrameHeader(codec_enum, bool(flags & FLAG_KEYFRAME), seq, ts, width, height)


def split_frame(data: bytes | bytearray | memoryview) -> tuple[FrameHeader, memoryview]:
    header = parse_header(data)
    payload = memoryview(data)[HEADER_SIZE:]
    if len(payload) == 0:
        raise ProtocolError("empty payload")
    return header, payload


def rewrite_timestamp(buf: bytearray, capture_ts_ms: int) -> None:
    """Overwrite the capture timestamp of a packed frame in place."""
    struct.pack_into("<Q", buf, _TS_OFFSET, max(0, capture_ts_ms))


def encode_control(msg_type: str, **fields: Any) -> str:
    return json.dumps({"type": msg_type, **fields}, separators=(",", ":"))


def decode_control(text: str) -> dict[str, Any]:
    """Parse a control message; raise ProtocolError if it is not well formed."""
    if len(text) > MAX_CONTROL_BYTES:
        raise ProtocolError("control message too large")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON") from exc
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("control message without type")
    return msg


def auth_payload(nonce: bytes, host: str) -> bytes:
    """Bytes an agent signs to prove key possession to one specific relay host."""
    return AUTH_CONTEXT + b"|" + nonce + b"|" + normalize_host(host).encode("utf-8")


def device_id_for(public_key_raw: bytes) -> str:
    """Device ID: ``ms-`` plus 16 base32 characters of SHA-256(raw public key)."""
    digest = hashlib.sha256(public_key_raw).digest()
    return "ms-" + base64.b32encode(digest).decode("ascii").lower()[:16]


def normalize_host(host: str) -> str:
    host = host.strip().lower()
    for default_port in (":443", ":80"):
        if host.endswith(default_port):
            return host[: -len(default_port)]
    return host


def now_ms() -> float:
    """Wall clock in milliseconds; the common clock base for latency maths."""
    return time.time() * 1000.0
