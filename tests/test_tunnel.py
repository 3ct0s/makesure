"""Tunnel providers, using a fake binary (no real tunnel is started)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from makesure.common.config import TunnelConfig
from makesure.relay.tunnel import (
    CloudflareQuickTunnel,
    ExternalTunnel,
    NgrokTunnel,
    TunnelError,
    create_tunnel,
)

from .conftest import wait_until


def test_cloudflare_url_parsing() -> None:
    tunnel = CloudflareQuickTunnel("cloudflared", 8080, lambda _: None)
    line = "2026-11-18T10:00:00Z INF |  https://quiet-lake-1234.trycloudflare.com  |"
    assert tunnel.parse_url(line) == "https://quiet-lake-1234.trycloudflare.com"
    error = 'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"'
    assert tunnel.parse_url(error) is None
    assert tunnel.command("/bin/cloudflared")[-2:] == ["--url", "http://127.0.0.1:8080"]


def test_ngrok_url_parsing() -> None:
    tunnel = NgrokTunnel("ngrok", 8080, "relay.ngrok.app", lambda _: None)
    line = '{"lvl":"info","msg":"started tunnel","url":"https://relay.ngrok.app"}'
    assert tunnel.parse_url(line) == "https://relay.ngrok.app"
    assert tunnel.parse_url("not json") is None
    assert tunnel.command("ngrok")[-2:] == ["--url", "relay.ngrok.app"]


def test_missing_binary_is_a_clear_error() -> None:
    tunnel = create_tunnel(TunnelConfig(cloudflared_path="/nonexistent/cloudflared"), 8080, lambda _: None)
    with pytest.raises(TunnelError, match="not found"):
        tunnel.start()


def test_external_tunnel_reports_configured_url() -> None:
    urls: list[str] = []
    tunnel = ExternalTunnel("https://relay.example.com/", urls.append)
    tunnel.start()
    assert tunnel.public_url == "https://relay.example.com" and urls == [tunnel.public_url]
    assert ExternalTunnel("", urls.append).public_url is None


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the fake binary")
def test_tunnel_restarts_when_process_dies(tmp_path: Path) -> None:
    fake = tmp_path / "cloudflared"
    fake.write_text('#!/bin/sh\necho "INF | https://fake-$$.trycloudflare.com |"\nsleep 0.3\nexit 1\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    urls: list[str] = []
    tunnel = CloudflareQuickTunnel(str(fake), 8080, urls.append)
    tunnel.start()
    try:
        assert wait_until(lambda: len(urls) >= 2, timeout=8)
        assert urls[0] != urls[1]  # each restart announces its new URL
    finally:
        tunnel.stop()
