"""Headless MakeSure viewer for measurements.

Logs in, watches one device for N seconds, decodes the stream with PyAV and
reports FPS, bitrate, end-to-end latency (same clock-sync scheme as the web
viewer), keyframes and sequence gaps. Optionally saves one decoded frame as PNG
and measures time-to-first-frame over K reconnects.

    python tools/probe_viewer.py --url http://127.0.0.1:8080 --password-env MS_PASS \
        --device ms-xxxxxxxxxxxxxxxx --seconds 30 --snapshot out.png --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import av
import httpx
import websockets
from av.video.codeccontext import VideoCodecContext
from websockets.asyncio.client import ClientConnection

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from makesure.common.clock import ClockSync
from makesure.common.protocol import Codec, ProtocolError, now_ms, split_frame


@dataclass
class Measurement:
    frames_received: int = 0
    frames_decoded: int = 0
    keyframes: int = 0
    seq_gaps: int = 0
    bytes_received: int = 0
    resolution: str = ""
    latencies_ms: list[float] = field(default_factory=list)
    decoded_per_second: dict[int, int] = field(default_factory=dict)
    bytes_per_second: dict[int, int] = field(default_factory=dict)
    states: list[dict[str, Any]] = field(default_factory=list)
    first_frame_s: float | None = None

    def summary(self) -> dict[str, Any]:
        # 1 s windows from the first to the last decoded frame; the first and
        # last windows are partial and left out when there are enough others
        windows: list[int] = []
        kbps: list[float] = []
        if self.decoded_per_second:
            first, last = min(self.decoded_per_second), max(self.decoded_per_second)
            span = range(first + 1, last) if last - first >= 2 else range(first, last + 1)
            windows = [self.decoded_per_second.get(s, 0) for s in span]
            kbps = [self.bytes_per_second.get(s, 0) * 8 / 1000 for s in span]
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> float | None:
            if not lat:
                return None
            return round(lat[min(len(lat) - 1, int(p / 100 * len(lat)))], 1)

        return {
            "frames_received": self.frames_received,
            "frames_decoded": self.frames_decoded,
            "fps_mean": round(statistics.fmean(windows), 2) if windows else 0.0,
            "fps_min": min(windows) if windows else 0,
            "fps_windows": windows,
            "resolution": self.resolution,
            "bitrate_kbps_mean": round(statistics.fmean(kbps), 1) if kbps else 0.0,
            "latency_ms_mean": round(statistics.fmean(lat), 1) if lat else None,
            "latency_ms_p50": pct(50),
            "latency_ms_p95": pct(95),
            "keyframes": self.keyframes,
            "seq_gaps": self.seq_gaps,
            "time_to_first_frame_s": round(self.first_frame_s, 3) if self.first_frame_s else None,
        }


class Probe:
    def __init__(self, ws_url: str, cookie: str, origin: str) -> None:
        self.ws_url = ws_url
        self.cookie = cookie
        self.origin = origin
        self.clock = ClockSync()
        self.last_frame: av.VideoFrame | None = None

    async def watch(self, seconds: float) -> Measurement:
        result = Measurement()
        started = time.monotonic()
        decoders: dict[Codec, VideoCodecContext] = {}
        last_seq: int | None = None
        async with websockets.connect(
            self.ws_url,
            origin=self.origin,  # type: ignore[arg-type]
            additional_headers={"Cookie": f"ms_session={self.cookie}"},
            compression=None,
            max_size=16 * 1024 * 1024,
        ) as ws:
            pinger = asyncio.create_task(self._ping_loop(ws))
            try:
                while (remaining := seconds - (time.monotonic() - started)) > 0:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except TimeoutError:
                        break
                    if isinstance(message, str):
                        self._on_control(message, result)
                        continue
                    try:
                        header, payload = split_frame(message)
                    except ProtocolError:
                        continue
                    elapsed = time.monotonic() - started
                    second = int(elapsed)
                    result.frames_received += 1
                    result.bytes_received += len(message)
                    result.bytes_per_second[second] = result.bytes_per_second.get(second, 0) + len(message)
                    result.keyframes += header.keyframe
                    if last_seq is not None and header.seq != last_seq + 1:
                        result.seq_gaps += max(0, header.seq - last_seq - 1)
                    last_seq = header.seq
                    decoder = decoders.get(header.codec)
                    if decoder is None:
                        name = "h264" if header.codec is Codec.H264 else "mjpeg"
                        decoder = cast(VideoCodecContext, av.CodecContext.create(name, "r"))
                        decoders[header.codec] = decoder
                    try:
                        frames = decoder.decode(av.Packet(bytes(payload)))
                    except av.FFmpegError:
                        continue
                    for frame in frames:
                        shown = time.monotonic() - started
                        result.frames_decoded += 1
                        result.decoded_per_second[int(shown)] = (
                            result.decoded_per_second.get(int(shown), 0) + 1
                        )
                        result.resolution = f"{frame.width}x{frame.height}"
                        if result.first_frame_s is None:
                            result.first_frame_s = shown
                        if self.clock.ready:
                            relay_now = now_ms() + self.clock.offset_ms
                            result.latencies_ms.append(relay_now - header.capture_ts_ms)
                        self.last_frame = frame
            finally:
                pinger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pinger
        return result

    def _on_control(self, text: str, result: Measurement) -> None:
        msg = json.loads(text)
        if msg.get("type") == "pong":
            self.clock.add_sample(msg["t0"], msg["t1"], msg["t2"], now_ms())
        elif msg.get("type") == "state":
            result.states.append(msg)

    async def _ping_loop(self, ws: ClientConnection) -> None:
        for _ in range(5):
            await ws.send(json.dumps({"type": "ping", "t0": now_ms()}))
            await asyncio.sleep(0.2)
        while True:
            await ws.send(json.dumps({"type": "ping", "t0": now_ms()}))
            await asyncio.sleep(2.0)


def save_png(frame: av.VideoFrame, path: Path) -> None:
    rgb = frame.reformat(format="rgb24")
    encoder = av.CodecContext.create("png", "w")
    encoder.width = rgb.width
    encoder.height = rgb.height
    encoder.pix_fmt = "rgb24"
    packets = encoder.encode(rgb) + encoder.encode(None)
    path.write_bytes(b"".join(bytes(p) for p in packets))


async def login(base_url: str, password: str) -> str:
    async with httpx.AsyncClient(base_url=base_url, timeout=20) as http:
        response = await http.post("/api/login", json={"password": password}, headers={"Origin": base_url})
        response.raise_for_status()
        cookie = response.cookies.get("ms_session")
    if not cookie:
        raise SystemExit("login succeeded but no session cookie was set")
    return cookie


async def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.url.rstrip("/")
    password = os.environ.get(args.password_env)
    if not password:
        raise SystemExit(f"environment variable {args.password_env} is empty")
    cookie = await login(base_url, password)
    ws_url = "ws" + base_url[len("http") :] + f"/ws/view/{args.device}"
    probe = Probe(ws_url, cookie, base_url)
    measurement = await probe.watch(args.seconds)
    report: dict[str, Any] = {"device": args.device, "seconds": args.seconds, **measurement.summary()}
    if args.snapshot and probe.last_frame is not None:
        save_png(probe.last_frame, Path(args.snapshot))
        report["snapshot"] = str(args.snapshot)
    reconnects = []
    for _ in range(args.reconnects):
        await asyncio.sleep(1.0)
        again = await Probe(ws_url, cookie, base_url).watch(args.reconnect_window)
        reconnects.append(again.summary()["time_to_first_frame_s"])
    if args.reconnects:
        report["reconnect_time_to_first_frame_s"] = reconnects
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", required=True, help="relay base URL, e.g. http://127.0.0.1:8080")
    parser.add_argument("--password-env", required=True, metavar="VAR")
    parser.add_argument("--device", required=True)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--reconnects", type=int, default=0, metavar="K")
    parser.add_argument("--reconnect-window", type=float, default=5.0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    text = json.dumps(report, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
