"""NTP-style clock offset estimation between two peers."""

from __future__ import annotations

from collections import deque


class ClockSync:
    """Estimate ``remote_clock - local_clock`` from ping/pong exchanges.

    A sample is (t0 local send, t1 remote receive, t2 remote send, t3 local
    receive). The estimate uses the sample with the smallest round trip from a
    short sliding window, which is the one least distorted by queueing.
    """

    def __init__(self, window: int = 16) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=window)

    def add_sample(self, t0: float, t1: float, t2: float, t3: float) -> None:
        rtt = (t3 - t0) - (t2 - t1)
        if rtt < 0:
            return
        offset = ((t1 - t0) + (t2 - t3)) / 2.0
        self._samples.append((rtt, offset))

    @property
    def ready(self) -> bool:
        return bool(self._samples)

    @property
    def offset_ms(self) -> float:
        if not self._samples:
            return 0.0
        return min(self._samples)[1]

    @property
    def rtt_ms(self) -> float:
        if not self._samples:
            return 0.0
        return min(self._samples)[0]
