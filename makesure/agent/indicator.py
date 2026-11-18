"""Visible on-screen indicator. It cannot be disabled by config or flags.

The only way to run without a window is the test-suite environment variable
below, and doing so logs a loud warning.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol

log = logging.getLogger(__name__)

HEADLESS_TEST_ENV = "MAKESURE_TEST_HEADLESS_INDICATOR"
POLL_MS = 100


class LinkState(Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IndicatorState:
    device_id: str
    link: LinkState = LinkState.CONNECTING
    streaming: bool = False
    viewers: int = 0
    paused: bool = False
    detail: str = ""


class Controls(Protocol):
    def pause(self) -> None: ...

    def allow(self) -> None: ...

    def quit(self) -> None: ...


class IndicatorUnavailable(RuntimeError):
    pass


class Indicator(ABC):
    @abstractmethod
    def update(self, state: IndicatorState) -> None:
        """Show a new state. Safe to call from any thread."""

    @abstractmethod
    def run(self, controls: Controls, worker_alive: Callable[[], bool]) -> None:
        """Block on the main thread until the user quits or the worker ends."""


def describe(state: IndicatorState) -> tuple[str, str]:
    """Headline and detail line for a state."""
    if state.paused:
        return (
            "MakeSure: viewing paused by the local user",
            "New viewing sessions are refused until you press Allow.",
        )
    if state.streaming:
        noun = "viewer" if state.viewers == 1 else "viewers"
        return (
            "MakeSure: this screen is being viewed",
            f"{max(state.viewers, 1)} {noun} watching. Device {state.device_id}",
        )
    if state.link is LinkState.CONNECTED:
        return ("MakeSure connected (not viewing)", f"Device {state.device_id}")
    if state.link is LinkState.REJECTED:
        return ("MakeSure: the relay refused this device", state.detail or state.device_id)
    return ("MakeSure: connecting to the relay", f"Device {state.device_id}")


class TkIndicator(Indicator):
    _COLORS: ClassVar[dict[str, str]] = {
        "streaming": "#e02424",
        "paused": "#d49b0b",
        "connected": "#2e9d4f",
        "other": "#8a8a8a",
    }

    def __init__(self) -> None:
        import tkinter as tk

        try:
            self._root = tk.Tk()
        except tk.TclError as exc:
            raise IndicatorUnavailable(f"no display for the indicator window: {exc}") from exc
        self._tk = tk
        self._updates: queue.SimpleQueue[IndicatorState] = queue.SimpleQueue()
        self._state: IndicatorState | None = None
        self._controls: Controls | None = None
        self._worker_alive: Callable[[], bool] = lambda: True
        self._build()

    def _build(self) -> None:
        tk = self._tk
        root = self._root
        root.title("MakeSure")
        root.resizable(False, False)
        root.configure(padx=10, pady=6)
        top = tk.Frame(root)
        top.pack(fill="x")
        self._dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self._dot_item = self._dot.create_oval(2, 2, 13, 13, fill=self._COLORS["other"], outline="")
        self._dot.pack(side="left", padx=(0, 8))
        self._headline = tk.Label(top, text="", font=("Segoe UI", 10, "bold"), anchor="w")
        self._headline.pack(side="left")
        self._detail = tk.Label(root, text="", anchor="w", font=("Segoe UI", 9))
        self._detail.pack(fill="x", pady=(2, 4))
        buttons = tk.Frame(root)
        buttons.pack(fill="x")
        self._toggle = tk.Button(buttons, text="", width=14, command=self._on_toggle)
        self._toggle.pack(side="left")
        tk.Button(buttons, text="Quit", width=8, command=self._on_quit).pack(side="right")
        root.protocol("WM_DELETE_WINDOW", self._on_quit)

    def update(self, state: IndicatorState) -> None:
        self._updates.put(state)

    def run(self, controls: Controls, worker_alive: Callable[[], bool]) -> None:
        self._controls = controls
        self._worker_alive = worker_alive
        signal.signal(signal.SIGINT, lambda *_: controls.quit())
        self._root.after(POLL_MS, self._poll)
        self._root.mainloop()

    def _poll(self) -> None:
        latest: IndicatorState | None = None
        while True:
            try:
                latest = self._updates.get_nowait()
            except queue.Empty:
                break
        if latest is not None and latest != self._state:
            self._render(latest)
        if not self._worker_alive():
            self._root.destroy()
            return
        if self._state is not None and (self._state.streaming or self._state.paused):
            self._keep_visible()
        self._root.after(POLL_MS, self._poll)

    def _render(self, state: IndicatorState) -> None:
        first = self._state is None
        self._state = state
        headline, detail = describe(state)
        self._headline.configure(text=headline)
        self._detail.configure(text=detail)
        if state.paused:
            color, toggle = "paused", "Allow"
        elif state.streaming:
            color, toggle = "streaming", "Stop viewing"
        else:
            color = "connected" if state.link is LinkState.CONNECTED else "other"
            toggle = "Block viewing"
        self._dot.itemconfigure(self._dot_item, fill=self._COLORS[color])
        self._toggle.configure(text=toggle)
        prominent = state.streaming or state.paused
        self._root.attributes("-topmost", prominent)
        if first or prominent:
            self._place_top_centre()
            self._keep_visible()

    def _keep_visible(self) -> None:
        if self._root.state() in ("iconic", "withdrawn"):
            self._root.deiconify()
        self._root.attributes("-topmost", True)
        self._root.lift()

    def _place_top_centre(self) -> None:
        self._root.update_idletasks()
        width = self._root.winfo_reqwidth()
        x = max(0, (self._root.winfo_screenwidth() - width) // 2)
        self._root.geometry(f"+{x}+8")

    def _on_toggle(self) -> None:
        if self._controls is None or self._state is None:
            return
        if self._state.paused:
            self._controls.allow()
        else:
            self._controls.pause()

    def _on_quit(self) -> None:
        if self._controls is not None:
            self._controls.quit()


class HeadlessIndicator(Indicator):
    """Test-suite only. Keeps recent states in memory for assertions."""

    def __init__(self) -> None:
        self.states: deque[IndicatorState] = deque(maxlen=200)

    def update(self, state: IndicatorState) -> None:
        self.states.append(state)

    def run(self, controls: Controls, worker_alive: Callable[[], bool]) -> None:
        try:
            while worker_alive():
                time.sleep(0.2)
        except KeyboardInterrupt:
            controls.quit()


def create_indicator() -> Indicator:
    if os.environ.get(HEADLESS_TEST_ENV) == "1":
        log.warning("=" * 72)
        log.warning(
            "%s=1: NO VISIBLE INDICATOR. This mode exists only for the test-suite.", HEADLESS_TEST_ENV
        )
        log.warning("=" * 72)
        return HeadlessIndicator()
    return TkIndicator()
