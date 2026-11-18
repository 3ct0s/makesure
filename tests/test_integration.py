"""End to end over real sockets: uvicorn relay, real agent service, websocket viewers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import av
import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from makesure.common.protocol import FrameHeader, split_frame

from .conftest import PASSWORD, AgentRunner, RelayServer, async_wait_until, issue_token


async def open_viewer(relay: RelayServer, device_id: str) -> ClientConnection:
    async with httpx.AsyncClient(base_url=relay.base_url) as http:
        response = await http.post(
            "/api/login", json={"password": PASSWORD}, headers={"Origin": relay.base_url}
        )
        response.raise_for_status()
        cookie = response.cookies["ms_session"]
    return await websockets.connect(
        f"ws://127.0.0.1:{relay.port}/ws/view/{device_id}",
        origin=relay.base_url,  # type: ignore[arg-type]
        additional_headers={"Cookie": f"ms_session={cookie}"},
        compression=None,
    )


async def read_frames(
    ws: ClientConnection, count: int, timeout: float = 10.0
) -> list[tuple[FrameHeader, bytes]]:
    frames: list[tuple[FrameHeader, bytes]] = []

    async def collect() -> None:
        while len(frames) < count:
            message = await ws.recv()
            if isinstance(message, bytes):
                header, payload = split_frame(message)
                frames.append((header, bytes(payload)))

    await asyncio.wait_for(collect(), timeout)
    return frames


async def read_state(
    ws: ClientConnection, predicate: Callable[[dict[str, object]], bool], timeout: float = 10.0
) -> dict[str, object]:
    async def wait() -> dict[str, object]:
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                msg = json.loads(message)
                if msg.get("type") == "state" and predicate(msg):
                    return dict(msg)

    return await asyncio.wait_for(wait(), timeout)


def agent_url(relay: RelayServer) -> str:
    return f"ws://127.0.0.1:{relay.port}/ws/agent"


async def start_enrolled_agent(relay: RelayServer, agent_factory: Callable[..., AgentRunner]) -> AgentRunner:
    agent = agent_factory(agent_url(relay), token=issue_token(relay.store))
    assert await async_wait_until(lambda: relay.ctx.registry.is_online(agent.device_id))
    return agent


async def test_idle_until_viewed_then_stream_then_stop(
    relay: RelayServer, agent_factory: Callable[..., AgentRunner]
) -> None:
    agent = await start_enrolled_agent(relay, agent_factory)
    await asyncio.sleep(1.5)
    assert agent.capture.grab_count == 0, "agent must not capture while nobody watches"

    viewer = await open_viewer(relay, agent.device_id)
    frames = await read_frames(viewer, 20)
    assert frames[0][0].keyframe
    assert [h.seq for h, _ in frames] == list(range(frames[0][0].seq, frames[0][0].seq + 20))
    decoder = av.CodecContext.create("h264", "r")
    decoded = [f for _, payload in frames for f in decoder.decode(av.Packet(payload))]
    assert len(decoded) >= 19 and decoded[0].width == 640
    assert agent.service.state.streaming

    await viewer.close()
    assert await async_wait_until(lambda: not agent.service.streaming, 5)
    grabs = agent.capture.grab_count
    await asyncio.sleep(0.7)
    assert agent.capture.grab_count == grabs


async def test_fan_out_to_two_viewers(relay: RelayServer, agent_factory: Callable[..., AgentRunner]) -> None:
    agent = await start_enrolled_agent(relay, agent_factory)
    first = await open_viewer(relay, agent.device_id)
    await read_frames(first, 5)
    second = await open_viewer(relay, agent.device_id)
    a, b = await asyncio.gather(read_frames(first, 30), read_frames(second, 30))
    assert b[0][0].keyframe, "a joining viewer starts at a keyframe"
    assert {h.seq for h, _ in a} & {h.seq for h, _ in b}, "both viewers see the same stream"
    assert agent.service.state.viewers == 2
    await first.close()
    await second.close()


async def test_agent_reconnects_after_relay_restart(
    relay: RelayServer, agent_factory: Callable[..., AgentRunner]
) -> None:
    agent = await start_enrolled_agent(relay, agent_factory)
    relay.stop()
    assert await async_wait_until(lambda: agent.service.state.link.value == "connecting", 10)
    await asyncio.sleep(1.0)
    relay.start()
    assert await async_wait_until(lambda: relay.ctx.registry.is_online(agent.device_id), 15)
    viewer = await open_viewer(relay, agent.device_id)
    frames = await read_frames(viewer, 5)
    assert frames[0][0].keyframe
    await viewer.close()


async def test_local_pause_is_visible_to_viewers(
    relay: RelayServer, agent_factory: Callable[..., AgentRunner]
) -> None:
    agent = await start_enrolled_agent(relay, agent_factory)
    viewer = await open_viewer(relay, agent.device_id)
    await read_frames(viewer, 3)
    agent.service.pause()
    await read_state(viewer, lambda m: m["paused"] is True)
    assert await async_wait_until(lambda: not agent.service.streaming, 5)
    grabs = agent.capture.grab_count
    await asyncio.sleep(0.7)
    assert agent.capture.grab_count == grabs
    agent.service.allow()
    await read_state(viewer, lambda m: m["paused"] is False and m["streaming"] is True)
    frames = await read_frames(viewer, 3)
    assert frames[0][0].keyframe
    await viewer.close()


async def test_revoked_agent_is_dropped(
    relay: RelayServer, agent_factory: Callable[..., AgentRunner]
) -> None:
    agent = await start_enrolled_agent(relay, agent_factory)
    relay.store.remove_device(agent.device_id)
    assert await async_wait_until(lambda: not relay.ctx.registry.is_online(agent.device_id), 10)
    assert await async_wait_until(lambda: agent.service.state.link.value == "rejected", 10)
