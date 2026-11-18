# PyInstaller spec for the MakeSure agent (Windows 10/11 x64).
#
#   pyinstaller packaging/agent.spec                 -> dist/MakeSureAgent.exe (windowed, no console)
#   pyinstaller packaging/agent.spec -- --console    -> dist/MakeSureAgentDebug.exe (console, for logs)
#
# Onefile unpacks to %TEMP% on every start. If antivirus or slow starts are a
# problem, build a folder instead: set ONEFILE = False below (the --onedir
# layout), which puts MakeSureAgent.exe plus an _internal folder in dist/.

import argparse
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

parser = argparse.ArgumentParser()
parser.add_argument("--console", action="store_true", help="console debug build")
options = parser.parse_args()
CONSOLE = options.console or os.environ.get("MAKESURE_CONSOLE") == "1"
ONEFILE = True
NAME = "MakeSureAgentDebug" if CONSOLE else "MakeSureAgent"

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 (SPECPATH is set by PyInstaller)

# PyAV ships FFmpeg DLLs (av.libs) next to its extension modules.
av_datas, av_binaries, av_hidden = collect_all("av")

hidden = (
    av_hidden
    + collect_submodules("websockets")  # lazy imports PyInstaller cannot see
    + collect_submodules("makesure.agent")
    + collect_submodules("makesure.common")
    + collect_submodules("mss")  # backend chosen at runtime (mss.windows.gdi)
    + ["numpy", "yaml", "tkinter", "tkinter.messagebox"]
    + ["cryptography.hazmat.primitives.asymmetric.ed25519"]
)

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "agent_entry.py")],
    pathex=[str(ROOT)],
    binaries=av_binaries,
    datas=av_datas + [(str(ROOT / "config" / "agent.example.yaml"), "config")],
    hiddenimports=hidden,
    excludes=[
        # relay-side and test-only packages never belong in the agent
        "fastapi", "starlette", "uvicorn", "httpx", "pytest", "makesure.relay",
        "PIL", "matplotlib", "IPython",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if ONEFILE:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        console=CONSOLE,
        disable_windowed_traceback=False,
        upx=False,
        runtime_tmpdir=None,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=CONSOLE, upx=False)  # noqa: F821
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)  # noqa: F821
