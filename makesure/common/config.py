"""Configuration models, YAML loading and per-user data directories."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """Raised for invalid or unreadable configuration."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory of the running executable when frozen, else the working directory."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _user_dir(windows_name: str, posix_name: str, env_override: str) -> Path:
    override = os.environ.get(env_override)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / windows_name
    return Path.home() / posix_name


def agent_data_dir() -> Path:
    """Per-user agent data: device key and logs. Never inside the executable."""
    return _user_dir("MakeSure", ".makesure", "MAKESURE_DATA_DIR")


def relay_data_dir() -> Path:
    return _user_dir("MakeSureRelay", ".makesure-relay", "MAKESURE_RELAY_DATA_DIR")


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)
    return path


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class AgentConfig:
    relay_url: str = ""
    enroll_token: str = ""
    monitor: int = 1
    codec: str = "h264"
    width: int = 1280
    height: int = 720
    fps: int = 15
    bitrate_kbps: int = 1500
    min_bitrate_kbps: int = 300
    max_bitrate_kbps: int = 4000
    keyframe_interval_s: float = 2.0
    x264_preset: str = "ultrafast"
    reconnect_max_s: float = 30.0
    log_level: str = "INFO"

    def validate(self) -> None:
        url = urlparse(self.relay_url)
        if url.scheme not in ("wss", "ws") or not url.hostname:
            raise ConfigError("relay_url must be a wss:// URL, for example wss://host/ws/agent")
        if url.scheme == "ws" and not is_loopback_host(url.hostname):
            raise ConfigError("plain ws:// is only allowed for a relay on this machine; use wss://")
        if self.codec not in ("h264", "jpeg"):
            raise ConfigError("codec must be h264 or jpeg")
        if self.x264_preset not in ("ultrafast", "superfast", "veryfast"):
            raise ConfigError("x264_preset must be ultrafast, superfast or veryfast")
        if not (16 <= self.width <= 3840 and 16 <= self.height <= 2160):
            raise ConfigError("width/height out of range")
        if not 1 <= self.fps <= 60:
            raise ConfigError("fps must be between 1 and 60")
        if not 50 <= self.min_bitrate_kbps <= self.bitrate_kbps <= self.max_bitrate_kbps:
            raise ConfigError("need 50 <= min_bitrate_kbps <= bitrate_kbps <= max_bitrate_kbps")
        if not 0.5 <= self.keyframe_interval_s <= 10:
            raise ConfigError("keyframe_interval_s must be between 0.5 and 10")
        if self.monitor < 1:
            raise ConfigError("monitor must be 1 (primary) or higher")
        if self.reconnect_max_s < 1:
            raise ConfigError("reconnect_max_s must be at least 1")


@dataclass
class LoginLimits:
    max_failures: int = 5
    window_s: float = 900.0
    lockout_s: float = 900.0


@dataclass
class TunnelConfig:
    provider: str = "cloudflare_quick"
    cloudflared_path: str = "cloudflared"
    ngrok_path: str = "ngrok"
    ngrok_domain: str = ""
    public_url: str = ""


@dataclass
class RelayConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: str = ""
    log_level: str = "INFO"
    session_hours: float = 12.0
    trust_forwarded_headers: bool = True
    allowed_origins: list[str] = field(default_factory=list)
    viewer_queue_frames: int = 5
    login: LoginLimits = field(default_factory=LoginLimits)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)

    def validate(self) -> None:
        if self.tunnel.provider not in ("cloudflare_quick", "ngrok", "none"):
            raise ConfigError("tunnel.provider must be cloudflare_quick, ngrok or none")
        if not 1 <= self.port <= 65535:
            raise ConfigError("port out of range")
        if not 2 <= self.viewer_queue_frames <= 60:
            raise ConfigError("viewer_queue_frames must be between 2 and 60")
        if self.session_hours <= 0:
            raise ConfigError("session_hours must be positive")

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else relay_data_dir()


T = typing.TypeVar("T")


def build_dataclass(cls: type[T], values: dict[str, Any], where: str = "config") -> T:
    """Create ``cls`` from a mapping, rejecting unknown keys and wrong types."""
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(values) - names
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for key, raw in values.items():
        kwargs[key] = _coerce(hints[key], raw, f"{where}.{key}")
    return cls(**kwargs)


def _coerce(tp: Any, raw: Any, where: str) -> Any:
    if dataclasses.is_dataclass(tp):
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        return build_dataclass(tp, raw, where)  # type: ignore[arg-type]
    if typing.get_origin(tp) is list:
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ConfigError(f"{where} must be a list of strings")
        return list(raw)
    if tp is bool:
        if not isinstance(raw, bool):
            raise ConfigError(f"{where} must be true or false")
        return raw
    if tp is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ConfigError(f"{where} must be an integer")
        return raw
    if tp is float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"{where} must be a number")
        return float(raw)
    if tp is str:
        if raw is None:
            return ""
        if not isinstance(raw, (str, int, float)):
            raise ConfigError(f"{where} must be a string")
        return str(raw)
    raise ConfigError(f"unsupported type for {where}")


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def remove_key_from_yaml_file(path: Path, key: str) -> bool:
    """Delete top-level ``key: ...`` lines from a YAML file, keeping comments."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    prefix = f"{key}:"
    kept = [line for line in lines if not line.startswith(prefix)]
    if len(kept) == len(lines):
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    os.replace(tmp, path)
    return True
