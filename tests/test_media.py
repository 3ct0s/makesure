"""Encoders, scaling and adaptive quality."""

from __future__ import annotations

import itertools

import av
import numpy as np
import pytest

from makesure.agent.adaptive import AdaptiveController, QualityLevel
from makesure.agent.capture import CapturedFrame, TestPatternCapture
from makesure.agent.encoder import H264Encoder, JpegEncoder, fit_size


def nal_units(data: bytes) -> list[bytes]:
    units, i = [], 0
    starts = []
    while (i := data.find(b"\x00\x00\x01", i)) != -1:
        starts.append(i + 3)
        i += 3
    for n, start in enumerate(starts):
        end = starts[n + 1] - 3 if n + 1 < len(starts) else len(data)
        units.append(data[start:end].rstrip(b"\x00"))
    return units


@pytest.mark.parametrize(
    ("src", "box", "expected"),
    [
        ((1920, 1080), (1280, 720), (1280, 720)),
        ((1366, 768), (1280, 720), (1280, 718)),
        ((1024, 768), (1280, 720), (960, 720)),
        ((800, 600), (1280, 720), (800, 600)),  # never upscale
        ((1513, 983), (1280, 720), (1108, 720)),
    ],
)
def test_fit_size(src: tuple[int, int], box: tuple[int, int], expected: tuple[int, int]) -> None:
    w, h = fit_size(*src, *box)
    assert (w, h) == expected
    assert w % 2 == 0 and h % 2 == 0


def test_h264_baseline_annexb_with_parameter_sets_on_keyframes() -> None:
    capture = TestPatternCapture(640, 360)
    encoder = H264Encoder(640, 360, 15, 1500, keyframe_interval_s=2.0, preset="ultrafast")
    frames = [encoder.encode(capture.grab(), force_keyframe=(i == 40)) for i in range(61)]
    assert all(f is not None for f in frames)
    keyframes = [i for i, f in enumerate(frames) if f and f.keyframe]
    assert keyframes[:3] == [0, 30, 40]  # 2 s GOP at 15 fps, plus the forced one
    decoder = av.CodecContext.create("h264", "r")
    for f in frames:
        assert f is not None
        assert f.payload.startswith(b"\x00\x00\x00\x01") or f.payload.startswith(b"\x00\x00\x01")
        types = [u[0] & 0x1F for u in nal_units(f.payload)]
        if f.keyframe:
            assert 7 in types and 8 in types and 5 in types
            sps = next(u for u in nal_units(f.payload) if u[0] & 0x1F == 7)
            assert sps[1] == 66  # profile_idc baseline
        else:
            assert 7 not in types and 5 not in types
        decoded = decoder.decode(av.Packet(f.payload))
        assert decoded and decoded[0].width == 640


def test_h264_bitrate_change_takes_effect() -> None:
    rng = np.random.default_rng(0)
    texture = rng.integers(0, 256, (360, 640, 4), dtype=np.uint8)
    encoder = H264Encoder(640, 360, 15, 2000, keyframe_interval_s=10.0, preset="ultrafast")
    step = iter(range(10_000))

    def kbps(n: int) -> float:
        size = 0
        for _ in range(n):
            out = encoder.encode(CapturedFrame(np.roll(texture, next(step) * 7, axis=1)), False)
            size += len(out.payload) if out else 0
        return size * 8 * 15 / n / 1000

    kbps(5)
    high = kbps(45)
    encoder.set_bitrate(300)
    kbps(15)
    low = kbps(45)
    assert high > 1200
    assert low < 600


def test_jpeg_encoder() -> None:
    capture = TestPatternCapture(640, 360)
    encoder = JpegEncoder(320, 180, 15, 800)
    out = encoder.encode(capture.grab(), force_keyframe=False)
    assert out is not None and out.keyframe
    assert out.payload[:2] == b"\xff\xd8" and out.payload[-2:] == b"\xff\xd9"
    assert (out.width, out.height) == (320, 180)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_adaptive_degrades_bitrate_then_fps_then_resolution() -> None:
    clock = FakeClock()
    ctl = AdaptiveController(1500, 300, 4000, 15, clock=clock)
    levels: list[QualityLevel] = []
    for _ in range(40):
        clock.now += 1.0
        ctl.record_skip()
        changed = ctl.evaluate()
        if changed:
            levels.append(changed)
    bitrates = [lvl.bitrate_kbps for lvl in levels]
    first_fps_drop = next(i for i, lvl in enumerate(levels) if lvl.fps < 15)
    first_scale_drop = next(i for i, lvl in enumerate(levels) if lvl.scale < 1.0)
    assert bitrates[first_fps_drop - 1] == 300
    assert levels[first_scale_drop - 1].fps == 5
    assert ctl.level == QualityLevel(300, 5, 0.5)


def test_adaptive_respects_cooldown_and_recovers_slowly() -> None:
    clock = FakeClock()
    ctl = AdaptiveController(1500, 300, 4000, 15, clock=clock)
    clock.now = 1.0
    ctl.on_quality_hint(drops=2, backlog=0)
    assert ctl.evaluate() == QualityLevel(1050, 15, 1.0)
    clock.now = 2.0
    ctl.on_quality_hint(drops=2, backlog=0)
    assert ctl.evaluate() is None  # within the 2 s cooldown
    changes = []
    for _ in range(30):
        clock.now += 1.0
        changed = ctl.evaluate()
        if changed:
            changes.append((clock.now, changed))
    assert changes, "healthy stream should step back up"
    assert changes[0][0] - 2.0 >= 8.0
    assert all(b[0] - a[0] >= 8.0 for a, b in itertools.pairwise(changes))
    assert changes[0][1].bitrate_kbps > 1050


def test_adaptive_slow_sends_count_as_congestion() -> None:
    clock = FakeClock()
    ctl = AdaptiveController(1500, 300, 4000, 15, clock=clock)
    ctl.record_send(0.2, 0)  # far longer than a 66 ms frame interval
    clock.now = 1.5
    assert ctl.evaluate() is not None
