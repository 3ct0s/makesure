"""The agent can only show the screen: no input, commands, files or clipboard."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT_SIDE = sorted((ROOT / "makesure" / "agent").glob("*.py")) + sorted(
    (ROOT / "makesure" / "common").glob("*.py")
)

FORBIDDEN_IMPORTS = {
    "subprocess",
    "ctypes",
    "multiprocessing",
    "pty",
    "pexpect",
    "shutil",
    "socketserver",
    "http.server",
    "telnetlib",
    "ftplib",
    "smtplib",
    "paramiko",
    "pickle",
    "marshal",
    "pyautogui",
    "pynput",
    "keyboard",
    "mouse",
    "pywinauto",
    "pydirectinput",
    "win32api",
    "win32con",
    "win32gui",
    "win32clipboard",
    "win32process",
    "pyperclip",
    "comtypes",
    "wmi",
    "winreg",
}
FORBIDDEN_OS_CALLS = {
    "system",
    "popen",
    "startfile",
    "fork",
    "forkpty",
    "kill",
    "killpg",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
    "posix_spawn",
    "posix_spawnp",
}
FORBIDDEN_BUILTINS = {"eval", "exec", "compile", "__import__"}
INJECTION_DISTRIBUTIONS = {
    "pyautogui",
    "pynput",
    "keyboard",
    "mouse",
    "pywinauto",
    "pydirectinput",
    "pywin32",
    "pyperclip",
    "comtypes",
    "wmi",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("path", AGENT_SIDE, ids=lambda p: p.name)
def test_agent_source_has_no_dangerous_capability(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for module in _imported_modules(tree):
        parts = module.split(".")
        prefixes = {".".join(parts[: i + 1]) for i in range(len(parts))}
        assert not prefixes & FORBIDDEN_IMPORTS, f"{path.name} imports {module}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                assert not (func.value.id == "os" and func.attr in FORBIDDEN_OS_CALLS), (
                    f"{path.name} calls os.{func.attr}"
                )
            if isinstance(func, ast.Name):
                assert func.id not in FORBIDDEN_BUILTINS, f"{path.name} calls {func.id}()"


def test_agent_runtime_loads_no_injection_module() -> None:
    code = "import json, sys, makesure.agent.main; print(json.dumps(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    loaded = {name.split(".")[0] for name in json.loads(out)}
    bad = loaded & (INJECTION_DISTRIBUTIONS | {"win32api", "win32clipboard", "pywinauto"})
    assert not bad


def test_no_injection_capable_dependency() -> None:
    requirements = (ROOT / "requirements.txt").read_text().lower()
    installed = {(dist.metadata["Name"] or "").lower() for dist in metadata.distributions()}
    for name in INJECTION_DISTRIBUTIONS:
        assert f"\n{name}" not in requirements
        assert name not in installed


def test_agent_accepts_only_the_documented_messages() -> None:
    from makesure.common.protocol import AGENT_ACCEPTED_TYPES

    assert {
        "auth_challenge",
        "auth_result",
        "start",
        "stop",
        "keyframe_request",
        "quality_hint",
        "ping",
        "pong",
    } == AGENT_ACCEPTED_TYPES


@pytest.mark.parametrize("module", ["registry.py", "app.py"])
def test_relay_never_writes_frames_to_disk(module: str) -> None:
    source = (ROOT / "makesure" / "relay" / module).read_text()
    for needle in ("open(", "write_bytes", "write_text", "tofile"):
        assert needle not in source
