"""Relay web application: viewer UI, operator API and the two websocket endpoints."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from makesure.common.aio import cancel_and_wait
from makesure.common.config import RelayConfig
from makesure.common.protocol import (
    PROTOCOL_VERSION,
    MsgType,
    ProtocolError,
    decode_control,
    encode_control,
    normalize_host,
    now_ms,
)
from makesure.relay.auth import LoginLimiter, SessionManager, verify_agent, verify_password
from makesure.relay.registry import AgentLink, Registry, ViewerSession
from makesure.relay.store import Store

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "viewer" / "static"
# Windows can map .js to text/plain in its registry; module scripts need a JS type.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
SESSION_COOKIE = "ms_session"
DEVICE_ID_RE = re.compile(r"^ms-[a-z2-7]{16}$")
HOST_SAFE_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")
AUTH_TIMEOUT_S = 15.0
MAX_LOGIN_BODY = 4096


class RelayContext:
    """Everything the endpoints share; also handy for tests."""

    def __init__(self, config: RelayConfig, store: Store, public_url: Callable[[], str | None]) -> None:
        self.config = config
        self.store = store
        self.public_url = public_url
        self.registry = Registry(config.viewer_queue_frames)
        self.sessions = SessionManager(config.session_hours * 3600)
        self.limiter = LoginLimiter(config.login)

    def accepted_hosts(self, headers: Headers) -> set[str]:
        hosts = {normalize_host(headers.get("host", ""))}
        if self.config.trust_forwarded_headers and headers.get("x-forwarded-host"):
            hosts.add(normalize_host(headers["x-forwarded-host"].split(",")[0]))
        public = self.public_url()
        for url in [public, *self.config.allowed_origins]:
            if url:
                hosts.add(normalize_host(urlparse(url).netloc))
        hosts.discard("")
        return hosts

    def origin_allowed(self, headers: Headers) -> bool:
        origin = headers.get("origin")
        if not origin:
            return False
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        return normalize_host(parsed.netloc) in self.accepted_hosts(headers)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_https(request: Request, trust_forwarded: bool) -> bool:
    if request.url.scheme == "https":
        return True
    return trust_forwarded and request.headers.get("x-forwarded-proto", "").lower() == "https"


def _security_headers(response: Response, host: str) -> None:
    connect = "'self'"
    if HOST_SAFE_RE.match(host):
        connect += f" wss://{host} ws://{host}"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        f"connect-src {connect}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"


def create_app(
    config: RelayConfig, store: Store, public_url: Callable[[], str | None] = lambda: None
) -> FastAPI:
    ctx = RelayContext(config, store, public_url)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_maintenance(ctx))
        try:
            yield
        finally:
            await cancel_and_wait(task)
            await ctx.registry.close_all()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        _security_headers(response, request.headers.get("host", ""))
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request) -> Response:
        if request.headers.get("origin") and not ctx.origin_allowed(request.headers):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        ip = _client_ip(request)
        wait = ctx.limiter.retry_after(ip)
        if wait > 0:
            return JSONResponse(
                {"error": "locked", "retry_after": int(wait) + 1},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        body = await request.body()
        if len(body) > MAX_LOGIN_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        password = _password_from(body)
        stored = await asyncio.to_thread(store.password_hash)
        ok = (
            stored is not None
            and password is not None
            and await asyncio.to_thread(verify_password, password, stored)
        )
        if not ok:
            ctx.limiter.record_failure(ip)
            log.warning("failed operator login from %s", ip)
            return JSONResponse({"error": "invalid password"}, status_code=401)
        ctx.limiter.record_success(ip)
        token = ctx.sessions.create()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=int(ctx.sessions.lifetime_s),
            httponly=True,
            samesite="strict",
            secure=_is_https(request, config.trust_forwarded_headers),
            path="/",
        )
        log.info("operator logged in from %s", ip)
        return response

    @app.post("/api/logout")
    async def logout(request: Request) -> Response:
        ctx.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return response

    @app.get("/api/devices")
    async def devices(request: Request) -> Response:
        if not ctx.sessions.validate(request.cookies.get(SESSION_COOKIE)):
            return JSONResponse({"error": "login required"}, status_code=401)
        records = await asyncio.to_thread(store.list_devices)
        result = []
        for record in records:
            channel = ctx.registry.channel_info(record.device_id)
            result.append(
                {
                    "device_id": record.device_id,
                    "name": record.name,
                    "online": ctx.registry.is_online(record.device_id),
                    "paused": bool(channel and channel.paused),
                    "viewers": len(channel.viewers) if channel else 0,
                    "last_seen": record.last_seen,
                }
            )
        return JSONResponse({"devices": result})

    @app.websocket("/ws/agent")
    async def agent_socket(ws: WebSocket) -> None:
        await ws.accept()
        link = await _authenticate_agent(ctx, ws)
        if link is None:
            return
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    ctx.registry.on_agent_frame(link, message["bytes"])
                elif message.get("text") is not None:
                    _on_agent_text(ctx, link, message["text"])
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await ctx.registry.detach_agent(link)
            await asyncio.to_thread(store.touch_device, link.device_id, time.time())

    @app.websocket("/ws/view/{device_id}")
    async def view_socket(ws: WebSocket, device_id: str) -> None:
        token = ws.cookies.get(SESSION_COOKIE)
        if not ctx.origin_allowed(ws.headers) or not ctx.sessions.validate(token):
            await ws.close(code=4401)
            return
        record = (
            await asyncio.to_thread(store.get_device, device_id) if DEVICE_ID_RE.match(device_id) else None
        )
        if record is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        viewer = ctx.registry.add_viewer(device_id, record.name, ws)
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is None:
                    continue
                if not ctx.sessions.validate(token):
                    await ws.close(code=4401)
                    break
                _on_viewer_text(ctx, viewer, message["text"])
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await ctx.registry.remove_viewer(viewer)

    return app


def _password_from(body: bytes) -> str | None:
    try:
        data = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    password = data.get("password") if isinstance(data, dict) else None
    return password if isinstance(password, str) and 0 < len(password) <= 1024 else None


async def _authenticate_agent(ctx: RelayContext, ws: WebSocket) -> AgentLink | None:
    nonce = secrets.token_bytes(32)
    hosts = ctx.accepted_hosts(ws.headers)
    try:
        await ws.send_text(
            encode_control(
                MsgType.AUTH_CHALLENGE,
                nonce=base64.b64encode(nonce).decode("ascii"),
                proto=PROTOCOL_VERSION,
            )
        )
        message = await asyncio.wait_for(ws.receive(), AUTH_TIMEOUT_S)
        text = message.get("text")
        if text is None:
            raise ProtocolError("expected an auth message")
        msg = decode_control(text)
        if msg["type"] != MsgType.AUTH:
            raise ProtocolError("expected an auth message")
    except (TimeoutError, WebSocketDisconnect, ProtocolError, RuntimeError) as exc:
        log.info("agent handshake failed: %s", exc)
        with contextlib.suppress(Exception):
            await ws.close(code=4400)
        return None
    result = await asyncio.to_thread(verify_agent, ctx.store, msg, nonce, hosts, time.time())
    if not result.ok:
        log.warning("agent auth refused (%s) for %s", result.reason, result.device_id or "?")
        with contextlib.suppress(Exception):
            await ws.send_text(encode_control(MsgType.AUTH_RESULT, ok=False, reason=result.reason))
            await ws.close(code=4401)
        return None
    await ws.send_text(
        encode_control(MsgType.AUTH_RESULT, ok=True, device_id=result.device_id, name=result.name)
    )
    await asyncio.to_thread(ctx.store.touch_device, result.device_id, time.time())
    return await ctx.registry.attach_agent(result.device_id, result.name, ws)


def _parse(text: str) -> dict[str, Any] | None:
    try:
        return decode_control(text)
    except ProtocolError:
        return None


def _on_agent_text(ctx: RelayContext, link: AgentLink, text: str) -> None:
    msg = _parse(text)
    if msg is None:
        return
    if msg["type"] == MsgType.PONG:
        ctx.registry.on_agent_pong(link, msg)
    elif msg["type"] == MsgType.STATUS:
        ctx.registry.on_agent_status(link, msg.get("paused") is True)
    else:
        log.debug("ignored agent message %r", msg["type"][:40])


def _on_viewer_text(ctx: RelayContext, viewer: ViewerSession, text: str) -> None:
    msg = _parse(text)
    if msg is None:
        return
    if msg["type"] == MsgType.PING:
        t0 = msg.get("t0")
        if isinstance(t0, (int, float)) and not isinstance(t0, bool):
            now = now_ms()
            viewer.send_control(encode_control(MsgType.PONG, t0=t0, t1=now, t2=now))
    elif msg["type"] == MsgType.KEYFRAME_REQUEST:
        ctx.registry.viewer_keyframe_request(viewer)


async def _maintenance(ctx: RelayContext) -> None:
    tick = 0
    while True:
        await asyncio.sleep(1.0)
        tick += 1
        try:
            ctx.registry.send_quality_hints()
            if tick % 2 == 0:
                ctx.registry.ping_agents()
            if tick % 5 == 0:
                enrolled = await asyncio.to_thread(ctx.store.device_ids)
                await ctx.registry.disconnect_unknown(enrolled)
            if tick % 600 == 0:
                await asyncio.to_thread(ctx.store.purge_expired_tokens, time.time())
        except Exception:
            log.exception("maintenance step failed")
