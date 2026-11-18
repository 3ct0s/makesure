"""Agent protocol behaviour against a scripted relay (no network)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from makesure.agent.capture import TestPatternCapture
from makesure.agent.identity import load_or_create_identity
from makesure.agent.indicator import HeadlessIndicator, LinkState
from makesure.agent.service import AgentService
from makesure.agent.transport import AuthRejected, Connection, ConnectionClosed, Handler, Transport
from makesure.common.config import AgentConfig
from makesure.common.protocol import encode_control

from .conftest import async_wait_until


class FakeRelayConnection(Connection):
    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent_text: list[dict[str, object]] = []
        self.frames_sent = 0

    @property
    def relay_host(self) -> str:
        return "relay.test"

    async def send_text(self, text: str) -> None:
        self.sent_text.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        self.frames_sent += 1

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if item is None:
            raise ConnectionClosed("closed by test")
        return item

    def backlog_bytes(self) -> int:
        return 0

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def sent_types(self) -> list[object]:
        return [m["type"] for m in self.sent_text]


class OneShotTransport(Transport):
    def __init__(self, conn: FakeRelayConnection) -> None:
        self.conn = conn
        self.error: BaseException | None = None

    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        task = asyncio.create_task(handler(self.conn))
        await asyncio.wait({task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            self.error = task.exception()
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
            await task


@dataclass
class Harness:
    service: AgentService
    conn: FakeRelayConnection
    capture: TestPatternCapture
    indicator: HeadlessIndicator
    transport: OneShotTransport
    task: asyncio.Task[None]

    async def send(self, msg_type: str, **fields: object) -> None:
        await self.conn.inbox.put(encode_control(msg_type, **fields))

    async def close(self) -> None:
        self.service.quit()
        await asyncio.wait_for(self.task, 10)


async def start_agent(tmp_path: Path, config: AgentConfig | None = None, ok: bool = True) -> Harness:
    conn = FakeRelayConnection()
    capture = TestPatternCapture(320, 180)
    indicator = HeadlessIndicator()
    transport = OneShotTransport(conn)
    config = config or AgentConfig(relay_url="ws://127.0.0.1:9/ws/agent", width=320, height=180)
    service = AgentService(
        config,
        load_or_create_identity(tmp_path / "id"),
        transport,
        capture_factory=lambda: capture,
        indicator=indicator,
        token_config_file=tmp_path / "agent.yaml" if (tmp_path / "agent.yaml").exists() else None,
    )
    task = asyncio.create_task(service.run())
    h = Harness(service, conn, capture, indicator, transport, task)
    await h.send("auth_challenge", nonce=base64.b64encode(bytes(32)).decode())
    assert await async_wait_until(lambda: "auth" in conn.sent_types())
    await h.send("auth_result", ok=ok, reason="" if ok else "unknown_device")
    if ok:
        assert await async_wait_until(lambda: service.state.link is LinkState.CONNECTED)
    return h


async def test_auth_reply_is_signed_for_relay_host(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    auth = next(m for m in h.conn.sent_text if m["type"] == "auth")
    assert auth["host"] == "relay.test"
    assert auth["device_id"] == h.service.state.device_id
    assert "enroll_token" not in auth
    await h.close()


async def test_idle_agent_does_no_capture(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    await h.send("ping", t0=1.0)
    await asyncio.sleep(1.0)
    assert h.capture.grab_count == 0
    assert h.conn.frames_sent == 0
    assert not h.service.streaming
    assert "pong" in h.conn.sent_types()
    await h.close()


async def test_start_and_stop(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    await h.send("start", viewers=2)
    assert await async_wait_until(lambda: h.conn.frames_sent >= 5)
    assert h.service.state.streaming and h.service.state.viewers == 2
    await h.send("keyframe_request")
    await h.send("quality_hint", drops=3, backlog=4)
    await h.send("stop")
    assert await async_wait_until(lambda: not h.service.streaming)
    grabs = h.capture.grab_count
    await asyncio.sleep(0.5)
    assert h.capture.grab_count == grabs
    assert h.capture.closed
    assert not h.service.state.streaming
    await h.close()


async def test_disconnect_stops_capture(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    await h.send("start", viewers=1)
    assert await async_wait_until(lambda: h.capture.grab_count >= 3)
    await h.conn.inbox.put(None)  # relay connection drops
    assert await async_wait_until(lambda: not h.service.streaming)
    grabs = h.capture.grab_count
    await asyncio.sleep(0.4)
    assert h.capture.grab_count == grabs
    await h.close()


CONTROL_LIKE = [
    {"type": "exec", "cmd": "calc.exe"},
    {"type": "shell", "command": "whoami"},
    {"type": "input", "x": 10, "y": 10},
    {"type": "mouse_move", "x": 1, "y": 1},
    {"type": "mouse_click", "button": "left"},
    {"type": "key", "key": "Enter"},
    {"type": "keyboard", "text": "hello"},
    {"type": "clipboard_set", "text": "x"},
    {"type": "clipboard_get"},
    {"type": "file_read", "path": "C:/Windows/win.ini"},
    {"type": "file_write", "path": "x", "data": "y"},
    {"type": "download", "url": "http://x"},
    {"type": "update", "url": "http://x"},
    {"type": "config", "relay_url": "wss://evil"},
    {"type": "START"},
    {"type": "start\u0000"},
]


async def test_unknown_and_control_like_messages_are_ignored(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    sent_before = len(h.conn.sent_text)
    for msg in CONTROL_LIKE:
        assert await h.service.handle_message(json.dumps(msg)) is False, msg
    for raw in [b"\x00binary", "not json", "[]", '{"no_type": 1}', '{"type": 3}']:
        assert await h.service.handle_message(raw) is False, raw
    # handshake messages are not accepted once authenticated
    assert await h.service.handle_message(encode_control("auth_result", ok=False)) is False
    assert await h.service.handle_message(encode_control("auth_challenge", nonce="AA==")) is False
    assert h.capture.grab_count == 0
    assert not h.service.streaming
    assert len(h.conn.sent_text) == sent_before
    assert h.service.state.link is LinkState.CONNECTED
    await h.close()


async def test_bad_field_types_are_harmless(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    assert await h.service.handle_message('{"type":"start","viewers":"lots"}') is True
    assert await async_wait_until(lambda: h.service.streaming)
    assert await h.service.handle_message('{"type":"quality_hint","drops":[1],"backlog":{"a":1}}') is True
    assert await h.service.handle_message('{"type":"ping","t0":"now"}') is True
    await h.close()


async def test_local_pause_refuses_and_stops_viewing(tmp_path: Path) -> None:
    h = await start_agent(tmp_path)
    await h.send("start", viewers=1)
    assert await async_wait_until(lambda: h.capture.grab_count >= 2)
    h.service.pause()
    assert await async_wait_until(lambda: h.service.state.paused and not h.service.streaming)
    assert {"type": "status", "paused": True} in h.conn.sent_text
    grabs = h.capture.grab_count
    await h.send("start", viewers=1)
    await asyncio.sleep(0.5)
    assert h.capture.grab_count == grabs and not h.service.streaming
    h.service.allow()
    assert await async_wait_until(lambda: not h.service.state.paused)
    assert h.conn.sent_text[-1] == {"type": "status", "paused": False}
    await h.send("start", viewers=1)
    assert await async_wait_until(lambda: h.capture.grab_count > grabs)
    await h.close()


async def test_enroll_token_removed_from_config_after_use(tmp_path: Path) -> None:
    config_file = tmp_path / "agent.yaml"
    config_file.write_text(
        "# my config\nrelay_url: ws://127.0.0.1:9/ws/agent\nenroll_token: abc123\nfps: 10\n"
    )
    config = AgentConfig(relay_url="ws://127.0.0.1:9/ws/agent", enroll_token="abc123")
    h = await start_agent(tmp_path, config)
    auth = next(m for m in h.conn.sent_text if m["type"] == "auth")
    assert auth["enroll_token"] == "abc123"
    assert config_file.read_text() == "# my config\nrelay_url: ws://127.0.0.1:9/ws/agent\nfps: 10\n"
    await h.close()


async def test_rejected_device(tmp_path: Path) -> None:
    h = await start_agent(tmp_path, ok=False)
    assert await async_wait_until(lambda: h.service.state.link is LinkState.REJECTED)
    assert await async_wait_until(lambda: h.transport.error is not None)
    assert isinstance(h.transport.error, AuthRejected)
    await h.close()


@pytest.mark.parametrize("nonce", ["", "not base64!", base64.b64encode(b"short").decode()])
async def test_malformed_challenge_closes(tmp_path: Path, nonce: str) -> None:
    conn = FakeRelayConnection()
    service = AgentService(
        AgentConfig(relay_url="ws://127.0.0.1:9/ws/agent"),
        load_or_create_identity(tmp_path / "id"),
        OneShotTransport(conn),
        capture_factory=TestPatternCapture,
        indicator=HeadlessIndicator(),
    )
    await conn.inbox.put(encode_control("auth_challenge", nonce=nonce))
    with pytest.raises(ConnectionClosed):
        await service._session(conn)
    assert conn.sent_text == []
