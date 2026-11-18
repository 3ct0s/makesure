from __future__ import annotations

import re

import pytest

from makesure.common.clock import ClockSync
from makesure.common.protocol import (
    HEADER_SIZE,
    Codec,
    FrameHeader,
    ProtocolError,
    auth_payload,
    decode_control,
    device_id_for,
    encode_control,
    pack_frame,
    parse_header,
    rewrite_timestamp,
    split_frame,
)


def test_frame_round_trip() -> None:
    header = FrameHeader(Codec.H264, True, 4_000_000_000, 1_763_456_789_123, 1280, 720)
    data = pack_frame(header, b"\x00\x00\x00\x01payload")
    assert len(data) == HEADER_SIZE + 11
    parsed, payload = split_frame(data)
    assert parsed == header
    assert bytes(payload) == b"\x00\x00\x00\x01payload"


def test_timestamp_rewrite_in_place() -> None:
    buf = bytearray(pack_frame(FrameHeader(Codec.JPEG, False, 7, 1000, 2, 2), b"x"))
    rewrite_timestamp(buf, 123456)
    parsed = parse_header(buf)
    assert parsed.capture_ts_ms == 123456
    assert (parsed.seq, parsed.codec, parsed.keyframe) == (7, Codec.JPEG, False)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"XS" + bytes(30),
        FrameHeader(Codec.H264, True, 1, 1, 2, 2).pack(),  # no payload
        b"MS\x02" + bytes(30),  # unknown version
        b"MS\x01\x09" + bytes(30),  # unknown codec
    ],
)
def test_bad_frames_rejected(data: bytes) -> None:
    with pytest.raises(ProtocolError):
        split_frame(data)


def test_control_messages() -> None:
    assert decode_control(encode_control("start", viewers=2)) == {"type": "start", "viewers": 2}
    for bad in [
        "not json",
        "[1,2]",
        '{"no": "type"}',
        '{"type": 5}',
        '{"type":"x","p":"' + "a" * 20000 + '"}',
    ]:
        with pytest.raises(ProtocolError):
            decode_control(bad)


def test_device_id_format() -> None:
    device_id = device_id_for(bytes(32))
    assert re.fullmatch(r"ms-[a-z2-7]{16}", device_id)
    assert device_id != device_id_for(b"\x01" * 32)


def test_auth_payload_binds_host() -> None:
    nonce = b"n" * 32
    assert auth_payload(nonce, "Relay.example:443") == auth_payload(nonce, "relay.example")
    assert auth_payload(nonce, "relay.example") != auth_payload(nonce, "other.example")


def test_clock_sync_uses_lowest_rtt_sample() -> None:
    clock = ClockSync()
    # remote is 500 ms ahead; first sample has 200 ms of asymmetric queueing
    clock.add_sample(0, 700, 700, 220)
    clock.add_sample(1000, 1505, 1505, 1010)
    assert clock.rtt_ms == pytest.approx(10)
    assert clock.offset_ms == pytest.approx(500)
