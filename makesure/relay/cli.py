"""Relay command line: ``python -m makesure.relay {run,set-password,enroll,devices,revoke}``."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import logging
import os
import sys
import time
from pathlib import Path

import uvicorn

from makesure import __version__
from makesure.common.config import (
    ConfigError,
    RelayConfig,
    build_dataclass,
    is_loopback_host,
    load_yaml_mapping,
)
from makesure.common.logging_setup import setup_logging
from makesure.relay.app import create_app
from makesure.relay.auth import MIN_PASSWORD_LENGTH, hash_password, hash_token, new_enroll_token
from makesure.relay.store import Store
from makesure.relay.tunnel import TunnelError, create_tunnel

log = logging.getLogger("makesure.relay")


def load_config(path: Path | None) -> RelayConfig:
    values = load_yaml_mapping(path) if path is not None else {}
    config = build_dataclass(RelayConfig, values, str(path or "defaults"))
    config.validate()
    return config


def cmd_set_password(config: RelayConfig, args: argparse.Namespace) -> int:
    if args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        password = getpass.getpass("New operator password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    Store(config.resolved_data_dir()).set_password_hash(hash_password(password))
    print("Operator password saved.")
    return 0


def cmd_enroll(config: RelayConfig, args: argparse.Namespace) -> int:
    name = args.name.strip()
    if not name or len(name) > 80:
        print("--name must be 1 to 80 characters.", file=sys.stderr)
        return 1
    if not 1 <= args.ttl_minutes <= 7 * 24 * 60:
        print("--ttl-minutes must be between 1 and 10080.", file=sys.stderr)
        return 1
    token = new_enroll_token()
    now = time.time()
    expires = now + args.ttl_minutes * 60
    Store(config.resolved_data_dir()).add_enroll_token(hash_token(token), name, now, expires)
    when = dt.datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
    print(f"Enrollment token for '{name}' (single use, expires {when}):\n\n  {token}\n")
    print("On the device run:  MakeSureAgent.exe --relay-url wss://<relay>/ws/agent --enroll " + token)
    return 0


def cmd_devices(config: RelayConfig, _: argparse.Namespace) -> int:
    devices = Store(config.resolved_data_dir()).list_devices()
    if not devices:
        print("No enrolled devices.")
        return 0
    print(f"{'DEVICE ID':<20} {'NAME':<30} LAST SEEN")
    for d in devices:
        seen = dt.datetime.fromtimestamp(d.last_seen).strftime("%Y-%m-%d %H:%M") if d.last_seen else "never"
        print(f"{d.device_id:<20} {d.name:<30} {seen}")
    return 0


def cmd_revoke(config: RelayConfig, args: argparse.Namespace) -> int:
    if Store(config.resolved_data_dir()).remove_device(args.device_id):
        print(f"Revoked {args.device_id}. A running relay disconnects it within 5 seconds.")
        return 0
    print(f"No device {args.device_id}.", file=sys.stderr)
    return 1


def _announce(url: str) -> None:
    agent_url = "wss://" + url.split("://", 1)[1] + "/ws/agent"
    banner = f"\n  Public viewer URL: {url}\n  Agent relay_url:   {agent_url}\n"
    log.info("public URL %s", url)
    print(banner, flush=True)


def cmd_run(config: RelayConfig, _: argparse.Namespace) -> int:
    data_dir = config.resolved_data_dir()
    setup_logging(config.log_level, data_dir / "logs" / "relay.log")
    store = Store(data_dir)
    if store.password_hash() is None:
        print("No operator password set. Run: python -m makesure.relay set-password", file=sys.stderr)
        return 2
    if not is_loopback_host(config.host):
        log.warning("relay is listening on %s, not only on this machine", config.host)
    tunnel = create_tunnel(config.tunnel, config.port, _announce)
    app = create_app(config, store, lambda: tunnel.public_url)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            ws="websockets-sansio",
            ws_max_size=8 * 1024 * 1024,
            ws_ping_interval=20.0,
            ws_ping_timeout=20.0,
            ws_per_message_deflate=False,
            proxy_headers=config.trust_forwarded_headers,
            forwarded_allow_ips="127.0.0.1,::1",
            server_header=False,
            log_config=None,
        )
    )
    log.info(
        "MakeSure relay %s on http://%s:%d (data in %s)", __version__, config.host, config.port, data_dir
    )
    try:
        tunnel.start()
    except TunnelError as exc:
        print(f"Tunnel error: {exc}", file=sys.stderr)
        return 2
    try:
        server.run()
    except KeyboardInterrupt:  # uvicorn re-raises Ctrl+C after its graceful shutdown
        pass
    finally:
        tunnel.stop()
    log.info("relay stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m makesure.relay", description="MakeSure relay")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", type=Path, help="relay YAML config")
        return p

    add("run", "start the relay and its tunnel").set_defaults(func=cmd_run)
    p = add("set-password", "set the operator password")
    p.add_argument("--password-env", metavar="VAR", help="read the password from this environment variable")
    p.set_defaults(func=cmd_set_password)
    p = add("enroll", "create a one-time device enrollment token")
    p.add_argument("--name", required=True, help='device name, e.g. "Lobby kiosk"')
    p.add_argument("--ttl-minutes", type=int, default=60, metavar="MIN", help="validity, 1 to 10080 minutes")
    p.set_defaults(func=cmd_enroll)
    add("devices", "list enrolled devices").set_defaults(func=cmd_devices)
    p = add("revoke", "remove an enrolled device")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return int(args.func(config, args))
