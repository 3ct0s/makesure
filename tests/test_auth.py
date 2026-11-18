"""Operator and device authentication against the relay app."""

from __future__ import annotations

import argparse
import base64
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from makesure.agent.identity import DeviceIdentity, load_or_create_identity
from makesure.common.config import RelayConfig
from makesure.common.protocol import auth_payload
from makesure.relay.app import create_app
from makesure.relay.auth import hash_token, new_enroll_token
from makesure.relay.cli import cmd_run
from makesure.relay.store import Store

from .conftest import PASSWORD, issue_token

ORIGIN = "http://testserver"


@pytest.fixture
def client(relay_config: RelayConfig, store: Store) -> Iterator[TestClient]:
    with TestClient(create_app(relay_config, store)) as c:
        yield c


def login(client: TestClient, password: str = PASSWORD, **kwargs: Any) -> Any:
    return client.post("/api/login", json={"password": password}, headers={"Origin": ORIGIN}, **kwargs)


def agent_handshake(
    client: TestClient,
    identity: DeviceIdentity,
    token: str | None = None,
    host: str = "testserver",
    forge_signature: bool = False,
) -> dict[str, Any]:
    with client.websocket_connect("/ws/agent") as ws:
        challenge = ws.receive_json()
        assert challenge["type"] == "auth_challenge"
        nonce = base64.b64decode(challenge["nonce"])
        signed = auth_payload(b"\x00" * 32 if forge_signature else nonce, host)
        msg: dict[str, Any] = {
            "type": "auth",
            "device_id": identity.device_id,
            "public_key": base64.b64encode(identity.public_key_raw).decode(),
            "signature": base64.b64encode(identity.sign(signed)).decode(),
            "host": host,
        }
        if token is not None:
            msg["enroll_token"] = token
        ws.send_json(msg)
        return dict(ws.receive_json())


def new_identity(tmp_path: Path, name: str) -> DeviceIdentity:
    return load_or_create_identity(tmp_path / name)


# ----- operator login -----


def test_healthz_reveals_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.json() == {"ok": True}


def test_device_list_requires_login(client: TestClient) -> None:
    assert client.get("/api/devices").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/devices").json() == {"devices": []}


def test_login_cookie_flags(client: TestClient) -> None:
    response = login(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "max-age" in cookie
    assert "secure" not in cookie
    client.cookies.clear()
    response = client.post("/api/login", json={"password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in response.headers["set-cookie"].lower()


def test_wrong_password_and_lockout(client: TestClient) -> None:
    for _ in range(3):
        assert login(client, "wrong password!").status_code == 401
    locked = login(client)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert client.get("/api/devices").status_code == 401


def test_logout_ends_session(client: TestClient) -> None:
    login(client)
    assert client.get("/api/devices").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/devices").status_code == 401


def test_login_from_foreign_origin_refused(client: TestClient) -> None:
    response = client.post(
        "/api/login", json={"password": PASSWORD}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_static_assets_served_with_script_types(client: TestClient) -> None:
    for path in ("/static/viewer.js", "/static/protocol.js"):
        assert client.get(path).headers["content-type"].startswith("text/javascript")
    assert client.get("/static/style.css").headers["content-type"].startswith("text/css")


def test_security_headers(client: TestClient) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert client.get("/api/devices").headers["cache-control"] == "no-store"


def test_relay_refuses_to_start_without_password(tmp_path: Path) -> None:
    config = RelayConfig(data_dir=str(tmp_path / "empty"))
    assert cmd_run(config, argparse.Namespace()) == 2


# ----- viewer socket -----


def test_viewer_socket_needs_session(client: TestClient, store: Store, tmp_path: Path) -> None:
    identity = new_identity(tmp_path, "dev")
    assert agent_handshake(client, identity, issue_token(store))["ok"] is True
    url = f"/ws/view/{identity.device_id}"
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, headers={"Origin": ORIGIN}):
        pass
    login(client)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(url, headers={"Origin": "https://evil.example"}),
    ):
        pass
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(url):  # no Origin at all
        pass
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/view/ms-aaaaaaaaaaaaaaaa", headers={"Origin": ORIGIN}),
    ):
        pass
    with client.websocket_connect(url, headers={"Origin": ORIGIN}) as ws:
        state = ws.receive_json()
        assert state["type"] == "state" and state["online"] is False


# ----- device enrollment and authentication -----


def test_enroll_then_reconnect_without_token(client: TestClient, store: Store, tmp_path: Path) -> None:
    identity = new_identity(tmp_path, "dev")
    result = agent_handshake(client, identity, issue_token(store, "Lobby kiosk"))
    assert result == {
        "type": "auth_result",
        "ok": True,
        "device_id": identity.device_id,
        "name": "Lobby kiosk",
    }
    assert agent_handshake(client, identity)["ok"] is True


def test_unknown_device_without_token(client: TestClient, tmp_path: Path) -> None:
    result = agent_handshake(client, new_identity(tmp_path, "dev"))
    assert result["ok"] is False and result["reason"] == "unknown_device"


def test_bad_token(client: TestClient, tmp_path: Path) -> None:
    result = agent_handshake(client, new_identity(tmp_path, "dev"), new_enroll_token())
    assert result["ok"] is False and result["reason"] == "enrollment_token_invalid"


def test_expired_token(client: TestClient, store: Store, tmp_path: Path) -> None:
    token = new_enroll_token()
    store.add_enroll_token(hash_token(token), "old", time.time() - 120, time.time() - 60)
    assert agent_handshake(client, new_identity(tmp_path, "dev"), token)["ok"] is False


def test_reused_token(client: TestClient, store: Store, tmp_path: Path) -> None:
    token = issue_token(store)
    assert agent_handshake(client, new_identity(tmp_path, "a"), token)["ok"] is True
    assert agent_handshake(client, new_identity(tmp_path, "b"), token)["ok"] is False


def test_wrong_signature(client: TestClient, store: Store, tmp_path: Path) -> None:
    token = issue_token(store)
    result = agent_handshake(client, new_identity(tmp_path, "dev"), token, forge_signature=True)
    assert result["ok"] is False and result["reason"] == "bad_signature"
    # the token was not burned by the failed attempt
    assert agent_handshake(client, new_identity(tmp_path, "dev"), token)["ok"] is True


def test_signature_for_another_relay_host(client: TestClient, store: Store, tmp_path: Path) -> None:
    result = agent_handshake(client, new_identity(tmp_path, "dev"), issue_token(store), host="evil.example")
    assert result["ok"] is False and result["reason"] == "wrong_host"


def test_revoked_device(client: TestClient, store: Store, tmp_path: Path) -> None:
    identity = new_identity(tmp_path, "dev")
    assert agent_handshake(client, identity, issue_token(store))["ok"] is True
    assert store.remove_device(identity.device_id)
    assert agent_handshake(client, identity)["ok"] is False
    login(client)
    assert client.get("/api/devices").json() == {"devices": []}


def test_tokens_stored_hashed(store: Store) -> None:
    token = issue_token(store)
    with sqlite3.connect(store.path) as db:
        dump = "\n".join(db.iterdump())
    assert token not in dump
    assert hash_token(token) in dump
