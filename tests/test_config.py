from __future__ import annotations

import sys
from pathlib import Path

import pytest

from makesure.agent.main import build_config, parse_args
from makesure.common.config import (
    AgentConfig,
    ConfigError,
    RelayConfig,
    agent_data_dir,
    app_dir,
    build_dataclass,
    load_yaml_mapping,
    remove_key_from_yaml_file,
)

ROOT = Path(__file__).resolve().parent.parent


def test_example_configs_load() -> None:
    agent = build_dataclass(AgentConfig, load_yaml_mapping(ROOT / "config" / "agent.example.yaml"))
    agent.validate()
    assert (agent.width, agent.height, agent.fps, agent.bitrate_kbps) == (1280, 720, 15, 1500)
    relay = build_dataclass(RelayConfig, load_yaml_mapping(ROOT / "config" / "relay.example.yaml"))
    relay.validate()
    assert relay.host == "127.0.0.1" and relay.tunnel.provider == "cloudflare_quick"


def test_unknown_and_mistyped_keys_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        build_dataclass(AgentConfig, {"relay_url": "wss://x/ws/agent", "disable_indicator": True})
    with pytest.raises(ConfigError, match="integer"):
        build_dataclass(AgentConfig, {"fps": "fast"})
    with pytest.raises(ConfigError, match="unknown"):
        build_dataclass(RelayConfig, {"tunnel": {"provider": "none", "token": "x"}})


@pytest.mark.parametrize(
    "url",
    ["ws://relay.example.com/ws/agent", "http://127.0.0.1/ws/agent", "wss:///ws/agent", ""],
)
def test_insecure_or_bad_relay_url_rejected(url: str) -> None:
    with pytest.raises(ConfigError):
        AgentConfig(relay_url=url).validate()


def test_plain_ws_allowed_only_for_loopback() -> None:
    AgentConfig(relay_url="ws://127.0.0.1:8080/ws/agent").validate()
    AgentConfig(relay_url="wss://relay.example.com/ws/agent").validate()


def test_cli_overrides_config_file(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.yaml"
    cfg.write_text("relay_url: wss://a.example/ws/agent\nfps: 10\nenroll_token: tok\n")
    config, token_file = build_config(parse_args(["--config", str(cfg), "--fps", "5"]), tmp_path)
    assert config.fps == 5 and config.relay_url == "wss://a.example/ws/agent"
    assert token_file == cfg
    config, token_file = build_config(parse_args(["--config", str(cfg), "--enroll", "cli"]), tmp_path)
    assert config.enroll_token == "cli" and token_file is None


def test_remove_key_keeps_comments(tmp_path: Path) -> None:
    cfg = tmp_path / "a.yaml"
    cfg.write_text("# hi\nenroll_token: x\nfps: 3\n")
    assert remove_key_from_yaml_file(cfg, "enroll_token")
    assert cfg.read_text() == "# hi\nfps: 3\n"
    assert not remove_key_from_yaml_file(cfg, "enroll_token")


def test_frozen_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "dist" / "MakeSureAgent.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert app_dir() == exe.parent


def test_windows_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MAKESURE_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert agent_data_dir() == tmp_path / "MakeSure"
