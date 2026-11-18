"""Agent entry point: ``python -m makesure.agent``.

tkinter (the indicator) owns the main thread; asyncio runs in a worker thread.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import traceback
from pathlib import Path

from makesure import __version__
from makesure.agent.capture import MssCapture
from makesure.agent.identity import load_or_create_identity
from makesure.agent.indicator import IndicatorUnavailable, create_indicator
from makesure.agent.service import AgentService
from makesure.agent.transport import Backoff, WebSocketTransport
from makesure.common.config import (
    AgentConfig,
    ConfigError,
    agent_data_dir,
    app_dir,
    build_dataclass,
    is_frozen,
    load_yaml_mapping,
)
from makesure.common.logging_setup import setup_logging

log = logging.getLogger("makesure.agent")

CONFIG_NAME = "agent.yaml"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="MakeSureAgent", description="MakeSure view-only screen streaming agent"
    )
    parser.add_argument(
        "--config", type=Path, help=f"YAML config (default: {CONFIG_NAME} next to the exe or in the data dir)"
    )
    parser.add_argument("--data-dir", type=Path, help="override the per-user data directory")
    parser.add_argument("--relay-url", help="relay agent endpoint, e.g. wss://host/ws/agent")
    parser.add_argument("--enroll", metavar="TOKEN", help="one-time enrollment token from the operator")
    parser.add_argument("--codec", choices=("h264", "jpeg"))
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=int)
    parser.add_argument("--bitrate", type=int, metavar="KBPS", help="starting bitrate in kbps")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--show-id", action="store_true", help="print this device's ID and exit")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def find_config(explicit: Path | None, data_dir: Path) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit}")
        return explicit
    for candidate in (app_dir() / CONFIG_NAME, data_dir / CONFIG_NAME):
        if candidate.is_file():
            return candidate
    return None


def build_config(args: argparse.Namespace, data_dir: Path) -> tuple[AgentConfig, Path | None]:
    """Return the config and, when the enroll token came from a file, that file."""
    path = find_config(args.config, data_dir)
    values = load_yaml_mapping(path) if path is not None else {}
    config = build_dataclass(AgentConfig, values, str(path or "defaults"))
    token_file = path if config.enroll_token else None
    overrides = {
        "relay_url": args.relay_url,
        "codec": args.codec,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "bitrate_kbps": args.bitrate,
        "log_level": args.log_level,
    }
    for key, value in overrides.items():
        if value is not None:
            setattr(config, key, value)
    if args.enroll:
        config.enroll_token = args.enroll
        token_file = None
    if config.bitrate_kbps > config.max_bitrate_kbps:
        config.max_bitrate_kbps = config.bitrate_kbps
    if config.bitrate_kbps < config.min_bitrate_kbps:
        config.min_bitrate_kbps = config.bitrate_kbps
    config.validate()
    return config, token_file


def show_fatal(message: str) -> None:
    log.critical(message)
    if sys.stderr is not None:
        print(f"MakeSure agent: {message}", file=sys.stderr)
    if is_frozen() and sys.platform == "win32":
        try:
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("MakeSure agent", message)
            root.destroy()
        except Exception:  # the message box is best effort; the log has the details
            pass


def _run_network(service: AgentService) -> None:
    try:
        asyncio.run(service.run())
    except Exception:
        log.exception("network thread crashed")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir or agent_data_dir()
    if args.show_id:
        device_id = load_or_create_identity(data_dir).device_id
        if sys.stdout is not None:
            print(device_id)
        return 0
    try:
        config, token_file = build_config(args, data_dir)
    except ConfigError as exc:
        setup_logging("INFO", data_dir / "logs" / "agent.log")
        show_fatal(f"configuration error: {exc}")
        return 2
    setup_logging(config.log_level, data_dir / "logs" / "agent.log")
    try:
        return _run(config, token_file, data_dir)
    except Exception:
        show_fatal("startup failed:\n" + traceback.format_exc(limit=8))
        return 1


def _run(config: AgentConfig, token_file: Path | None, data_dir: Path) -> int:
    identity = load_or_create_identity(data_dir)
    log.info("MakeSure agent %s, device %s, relay %s", __version__, identity.device_id, config.relay_url)
    try:
        indicator = create_indicator()
    except IndicatorUnavailable as exc:
        show_fatal(f"cannot show the viewing indicator, refusing to run: {exc}")
        return 3
    transport = WebSocketTransport(
        config.relay_url,
        Backoff(base_s=1.0, cap_s=config.reconnect_max_s),
        user_agent=f"MakeSureAgent/{__version__}",
    )
    service = AgentService(
        config,
        identity,
        transport,
        capture_factory=lambda: MssCapture(config.monitor),
        indicator=indicator,
        token_config_file=token_file,
    )
    worker = threading.Thread(target=_run_network, args=(service,), name="makesure-net", daemon=True)
    worker.start()
    indicator.run(service, worker.is_alive)
    service.quit()
    worker.join(timeout=10)
    log.info("agent stopped")
    return 0
