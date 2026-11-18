"""Shared fixtures: an in-thread relay server, an in-thread agent, helpers."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import uvicorn

from makesure.agent.indicator import HEADLESS_TEST_ENV, HeadlessIndicator

os.environ[HEADLESS_TEST_ENV] = "1"

from makesure.agent.capture import TestPatternCapture
from makesure.agent.identity import DeviceIdentity, load_or_create_identity
from makesure.agent.service import AgentService
from makesure.agent.transport import Backoff, WebSocketTransport
from makesure.common.config import AgentConfig, LoginLimits, RelayConfig, TunnelConfig
from makesure.relay.app import create_app
from makesure.relay.auth import hash_password, hash_token, new_enroll_token
from makesure.relay.store import Store

PASSWORD = "correct horse battery"
_PASSWORD_HASH = hash_password(PASSWORD)


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


async def async_wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def relay_config(tmp_path: Path) -> RelayConfig:
    return RelayConfig(
        data_dir=str(tmp_path / "relay"),
        login=LoginLimits(max_failures=3, window_s=60, lockout_s=60),
        tunnel=TunnelConfig(provider="none"),
    )


@pytest.fixture
def store(relay_config: RelayConfig) -> Store:
    s = Store(relay_config.resolved_data_dir())
    s.set_password_hash(_PASSWORD_HASH)
    return s


def issue_token(store: Store, name: str = "Test kiosk", ttl: float = 600) -> str:
    token = new_enroll_token()
    now = time.time()
    store.add_enroll_token(hash_token(token), name, now, now + ttl)
    return token


class RelayServer:
    """Runs the real relay app under uvicorn in a background thread."""

    def __init__(self, config: RelayConfig, store: Store, port: int) -> None:
        self.config = config
        self.store = store
        self.port = port
        self.app = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ctx(self):  # type: ignore[no-untyped-def]
        return self.app.state.ctx  # type: ignore[union-attr]

    def start(self) -> None:
        self.app = create_app(self.config, self.store)
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                ws="websockets-sansio",
                ws_per_message_deflate=False,
                log_config=None,
                lifespan="on",
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        assert wait_until(lambda: self._server is not None and self._server.started, 10)

    def stop(self) -> None:
        assert self._server is not None and self._thread is not None
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def relay(relay_config: RelayConfig, store: Store) -> Iterator[RelayServer]:
    server = RelayServer(relay_config, store, free_port())
    server.start()
    yield server
    if server._thread is not None and server._thread.is_alive():
        server.stop()


class AgentRunner:
    """A real AgentService (headless indicator, test capture) on its own thread."""

    def __init__(self, relay_url: str, data_dir: Path, token: str = "") -> None:
        self.identity: DeviceIdentity = load_or_create_identity(data_dir)
        self.capture = TestPatternCapture()
        self.indicator = HeadlessIndicator()
        config = AgentConfig(relay_url=relay_url, enroll_token=token, width=640, height=360, fps=15)
        config.validate()
        self.service = AgentService(
            config,
            self.identity,
            WebSocketTransport(relay_url, Backoff(base_s=0.2, cap_s=1.0), "MakeSureTest"),
            capture_factory=lambda: self.capture,
            indicator=self.indicator,
        )
        self._thread = threading.Thread(target=lambda: asyncio.run(self.service.run()), daemon=True)

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.service.quit()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()


@pytest.fixture
def agent_factory(tmp_path: Path) -> Iterator[Callable[..., AgentRunner]]:
    runners: list[AgentRunner] = []

    def make(relay_url: str, token: str = "", name: str = "agent") -> AgentRunner:
        runner = AgentRunner(relay_url, tmp_path / name, token)
        runners.append(runner)
        runner.start()
        return runner

    yield make
    for runner in runners:
        runner.stop()
