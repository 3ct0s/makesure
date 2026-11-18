"""Screen capture sources.

A capture source is created, used and closed on one worker thread: on Windows
the GDI handles mss holds are bound to the thread that created them.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import mss
import numpy as np


@dataclass(slots=True)
class CapturedFrame:
    bgra: np.ndarray  # shape (height, width, 4), dtype uint8, BGRA order

    @property
    def width(self) -> int:
        return int(self.bgra.shape[1])

    @property
    def height(self) -> int:
        return int(self.bgra.shape[0])


class CaptureError(RuntimeError):
    pass


class CaptureSource(ABC):
    @abstractmethod
    def grab(self) -> CapturedFrame:
        """Capture the current screen contents."""

    @abstractmethod
    def close(self) -> None:
        """Release OS resources."""


class MssCapture(CaptureSource):
    """Captures one monitor (1 = primary) with mss."""

    def __init__(self, monitor: int = 1) -> None:
        self._monitor = monitor
        self._sct: mss.MSS | None = None

    def grab(self) -> CapturedFrame:
        if self._sct is None:
            self._sct = mss.MSS()
        monitors = self._sct.monitors
        if self._monitor >= len(monitors):
            raise CaptureError(f"monitor {self._monitor} not found ({len(monitors) - 1} available)")
        shot = self._sct.grab(monitors[self._monitor])
        pixels = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return CapturedFrame(pixels)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class TestPatternCapture(CaptureSource):
    """Synthetic moving pattern for the test-suite. Counts every grab."""

    __test__ = False  # not a pytest test class

    def __init__(self, width: int = 640, height: int = 360) -> None:
        self._frame = np.zeros((height, width, 4), dtype=np.uint8)
        self._frame[:, :, 3] = 255
        self._lock = threading.Lock()
        self.grab_count = 0
        self.closed = False

    def grab(self) -> CapturedFrame:
        with self._lock:
            self.grab_count += 1
            n = self.grab_count
        width = self._frame.shape[1]
        self._frame[:, :, 0] = (n * 3) % 256
        self._frame[:, :, 1] = 64
        self._frame[:, :, 2] = 128
        bar = (n * 8) % width
        self._frame[:, bar : bar + 16, 0:3] = 255
        return CapturedFrame(self._frame)

    def close(self) -> None:
        self.closed = True
