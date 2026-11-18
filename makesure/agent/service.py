"""Agent protocol logic: authentication, the message whitelist and streaming state.

The agent acts on exactly the message types in AGENT_ACCEPTED_TYPES. Anything
else is logged and dropped. There is no input, command, file or clipboard path.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import logging
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from makesure import __version__
from makesure.agent.adaptive import AdaptiveController
from makesure.agent.capture import CaptureSource
from makesure.agent.identity import DeviceIdentity
from makesure.agent.indicator import Indicator, IndicatorState, LinkState
from makesure.agent.streamer import Streamer, VideoSettings
from makesure.agent.transport import AuthRejected, Connection, ConnectionClosed, Transport
from makesure.common.aio import cancel_and_wait
from makesure.common.config import AgentConfig, remove_key_from_yaml_file
from makesure.common.protocol import (
    AGENT_ACCEPTED_TYPES,
    MsgType,
    ProtocolError,
    auth_payload,
    decode_control,
    encode_control,
    now_ms,
)

log = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT_S = 15.0


def _bounded_int(msg: dict[str, Any], key: str, upper: int) -> int:
    value = msg.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(upper, value))


class AgentService:
    def __init__(
        self,
        config: AgentConfig,
        identity: DeviceIdentity,
        transport: Transport,
        capture_factory: Callable[[], CaptureSource],
        indicator: Indicator,
        token_config_file: Path | None = None,
    ) -> None:
        self._config = config
        self._identity = identity
        self._transport = transport
        self._capture_factory = capture_factory
        self._indicator = indicator
        self._token_config_file = token_config_file
        self._enroll_token = config.enroll_token
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._conn: Connection | None = None
        self._streamer: Streamer | None = None
        self._streamer_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._paused = False
        self._state = IndicatorState(device_id=identity.device_id)

    # ----- controls, called from the indicator (main) thread -----

    def pause(self) -> None:
        self._call_soon(self._spawn, self._apply_pause(True))

    def allow(self) -> None:
        self._call_soon(self._spawn, self._apply_pause(False))

    def quit(self) -> None:
        self._call_soon(self._request_stop)

    def _call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            for arg in args:
                if asyncio.iscoroutine(arg):
                    arg.close()
            return
        with contextlib.suppress(RuntimeError):  # loop closed in between
            loop.call_soon_threadsafe(fn, *args)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _request_stop(self) -> None:
        if self._stop is not None:
            log.info("quit requested")
            self._stop.set()

    # ----- main loop -----

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._publish()
        try:
            await self._transport.run(self._session, self._stop)
        finally:
            await self._stop_streaming()

    @property
    def streaming(self) -> bool:
        return self._streamer_task is not None

    @property
    def state(self) -> IndicatorState:
        return self._state

    async def _session(self, conn: Connection) -> None:
        self._conn = conn
        try:
            await self._authenticate(conn)
            self._publish(link=LinkState.CONNECTED, detail="")
            await self._send_status()
            while True:
                await self.handle_message(await conn.recv())
        finally:
            self._conn = None
            await self._stop_streaming()
            if self._state.link is LinkState.CONNECTED:
                self._publish(link=LinkState.CONNECTING)

    async def _recv_control(self, conn: Connection, expected: str) -> dict[str, Any]:
        raw = await asyncio.wait_for(conn.recv(), HANDSHAKE_TIMEOUT_S)
        if not isinstance(raw, str):
            raise ConnectionClosed("binary message during handshake")
        try:
            msg = decode_control(raw)
        except ProtocolError as exc:
            raise ConnectionClosed(f"bad handshake message: {exc}") from exc
        if msg["type"] != expected:
            raise ConnectionClosed(f"expected {expected}, got {msg['type'][:40]!r}")
        return msg

    async def _authenticate(self, conn: Connection) -> None:
        challenge = await self._recv_control(conn, MsgType.AUTH_CHALLENGE)
        try:
            nonce = base64.b64decode(str(challenge.get("nonce", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConnectionClosed("malformed challenge") from exc
        if len(nonce) != 32:
            raise ConnectionClosed("malformed challenge")
        host = conn.relay_host
        fields: dict[str, Any] = {
            "device_id": self._identity.device_id,
            "public_key": base64.b64encode(self._identity.public_key_raw).decode("ascii"),
            "signature": base64.b64encode(self._identity.sign(auth_payload(nonce, host))).decode("ascii"),
            "host": host,
            "agent_version": __version__,
        }
        if self._enroll_token:
            fields["enroll_token"] = self._enroll_token
        await conn.send_text(encode_control(MsgType.AUTH, **fields))
        result = await self._recv_control(conn, MsgType.AUTH_RESULT)
        if result.get("ok") is not True:
            reason = str(result.get("reason", "rejected"))[:200]
            self._publish(link=LinkState.REJECTED, detail=f"Refused: {reason}")
            raise AuthRejected(reason)
        log.info("authenticated to relay as %s", self._identity.device_id)
        if self._enroll_token:
            self._enroll_token = ""
            self._forget_config_token()

    def _forget_config_token(self) -> None:
        if self._token_config_file is None:
            return
        try:
            if remove_key_from_yaml_file(self._token_config_file, "enroll_token"):
                log.info("removed used enroll_token from %s", self._token_config_file)
        except OSError as exc:
            log.warning("could not remove enroll_token from %s: %s", self._token_config_file, exc)

    # ----- relay messages -----

    async def handle_message(self, raw: str | bytes) -> bool:
        """Act on one relay message. Returns False when it was ignored."""
        if not isinstance(raw, str):
            log.warning("ignored a binary message from the relay")
            return False
        try:
            msg = decode_control(raw)
        except ProtocolError as exc:
            log.warning("ignored malformed relay message: %s", exc)
            return False
        msg_type = msg["type"]
        if msg_type not in AGENT_ACCEPTED_TYPES:
            log.warning("ignored unsupported relay message type %r", msg_type[:40])
            return False
        if msg_type == MsgType.START:
            await self._on_start(_bounded_int(msg, "viewers", 10_000))
        elif msg_type == MsgType.STOP:
            await self._stop_streaming()
        elif msg_type == MsgType.KEYFRAME_REQUEST:
            if self._streamer is not None:
                self._streamer.request_keyframe()
        elif msg_type == MsgType.QUALITY_HINT:
            if self._streamer is not None:
                self._streamer.on_quality_hint(
                    _bounded_int(msg, "drops", 10_000), _bounded_int(msg, "backlog", 10_000)
                )
        elif msg_type == MsgType.PING:
            await self._on_ping(msg)
        elif msg_type == MsgType.PONG:
            pass
        else:
            log.warning("ignored %s outside the handshake", msg_type)
            return False
        return True

    async def _on_ping(self, msg: dict[str, Any]) -> None:
        t0 = msg.get("t0")
        if isinstance(t0, bool) or not isinstance(t0, (int, float)) or self._conn is None:
            return
        now = now_ms()
        with contextlib.suppress(ConnectionClosed):
            await self._conn.send_text(encode_control(MsgType.PONG, t0=t0, t1=now, t2=now_ms()))

    async def _on_start(self, viewers: int) -> None:
        if self._paused:
            log.info("start refused: viewing is paused by the local user")
            await self._send_status()
            return
        if self._streamer_task is None:
            conn = self._conn
            if conn is None:
                return
            cfg = self._config
            adaptive = AdaptiveController(
                cfg.bitrate_kbps, cfg.min_bitrate_kbps, cfg.max_bitrate_kbps, cfg.fps
            )
            settings = VideoSettings(
                cfg.codec, cfg.width, cfg.height, cfg.keyframe_interval_s, cfg.x264_preset
            )
            self._streamer = Streamer(self._capture_factory, settings, adaptive, conn)
            self._streamer_task = asyncio.create_task(self._streamer.run())
        self._publish(streaming=True, viewers=viewers)

    async def _stop_streaming(self) -> None:
        task = self._streamer_task
        self._streamer_task = None
        self._streamer = None
        if task is not None:
            await cancel_and_wait(task)
        if self._state.streaming:
            self._publish(streaming=False, viewers=0)

    async def _apply_pause(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        log.info("local user %s viewing", "paused" if paused else "allowed")
        if paused:
            await self._stop_streaming()
        self._publish(paused=paused)
        await self._send_status()

    async def _send_status(self) -> None:
        if self._conn is None:
            return
        with contextlib.suppress(ConnectionClosed):
            await self._conn.send_text(encode_control(MsgType.STATUS, paused=self._paused))

    def _publish(self, **changes: Any) -> None:
        self._state = dataclasses.replace(self._state, **changes)
        self._indicator.update(self._state)
