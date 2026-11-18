"""Outbound transport to the relay. The agent never listens on any port."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import ClientConnection, connect

from makesure.common.aio import cancel_and_wait

log = logging.getLogger(__name__)

MAX_INBOUND_MESSAGE = 64 * 1024  # the relay only ever sends small control messages
STABLE_SESSION_S = 10.0


class ConnectionClosed(Exception):
    """The relay connection ended."""


class AuthRejected(Exception):
    """The relay refused this device; retry slowly."""


class Connection(ABC):
    @property
    @abstractmethod
    def relay_host(self) -> str:
        """Host (and port when not default) this connection was dialled to."""

    @abstractmethod
    async def send_text(self, text: str) -> None: ...

    @abstractmethod
    async def send_bytes(self, data: bytes) -> None: ...

    @abstractmethod
    async def recv(self) -> str | bytes:
        """Next message; raises ConnectionClosed when the connection ends."""

    @abstractmethod
    def backlog_bytes(self) -> int:
        """Bytes written but not yet handed to the OS socket."""

    @abstractmethod
    async def close(self) -> None: ...


Handler = Callable[[Connection], Coroutine[Any, Any, None]]


class Transport(ABC):
    @abstractmethod
    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        """Keep a connection up, calling ``handler`` for each one, until ``stop``."""


class Backoff:
    """Exponential backoff with equal jitter, capped."""

    def __init__(self, base_s: float = 1.0, cap_s: float = 30.0, factor: float = 2.0) -> None:
        self.base_s = base_s
        self.cap_s = cap_s
        self.factor = factor
        self._attempt = 0

    def next_delay(self) -> float:
        ceiling = min(self.cap_s, self.base_s * self.factor**self._attempt)
        self._attempt += 1
        return ceiling / 2 + random.uniform(0, ceiling / 2)

    def reset(self) -> None:
        self._attempt = 0


class _WsConnection(Connection):
    def __init__(self, ws: ClientConnection, relay_host: str) -> None:
        self._ws = ws
        self._relay_host = relay_host

    @property
    def relay_host(self) -> str:
        return self._relay_host

    async def send_text(self, text: str) -> None:
        try:
            await self._ws.send(text)
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed(str(exc)) from exc

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._ws.send(data)
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed(str(exc)) from exc

    async def recv(self) -> str | bytes:
        try:
            return await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed(str(exc)) from exc

    def backlog_bytes(self) -> int:
        transport = self._ws.transport
        return transport.get_write_buffer_size() if transport is not None else 0

    async def close(self) -> None:
        await self._ws.close()


class WebSocketTransport(Transport):
    def __init__(self, url: str, backoff: Backoff, user_agent: str) -> None:
        self._url = url
        self._backoff = backoff
        self._user_agent = user_agent
        self._relay_host = urlparse(url).netloc

    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            delay: float | None = None
            try:
                async with connect(
                    self._url,
                    compression=None,
                    max_size=MAX_INBOUND_MESSAGE,
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    user_agent_header=self._user_agent,
                ) as ws:
                    log.info("connected to relay %s", self._relay_host)
                    await self._serve(handler, _WsConnection(ws, self._relay_host), stop)
            except AuthRejected as exc:
                log.error("relay rejected this device: %s", exc)
                delay = self._backoff.cap_s
            except ConnectionClosed as exc:
                log.warning("relay connection closed: %s", exc or "no reason")
            except (OSError, TimeoutError, websockets.WebSocketException) as exc:
                log.warning("cannot reach relay %s: %s", self._relay_host, exc)
            if stop.is_set():
                break
            if time.monotonic() - started >= STABLE_SESSION_S:
                self._backoff.reset()
            if delay is None:
                delay = self._backoff.next_delay()
            log.info("reconnecting in %.1f s", delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    @staticmethod
    async def _serve(handler: Handler, conn: Connection, stop: asyncio.Event) -> None:
        session = asyncio.create_task(handler(conn))
        stopper = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not session.done():
                with contextlib.suppress(ConnectionClosed):
                    await cancel_and_wait(session)
                await conn.close()
        if session.done() and not session.cancelled():
            session.result()  # re-raise AuthRejected / ConnectionClosed
