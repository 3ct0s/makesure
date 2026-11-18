"""Public exposure of the local relay through a tunnel provider."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from makesure.common.config import TunnelConfig

log = logging.getLogger(__name__)

UrlCallback = Callable[[str], None]


class TunnelError(RuntimeError):
    pass


class TunnelProvider(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def public_url(self) -> str | None:
        """Current public https URL, or None while unknown."""


class ExternalTunnel(TunnelProvider):
    """No managed tunnel: the operator exposes the relay (or uses it locally)."""

    def __init__(self, public_url: str, on_url: UrlCallback) -> None:
        self._url = public_url.rstrip("/") or None
        self._on_url = on_url

    def start(self) -> None:
        if self._url:
            self._on_url(self._url)

    def stop(self) -> None:
        pass

    @property
    def public_url(self) -> str | None:
        return self._url


class ProcessTunnel(TunnelProvider):
    """Supervises a tunnel binary, parses its public URL, restarts it with backoff."""

    MAX_RESTART_DELAY_S = 60.0

    def __init__(self, binary: str, port: int, on_url: UrlCallback) -> None:
        self._binary = binary
        self._port = port
        self._on_url = on_url
        self._url: str | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    @abstractmethod
    def command(self, executable: str) -> list[str]: ...

    @abstractmethod
    def parse_url(self, line: str) -> str | None: ...

    @property
    def public_url(self) -> str | None:
        return self._url

    def _resolve(self) -> str:
        found = shutil.which(self._binary)
        if found:
            return found
        if Path(self._binary).is_file():
            return self._binary
        raise TunnelError(
            f"tunnel binary '{self._binary}' not found. Install it, set its full path in the "
            "relay config, or use tunnel.provider: none"
        )

    def start(self) -> None:
        executable = self._resolve()
        self._thread = threading.Thread(
            target=self._supervise, args=(executable,), name="makesure-tunnel", daemon=True
        )
        self._thread.start()

    def _supervise(self, executable: str) -> None:
        delay = 1.0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                proc = subprocess.Popen(
                    self.command(executable),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except OSError as exc:
                log.error("cannot start tunnel: %s", exc)
            else:
                self._proc = proc
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.debug("tunnel: %s", line.rstrip())
                    url = self.parse_url(line)
                    if url and url != self._url:
                        self._url = url
                        self._on_url(url)
                code = proc.wait()
                self._url = None
                if self._stop.is_set():
                    break
                log.warning("tunnel process exited with code %s", code)
            if time.monotonic() - started > 60:
                delay = 1.0
            log.info("restarting tunnel in %.0f s", delay)
            self._stop.wait(delay)
            delay = min(delay * 2, self.MAX_RESTART_DELAY_S)

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=5)


class CloudflareQuickTunnel(ProcessTunnel):
    """``cloudflared tunnel --url``: a free trycloudflare.com URL, new on every start."""

    _URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")

    def command(self, executable: str) -> list[str]:
        return [executable, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self._port}"]

    def parse_url(self, line: str) -> str | None:
        match = self._URL_RE.search(line)
        return match.group(0) if match else None


class NgrokTunnel(ProcessTunnel):
    """``ngrok http``; the authtoken comes from ngrok's own config or NGROK_AUTHTOKEN."""

    def __init__(self, binary: str, port: int, domain: str, on_url: UrlCallback) -> None:
        super().__init__(binary, port, on_url)
        self._domain = domain

    def command(self, executable: str) -> list[str]:
        cmd = [executable, "http", f"127.0.0.1:{self._port}", "--log", "stdout", "--log-format", "json"]
        if self._domain:
            cmd += ["--url", self._domain]
        return cmd

    def parse_url(self, line: str) -> str | None:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return None
        url = entry.get("url") if isinstance(entry, dict) else None
        if isinstance(url, str) and url.startswith("https://"):
            return url
        return None


def create_tunnel(config: TunnelConfig, port: int, on_url: UrlCallback) -> TunnelProvider:
    if config.provider == "cloudflare_quick":
        return CloudflareQuickTunnel(config.cloudflared_path, port, on_url)
    if config.provider == "ngrok":
        return NgrokTunnel(config.ngrok_path, port, config.ngrok_domain, on_url)
    return ExternalTunnel(config.public_url, on_url)
