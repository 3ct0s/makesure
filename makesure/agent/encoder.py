"""Video encoders. Scaling and BGRA to YUV conversion use PyAV's swscale."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from fractions import Fraction

import av
from av.video.codeccontext import VideoCodecContext
from av.video.frame import PictureType
from av.video.reformatter import Interpolation, VideoReformatter

from makesure.agent.capture import CapturedFrame
from makesure.common.protocol import Codec


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    payload: bytes
    keyframe: bool
    width: int
    height: int


def fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Scale down (never up) to fit the box, keep aspect, round to even sizes."""
    scale = min(1.0, box_w / src_w, box_h / src_h)
    width = max(2, int(src_w * scale) // 2 * 2)
    height = max(2, int(src_h * scale) // 2 * 2)
    return width, height


class Encoder(ABC):
    codec: Codec

    def __init__(self, width: int, height: int, fps: int) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self._reformatter = VideoReformatter()

    def _convert(self, frame: CapturedFrame, pix_fmt: str) -> av.VideoFrame:
        src = av.VideoFrame.from_ndarray(frame.bgra, format="bgra")
        return self._reformatter.reformat(
            src,
            width=self.width,
            height=self.height,
            format=pix_fmt,
            interpolation=Interpolation.BILINEAR,
        )

    @abstractmethod
    def encode(self, frame: CapturedFrame, force_keyframe: bool) -> EncodedFrame | None:
        """Encode one frame; None when the encoder produced no output."""

    @abstractmethod
    def set_bitrate(self, kbps: int) -> None:
        """Change the target bitrate without restarting the stream."""


class H264Encoder(Encoder):
    """libx264, baseline profile, no B-frames, Annex B, SPS/PPS on every IDR."""

    codec = Codec.H264

    def __init__(
        self,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        keyframe_interval_s: float,
        preset: str,
    ) -> None:
        super().__init__(width, height, fps)
        keyint = max(1, round(fps * keyframe_interval_s))
        ctx = av.CodecContext.create("libx264", "w")
        ctx.width = width
        ctx.height = height
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = Fraction(1, fps)
        ctx.framerate = Fraction(fps, 1)
        ctx.gop_size = keyint
        ctx.max_b_frames = 0
        self._ctx = ctx
        self.set_bitrate(bitrate_kbps)
        ctx.options = {
            "preset": preset,
            "tune": "zerolatency",
            "profile": "baseline",
            "forced-idr": "1",
            "x264-params": (
                f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0:"
                "repeat-headers=1:annexb=1:nal-hrd=none"
            ),
        }
        ctx.open()
        self._pts = 0

    def set_bitrate(self, kbps: int) -> None:
        # libavcodec's libx264 wrapper reconfigures the running encoder when these change.
        self._ctx.bit_rate = kbps * 1000
        self._ctx.max_bit_rate = kbps * 1000
        self._ctx.rc_buffer_size = kbps * 500

    def encode(self, frame: CapturedFrame, force_keyframe: bool) -> EncodedFrame | None:
        yuv = self._convert(frame, "yuv420p")
        yuv.pts = self._pts
        self._pts += 1
        if force_keyframe:
            yuv.pict_type = PictureType.I
        packets = self._ctx.encode(yuv)
        if not packets:
            return None
        payload = b"".join(bytes(p) for p in packets)
        keyframe = any(p.is_keyframe for p in packets)
        return EncodedFrame(payload, keyframe, self.width, self.height)


class JpegEncoder(Encoder):
    """Every frame is a standalone JPEG; quality follows the bitrate target."""

    codec = Codec.JPEG
    _Q_BEST = 2
    _Q_WORST = 31

    def __init__(self, width: int, height: int, fps: int, bitrate_kbps: int) -> None:
        super().__init__(width, height, fps)
        self._q = 6
        self._target_bytes = 0
        self.set_bitrate(bitrate_kbps)
        self._ctx = self._open(self._q)

    def _open(self, q: int) -> VideoCodecContext:
        # mjpeg ignores bitrate targets and cannot change quantiser while open,
        # so a fixed-quantiser context is reopened when the quality changes.
        ctx = av.CodecContext.create("mjpeg", "w")
        ctx.width = self.width
        ctx.height = self.height
        ctx.pix_fmt = "yuvj420p"
        ctx.time_base = Fraction(1, self.fps)
        ctx.qmin = q
        ctx.qmax = q
        ctx.open()
        return ctx

    def set_bitrate(self, kbps: int) -> None:
        self._target_bytes = kbps * 125 // self.fps

    def encode(self, frame: CapturedFrame, force_keyframe: bool) -> EncodedFrame | None:
        yuv = self._convert(frame, "yuvj420p")
        yuv.pts = 0
        packets = self._ctx.encode(yuv)
        if not packets:
            return None
        payload = b"".join(bytes(p) for p in packets)
        self._adjust_quality(len(payload))
        return EncodedFrame(payload, True, self.width, self.height)

    def _adjust_quality(self, size: int) -> None:
        q = self._q
        if size > self._target_bytes * 1.15 and q < self._Q_WORST:
            q += 1
        elif size < self._target_bytes * 0.7 and q > self._Q_BEST:
            q -= 1
        if q != self._q:
            self._q = q
            self._ctx = self._open(q)
