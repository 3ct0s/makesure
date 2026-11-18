"""Relay routing: fan-out, slow viewers, start/stop and state notifications."""

from __future__ import annotations

import asyncio
import json

import pytest

from makesure.common.protocol import Codec, FrameHeader, now_ms, parse_header
from makesure.relay import registry as registry_module
from makesure.relay.registry import Registry

DEVICE = "ms-aaaaaaaaaaaaaaaa"


class FakeSocket:
    def __init__(self, blocked: bool = False) -> None:
        self.texts: list[dict[str, object]] = []
        self.frames: list[bytes] = []
        self.gate = asyncio.Event()
        if not blocked:
            self.gate.set()
        self.closed_with: int | None = None

    async def send_text(self, data: str) -> None:
        self.texts.append(json.loads(data))

    async def send_bytes(self, data: bytes) -> None:
        await self.gate.wait()
        self.frames.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed_with = code

    def types(self) -> list[object]:
        return [t["type"] for t in self.texts]


def frame(seq: int, keyframe: bool, ts: int = 1_000_000) -> bytes:
    return FrameHeader(Codec.H264, keyframe, seq, ts, 640, 360).pack() + b"\x00\x00\x00\x01\x65" + bytes(200)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_slow_viewer_does_not_stall_fast_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_module, "KEYFRAME_REQUEST_MIN_INTERVAL_S", 0.0)
    registry = Registry(viewer_queue_frames=4)
    agent_ws = FakeSocket()
    link = await registry.attach_agent(DEVICE, "Kiosk", agent_ws)
    fast_ws, slow_ws = FakeSocket(), FakeSocket(blocked=True)
    registry.add_viewer(DEVICE, "Kiosk", fast_ws)
    slow = registry.add_viewer(DEVICE, "Kiosk", slow_ws)
    await settle()
    agent_ws.texts.clear()
    for seq in range(120):
        registry.on_agent_frame(link, frame(seq, keyframe=seq % 30 == 0))
        await settle()
    assert [parse_header(f).seq for f in fast_ws.frames] == list(range(120))
    assert slow.backlog <= 4
    assert slow.dropped > 0
    assert "keyframe_request" in agent_ws.types()
    # after the slow viewer recovers it resumes at a keyframe
    slow_ws.gate.set()
    registry.on_agent_frame(link, frame(120, keyframe=False))
    registry.on_agent_frame(link, frame(121, keyframe=True))
    await settle()
    assert parse_header(slow_ws.frames[-1]).seq == 121
    registry.send_quality_hints()
    await settle()
    hint = next(t for t in agent_ws.texts if t["type"] == "quality_hint")
    assert int(hint["drops"]) > 0  # type: ignore[call-overload]
    await registry.close_all()


async def test_fan_out_and_start_stop_on_first_and_last_viewer() -> None:
    registry = Registry(viewer_queue_frames=5)
    agent_ws = FakeSocket()
    link = await registry.attach_agent(DEVICE, "Kiosk", agent_ws)
    await settle()
    assert "start" not in agent_ws.types()  # nobody watching yet
    a_ws, b_ws = FakeSocket(), FakeSocket()
    a = registry.add_viewer(DEVICE, "Kiosk", a_ws)
    await settle()
    assert {"type": "start", "viewers": 1} in agent_ws.texts
    b = registry.add_viewer(DEVICE, "Kiosk", b_ws)
    await settle()
    assert {"type": "start", "viewers": 2} in agent_ws.texts
    assert agent_ws.types().count("keyframe_request") == 1  # second viewer joined a live stream
    for seq in range(10):
        registry.on_agent_frame(link, frame(seq, keyframe=seq == 0))
        await settle()
    assert len(a_ws.frames) == len(b_ws.frames) == 10
    await registry.remove_viewer(a)
    await settle()
    assert agent_ws.texts[-1] == {"type": "start", "viewers": 1}
    await registry.remove_viewer(b)
    await settle()
    assert agent_ws.texts[-1] == {"type": "stop"}
    await registry.close_all()


async def test_viewer_waits_for_keyframe() -> None:
    registry = Registry(viewer_queue_frames=5)
    link = await registry.attach_agent(DEVICE, "Kiosk", FakeSocket())
    ws = FakeSocket()
    registry.add_viewer(DEVICE, "Kiosk", ws)
    registry.on_agent_frame(link, frame(0, keyframe=False))
    registry.on_agent_frame(link, frame(1, keyframe=True))
    registry.on_agent_frame(link, frame(2, keyframe=False))
    await settle()
    assert [parse_header(f).seq for f in ws.frames] == [1, 2]
    await registry.close_all()


async def test_viewers_told_about_offline_and_pause() -> None:
    registry = Registry(viewer_queue_frames=5)
    viewer_ws = FakeSocket()
    registry.add_viewer(DEVICE, "Kiosk", viewer_ws)
    await settle()
    assert viewer_ws.texts[-1]["online"] is False
    agent_ws = FakeSocket()
    link = await registry.attach_agent(DEVICE, "Kiosk", agent_ws)
    await settle()
    assert {"type": "start", "viewers": 1} in agent_ws.texts  # viewer was already waiting
    assert viewer_ws.texts[-1]["online"] is True and viewer_ws.texts[-1]["streaming"] is True
    registry.on_agent_status(link, paused=True)
    await settle()
    assert viewer_ws.texts[-1]["paused"] is True
    registry.on_agent_status(link, paused=False)
    await settle()
    assert agent_ws.texts[-1] == {"type": "start", "viewers": 1}
    await registry.detach_agent(link)
    await settle()
    assert viewer_ws.texts[-1]["online"] is False
    await registry.close_all()


async def test_timestamps_rewritten_into_relay_clock() -> None:
    registry = Registry(viewer_queue_frames=5)
    link = await registry.attach_agent(DEVICE, "Kiosk", FakeSocket())
    t0 = now_ms()
    # agent clock runs 5000 ms ahead of the relay
    registry.on_agent_pong(link, {"t0": t0, "t1": t0 + 5000, "t2": t0 + 5000})
    ws = FakeSocket()
    registry.add_viewer(DEVICE, "Kiosk", ws)
    registry.on_agent_frame(link, frame(0, keyframe=True, ts=int(t0) + 5000))
    await settle()
    assert abs(parse_header(ws.frames[0]).capture_ts_ms - t0) < 50
    await registry.close_all()


async def test_revoked_device_is_disconnected() -> None:
    registry = Registry(viewer_queue_frames=5)
    agent_ws, viewer_ws = FakeSocket(), FakeSocket()
    await registry.attach_agent(DEVICE, "Kiosk", agent_ws)
    registry.add_viewer(DEVICE, "Kiosk", viewer_ws)
    await registry.disconnect_unknown({DEVICE})
    assert agent_ws.closed_with is None
    await registry.disconnect_unknown(set())
    assert agent_ws.closed_with == 4403 and viewer_ws.closed_with == 4404
    await registry.close_all()
