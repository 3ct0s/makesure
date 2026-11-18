"""Capture, encode and send loop. It exists only while a viewer is watching."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from makesure.agent.adaptive import AdaptiveController, QualityLevel
from makesure.agent.capture import CaptureSource
from makesure.agent.encoder import Encoder, H264Encoder, JpegEncoder, fit_size
from makesure.agent.transport import Connection
from makesure.common.aio import cancel_and_wait
from makesure.common.protocol import FrameHeader, now_ms

log = logging.getLogger(__name__)

SEND_QUEUE_FRAMES = 2
ERROR_RETRY_S = 1.0


@dataclass(frozen=True, slots=True)
class VideoSettings:
    codec: str
    width: int
    height: int
    keyframe_interval_s: float
    x264_preset: str


class Streamer:
    def __init__(
        self,
        capture_factory: Callable[[], CaptureSource],
        settings: VideoSettings,
        adaptive: AdaptiveController,
        conn: Connection,
    ) -> None:
        self._capture_factory = capture_factory
        self._settings = settings
        self._adaptive = adaptive
        self._conn = conn
        self._capture: CaptureSource | None = None
        self._encoder: Encoder | None = None
        self._encoder_kbps = 0
        self._seq = 0
        self._kf_lock = threading.Lock()
        self._kf_pending = False
        self.frames_sent = 0

    def request_keyframe(self) -> None:
        with self._kf_lock:
            self._kf_pending = True

    def on_quality_hint(self, drops: int, backlog: int) -> None:
        self._adaptive.on_quality_hint(drops, backlog)

    def _take_keyframe_request(self) -> bool:
        with self._kf_lock:
            pending, self._kf_pending = self._kf_pending, False
            return pending

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # One dedicated thread: capture handles must stay on the thread that made them.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="makesure-capture")
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=SEND_QUEUE_FRAMES)
        sender = asyncio.create_task(self._send_loop(queue))
        log.info("streaming started")
        try:
            next_tick = time.monotonic()
            while True:
                level = self._adaptive.level
                interval = 1.0 / level.fps
                if queue.full():
                    # Drop before encoding so the reference chain stays intact.
                    self._adaptive.record_skip()
                else:
                    try:
                        data = await loop.run_in_executor(pool, self._produce, level)
                    except Exception:
                        log.exception("capture or encode failed; retrying")
                        await loop.run_in_executor(pool, self._release)
                        await asyncio.sleep(ERROR_RETRY_S)
                        next_tick = time.monotonic()
                        continue
                    if data is not None:
                        queue.put_nowait(data)
                changed = self._adaptive.evaluate()
                if changed is not None:
                    log.info(
                        "quality now %d kbps, %d fps, scale %.2f",
                        changed.bitrate_kbps,
                        changed.fps,
                        changed.scale,
                    )
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay < -interval:
                    next_tick = time.monotonic()
                    delay = 0.0
                await asyncio.sleep(max(0.0, delay))
        finally:
            with contextlib.suppress(Exception):  # a dead connection is handled by the service
                await cancel_and_wait(sender)
            await loop.run_in_executor(pool, self._release)
            pool.shutdown(wait=False)
            log.info("streaming stopped after %d frames", self.frames_sent)

    async def _send_loop(self, queue: asyncio.Queue[bytes]) -> None:
        while True:
            data = await queue.get()
            started = time.monotonic()
            await self._conn.send_bytes(data)
            self.frames_sent += 1
            self._adaptive.record_send(time.monotonic() - started, self._conn.backlog_bytes())

    def _produce(self, level: QualityLevel) -> bytes | None:
        """Runs on the capture thread."""
        if self._capture is None:
            self._capture = self._capture_factory()
        capture_ts = int(now_ms())
        frame = self._capture.grab()
        box_w = int(self._settings.width * level.scale)
        box_h = int(self._settings.height * level.scale)
        width, height = fit_size(frame.width, frame.height, box_w, box_h)
        encoder = self._encoder
        if encoder is None or (encoder.width, encoder.height, encoder.fps) != (
            width,
            height,
            level.fps,
        ):
            encoder = self._encoder = self._make_encoder(width, height, level)
            self._encoder_kbps = level.bitrate_kbps
        elif self._encoder_kbps != level.bitrate_kbps:
            encoder.set_bitrate(level.bitrate_kbps)
            self._encoder_kbps = level.bitrate_kbps
        encoded = encoder.encode(frame, force_keyframe=self._take_keyframe_request())
        if encoded is None:
            return None
        header = FrameHeader(
            encoder.codec, encoded.keyframe, self._seq, capture_ts, encoded.width, encoded.height
        )
        self._seq += 1
        return header.pack() + encoded.payload

    def _make_encoder(self, width: int, height: int, level: QualityLevel) -> Encoder:
        log.info("encoder %s %dx%d @ %d fps", self._settings.codec, width, height, level.fps)
        if self._settings.codec == "jpeg":
            return JpegEncoder(width, height, level.fps, level.bitrate_kbps)
        return H264Encoder(
            width,
            height,
            level.fps,
            level.bitrate_kbps,
            self._settings.keyframe_interval_s,
            self._settings.x264_preset,
        )

    def _release(self) -> None:
        """Runs on the capture thread."""
        self._encoder = None
        if self._capture is not None:
            self._capture.close()
            self._capture = None
