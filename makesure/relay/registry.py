"""Routing of one agent's stream to its viewers.

Every websocket gets its own sender task (an outbox), so a slow viewer only
ever delays itself. Video for a viewer sits in a small bounded queue; when it
overflows, queued frames are dropped up to the next keyframe and the agent is
asked for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from makesure.common.aio import cancel_and_wait
from makesure.common.clock import ClockSync
from makesure.common.protocol import (
    MsgType,
    ProtocolError,
    encode_control,
    now_ms,
    parse_header,
    rewrite_timestamp,
)

log = logging.getLogger(__name__)

CONTROL_QUEUE = 64
KEYFRAME_REQUEST_MIN_INTERVAL_S = 0.5
CLOCK_BURST = 5


class SocketLike(Protocol):
    async def send_text(self, data: str) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class _Outbox:
    """Owns all sends on one websocket."""

    def __init__(self, ws: SocketLike) -> None:
        self.ws = ws
        self._control: deque[str] = deque(maxlen=CONTROL_QUEUE)
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.closed = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def send_control(self, text: str) -> None:
        if not self.closed:
            self._control.append(text)
            self._wake.set()

    def _pop_frame(self) -> bytes | None:
        return None

    def _has_frames(self) -> bool:
        return False

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._control or self._has_frames():
                    while self._control:
                        await self.ws.send_text(self._control.popleft())
                    frame = self._pop_frame()
                    if frame is not None:
                        await self.ws.send_bytes(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the peer went away; the receive loop cleans up
            log.debug("send failed: %s", exc)
            self.closed = True

    async def close_socket(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close(code=code, reason=reason)

    async def aclose(self) -> None:
        self.closed = True
        if self._task is not None:
            await cancel_and_wait(self._task)


class ViewerSession(_Outbox):
    def __init__(self, ws: SocketLike, device_id: str, max_frames: int) -> None:
        super().__init__(ws)
        self.device_id = device_id
        self.max_frames = max_frames
        self._frames: deque[bytes] = deque()
        self.need_keyframe = True
        self.frames_sent_or_queued = 0
        self.dropped = 0
        self._drops_since_hint = 0

    def _pop_frame(self) -> bytes | None:
        return self._frames.popleft() if self._frames else None

    def _has_frames(self) -> bool:
        return bool(self._frames)

    @property
    def backlog(self) -> int:
        return len(self._frames)

    def offer_frame(self, frame: bytes, keyframe: bool) -> bool:
        """Queue a frame. Returns True when this viewer is waiting for a keyframe."""
        if self.closed:
            return False
        if keyframe:
            if len(self._frames) >= self.max_frames:
                self._drop_queue()
            self.need_keyframe = False
        elif self.need_keyframe:
            return True
        elif len(self._frames) >= self.max_frames:
            self._drop_queue()
            self.need_keyframe = True
            return True
        self._frames.append(frame)
        self.frames_sent_or_queued += 1
        self._wake.set()
        return False

    def _drop_queue(self) -> None:
        dropped = len(self._frames)
        self._frames.clear()
        self.dropped += dropped
        self._drops_since_hint += dropped

    def take_drops(self) -> int:
        drops, self._drops_since_hint = self._drops_since_hint, 0
        return drops


class AgentLink(_Outbox):
    def __init__(self, ws: SocketLike, device_id: str, name: str) -> None:
        super().__init__(ws)
        self.device_id = device_id
        self.name = name
        self.clock = ClockSync()
        self.frames_in = 0
        self.bytes_in = 0
        self._burst: asyncio.Task[None] | None = None

    def start(self) -> None:
        super().start()
        self._burst = asyncio.create_task(self._clock_burst())

    async def _clock_burst(self) -> None:
        for _ in range(CLOCK_BURST):
            self.send_ping()
            await asyncio.sleep(0.2)

    def send_ping(self) -> None:
        self.send_control(encode_control(MsgType.PING, t0=now_ms()))

    async def aclose(self) -> None:
        if self._burst is not None:
            self._burst.cancel()
        await super().aclose()


@dataclass
class DeviceChannel:
    device_id: str
    name: str
    agent: AgentLink | None = None
    viewers: set[ViewerSession] = field(default_factory=set)
    paused: bool = False
    streaming_viewers: int = 0
    last_keyframe_request: float = 0.0

    def state_message(self) -> str:
        return encode_control(
            MsgType.STATE,
            device_id=self.device_id,
            name=self.name,
            online=self.agent is not None,
            paused=self.paused,
            streaming=self.streaming_viewers > 0,
        )


def _number(msg: dict[str, Any], key: str) -> float | None:
    value = msg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class Registry:
    def __init__(self, viewer_queue_frames: int) -> None:
        self.viewer_queue_frames = viewer_queue_frames
        self._channels: dict[str, DeviceChannel] = {}

    def _channel(self, device_id: str, name: str) -> DeviceChannel:
        channel = self._channels.get(device_id)
        if channel is None:
            channel = self._channels[device_id] = DeviceChannel(device_id, name)
        return channel

    def _discard_if_unused(self, channel: DeviceChannel) -> None:
        if channel.agent is None and not channel.viewers:
            self._channels.pop(channel.device_id, None)

    def channel_info(self, device_id: str) -> DeviceChannel | None:
        return self._channels.get(device_id)

    # ----- agents -----

    async def attach_agent(self, device_id: str, name: str, ws: SocketLike) -> AgentLink:
        channel = self._channel(device_id, name)
        previous = channel.agent
        link = AgentLink(ws, device_id, name)
        channel.agent = link
        channel.name = name
        channel.paused = False
        channel.streaming_viewers = 0
        if previous is not None:
            log.info("device %s reconnected; closing its previous connection", device_id)
            await previous.aclose()
            await previous.close_socket(4409, "replaced by a newer connection")
        link.start()
        log.info("agent online: %s (%s)", device_id, name)
        self._broadcast_state(channel)
        self._sync_streaming(channel)
        return link

    async def detach_agent(self, link: AgentLink) -> None:
        await link.aclose()
        channel = self._channels.get(link.device_id)
        if channel is None or channel.agent is not link:
            return
        channel.agent = None
        channel.streaming_viewers = 0
        log.info("agent offline: %s", link.device_id)
        self._broadcast_state(channel)
        self._discard_if_unused(channel)

    def on_agent_status(self, link: AgentLink, paused: bool) -> None:
        channel = self._channels.get(link.device_id)
        if channel is None or channel.agent is not link or channel.paused == paused:
            return
        channel.paused = paused
        if paused:
            channel.streaming_viewers = 0  # the agent stopped on its own
        log.info("device %s: local user %s viewing", link.device_id, "paused" if paused else "allowed")
        self._broadcast_state(channel)
        self._sync_streaming(channel)

    def on_agent_pong(self, link: AgentLink, msg: dict[str, Any]) -> None:
        t3 = now_ms()
        t0, t1, t2 = _number(msg, "t0"), _number(msg, "t1"), _number(msg, "t2")
        if t0 is None or t1 is None or t2 is None:
            return
        link.clock.add_sample(t0, t1, t2, t3)

    def on_agent_frame(self, link: AgentLink, data: bytes) -> None:
        channel = self._channels.get(link.device_id)
        if channel is None or channel.agent is not link:
            return
        try:
            header = parse_header(data)
        except ProtocolError as exc:
            log.warning("bad frame from %s: %s", link.device_id, exc)
            return
        link.frames_in += 1
        link.bytes_in += len(data)
        buf = bytearray(data)
        rewrite_timestamp(buf, round(header.capture_ts_ms - link.clock.offset_ms))
        frame = bytes(buf)
        wants_keyframe = False
        for viewer in channel.viewers:
            if viewer.offer_frame(frame, header.keyframe):
                wants_keyframe = True
        if wants_keyframe:
            self.request_keyframe(channel)

    def request_keyframe(self, channel: DeviceChannel, force: bool = False) -> None:
        if channel.agent is None or channel.streaming_viewers == 0:
            return
        now = time.monotonic()
        if not force and now - channel.last_keyframe_request < KEYFRAME_REQUEST_MIN_INTERVAL_S:
            return
        channel.last_keyframe_request = now
        channel.agent.send_control(encode_control(MsgType.KEYFRAME_REQUEST))

    # ----- viewers -----

    def add_viewer(self, device_id: str, name: str, ws: SocketLike) -> ViewerSession:
        channel = self._channel(device_id, name)
        viewer = ViewerSession(ws, device_id, self.viewer_queue_frames)
        viewer.start()
        was_streaming = channel.streaming_viewers > 0
        channel.viewers.add(viewer)
        self._sync_streaming(channel)
        viewer.send_control(channel.state_message())
        if was_streaming:
            self.request_keyframe(channel, force=True)
        return viewer

    async def remove_viewer(self, viewer: ViewerSession) -> None:
        await viewer.aclose()
        channel = self._channels.get(viewer.device_id)
        if channel is None:
            return
        channel.viewers.discard(viewer)
        self._sync_streaming(channel)
        self._discard_if_unused(channel)

    def viewer_keyframe_request(self, viewer: ViewerSession) -> None:
        channel = self._channels.get(viewer.device_id)
        if channel is not None:
            viewer.need_keyframe = True
            self.request_keyframe(channel)

    # ----- shared -----

    def _sync_streaming(self, channel: DeviceChannel) -> None:
        """Tell the agent start/stop when the set of viewers changes."""
        wanted = len(channel.viewers) if channel.agent is not None and not channel.paused else 0
        previous = channel.streaming_viewers
        channel.streaming_viewers = wanted
        if channel.agent is not None:
            if wanted and wanted != previous:
                channel.agent.send_control(encode_control(MsgType.START, viewers=wanted))
            elif not wanted and previous:
                channel.agent.send_control(encode_control(MsgType.STOP))
        if bool(wanted) != bool(previous):
            self._broadcast_state(channel)

    def _broadcast_state(self, channel: DeviceChannel) -> None:
        message = channel.state_message()
        for viewer in channel.viewers:
            viewer.send_control(message)

    def send_quality_hints(self) -> None:
        for channel in self._channels.values():
            if channel.agent is None or channel.streaming_viewers == 0:
                continue
            drops = sum(v.take_drops() for v in channel.viewers)
            backlog = max((v.backlog for v in channel.viewers), default=0)
            channel.agent.send_control(encode_control(MsgType.QUALITY_HINT, drops=drops, backlog=backlog))

    def ping_agents(self) -> None:
        for channel in self._channels.values():
            if channel.agent is not None:
                channel.agent.send_ping()

    def is_online(self, device_id: str) -> bool:
        channel = self._channels.get(device_id)
        return channel is not None and channel.agent is not None

    async def disconnect_unknown(self, enrolled: set[str]) -> None:
        """Drop agents and viewers of devices that were revoked."""
        for channel in list(self._channels.values()):
            if channel.device_id in enrolled:
                continue
            log.warning("device %s was revoked; disconnecting it", channel.device_id)
            if channel.agent is not None:
                await channel.agent.close_socket(4403, "device revoked")
            for viewer in list(channel.viewers):
                await viewer.close_socket(4404, "device revoked")

    async def close_all(self) -> None:
        for channel in list(self._channels.values()):
            if channel.agent is not None:
                await channel.agent.aclose()
            for viewer in list(channel.viewers):
                await viewer.aclose()
        self._channels.clear()
