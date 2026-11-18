"""Adaptive quality: degrade bitrate, then fps, then resolution; recover slowly."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

SCALES: tuple[float, ...] = (1.0, 0.75, 0.5)


@dataclass(frozen=True, slots=True)
class QualityLevel:
    bitrate_kbps: int
    fps: int
    scale: float


class AdaptiveController:
    """Collects congestion signals and decides quality steps once per second."""

    def __init__(
        self,
        start_kbps: int,
        min_kbps: int,
        max_kbps: int,
        max_fps: int,
        *,
        min_fps: int = 5,
        down_cooldown_s: float = 2.0,
        up_after_s: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_kbps = min_kbps
        self.max_kbps = max_kbps
        self.max_fps = max_fps
        self.min_fps = min(min_fps, max_fps)
        self.level = QualityLevel(start_kbps, max_fps, SCALES[0])
        self._down_cooldown_s = down_cooldown_s
        self._up_after_s = up_after_s
        self._clock = clock
        now = clock()
        self._window_start = now
        self._last_change = float("-inf")
        self._healthy_since = now
        self._reset_window()

    def _reset_window(self) -> None:
        self._skips = 0
        self._sends = 0
        self._send_time = 0.0
        self._max_backlog_bytes = 0
        self._hint_drops = 0
        self._hint_backlog = 0

    def record_skip(self) -> None:
        """A capture tick was skipped because the sender was still busy."""
        self._skips += 1

    def record_send(self, seconds: float, backlog_bytes: int) -> None:
        self._sends += 1
        self._send_time += seconds
        self._max_backlog_bytes = max(self._max_backlog_bytes, backlog_bytes)

    def on_quality_hint(self, drops: int, backlog: int) -> None:
        self._hint_drops += max(0, drops)
        self._hint_backlog = max(self._hint_backlog, backlog)

    def congested(self) -> bool:
        frame_interval = 1.0 / self.level.fps
        backlog_limit = self.level.bitrate_kbps * 125 // 2  # half a second of video
        slow_sends = self._sends > 0 and self._send_time / self._sends > 0.8 * frame_interval
        return (
            self._skips > 0
            or self._hint_drops > 0
            or self._hint_backlog >= 3
            or slow_sends
            or self._max_backlog_bytes > backlog_limit
        )

    def evaluate(self) -> QualityLevel | None:
        """Return the new level when it changes, else None. Cheap to call often."""
        now = self._clock()
        if now - self._window_start < 1.0:
            return None
        congested = self.congested()
        self._window_start = now
        self._reset_window()
        new_level: QualityLevel | None = None
        if congested:
            self._healthy_since = now
            if now - self._last_change >= self._down_cooldown_s:
                new_level = self.step_down(self.level)
        elif now - self._healthy_since >= self._up_after_s:
            self._healthy_since = now
            new_level = self.step_up(self.level)
        if new_level is None or new_level == self.level:
            return None
        self.level = new_level
        self._last_change = now
        return new_level

    def step_down(self, level: QualityLevel) -> QualityLevel | None:
        if level.bitrate_kbps > self.min_kbps:
            return replace(level, bitrate_kbps=max(self.min_kbps, int(level.bitrate_kbps * 0.7)))
        if level.fps > self.min_fps:
            return replace(level, fps=max(self.min_fps, int(level.fps * 0.67)))
        index = SCALES.index(level.scale)
        if index + 1 < len(SCALES):
            return replace(level, scale=SCALES[index + 1])
        return None

    def step_up(self, level: QualityLevel) -> QualityLevel | None:
        index = SCALES.index(level.scale)
        if index > 0:
            return replace(level, scale=SCALES[index - 1])
        if level.fps < self.max_fps:
            return replace(level, fps=min(self.max_fps, max(level.fps + 1, math.ceil(level.fps * 1.5))))
        if level.bitrate_kbps < self.max_kbps:
            return replace(level, bitrate_kbps=min(self.max_kbps, int(level.bitrate_kbps * 1.15) + 1))
        return None
